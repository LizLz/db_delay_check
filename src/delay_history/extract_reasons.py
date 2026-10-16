"""extract_reasons.py - delay-cause codes from piebro's RAW data -> a small reasons parquet.

Why: the processed monthly files (monthly_processed_data/) drop the delay-cause messages,
but the raw `timetables/v1/fchg` XML responses (raw_data/) still carry them:

    <s id="...-2510151030-4"><ar ct="2510151052"/><m t="d" c="43" ts="2510151040"/></s>
          t="d" = delay-cause message, c = IRIS cause code, ts = yymmddhhmm

Output: columns  id, reason_code, reason_ts  - the latest cause message per stop id, the same
key as the processed data's `id`, ready for  delay_store.build_db_file(reasons_parquet=...).

Only fchg responses that contain a message are parsed (a SQL LIKE pre-filter), and only the
three columns are kept, so a day of raw data shrinks to a few MB. Process day by day and
delete the raw files afterwards.

The download step (`fetch_day`) needs `pip install huggingface_hub` and was NOT run here.
Parsing, de-duplication and the combine step are tested (test_extract_reasons.py).
"""
from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

import duckdb
import polars as pl

try:
    from lxml import etree as ET
except ImportError:                                   # stdlib fallback
    import xml.etree.ElementTree as ET

HF_REPO = "piebro/deutsche-bahn-data"
SCHEMA = {"id": pl.Utf8, "reason_code": pl.Int32, "reason_ts": pl.Utf8}


def parse_fchg(xml: str) -> list[tuple[str, int, str]]:
    """(stop id, cause code, message ts) for every stop with a delay-cause message.
    Latest message per stop wins (ts is yymmddhhmm, so string order is time order)."""
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    out = []
    for s in root.findall("s"):
        sid = s.get("id")
        if not sid:
            continue
        best = None
        for m in s.iter("m"):                         # messages can sit on s, ar or dp
            code = m.get("c")
            if m.get("t") != "d" or not code or not code.isdigit():
                continue
            ts = m.get("ts") or ""
            if best is None or ts > best[1]:
                best = (int(code), ts)
        if best:
            out.append((sid, best[0], best[1]))
    return out


def extract_files(raw_files: list[Path], out_file: Path) -> int:
    """Raw parquet files (any number of hourly files) -> one reasons parquet. Returns rows."""
    latest: dict[str, tuple[int, str]] = {}
    con = duckdb.connect()
    for f in raw_files:
        cur = con.execute(
            """SELECT response_data FROM read_parquet(?)
               WHERE api_name = 'timetables/v1/fchg'
                 AND CAST(status_code AS VARCHAR) = '200'
                 AND response_data LIKE '%<m %'""", [str(f)])
        while True:
            batch = cur.fetchmany(500)
            if not batch:
                break
            for (xml,) in batch:
                try:
                    rows = parse_fchg(xml)
                except ET.ParseError:                  # a truncated response must not stop the day
                    continue
                for sid, code, ts in rows:
                    if sid not in latest or ts > latest[sid][1]:
                        latest[sid] = (code, ts)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    df = pl.DataFrame(
        {"id": list(latest), "reason_code": [v[0] for v in latest.values()],
         "reason_ts": [v[1] for v in latest.values()]}, schema=SCHEMA)
    tmp = out_file.with_suffix(".tmp")
    df.write_parquet(tmp)
    tmp.replace(out_file)                             # atomic: no half-written file
    return df.height


def combine(day_files: list[Path], out_file: Path) -> int:
    """Per-day reasons files -> one file with the latest message per stop id."""
    if not day_files:
        raise ValueError("no per-day reasons files to combine")
    con = duckdb.connect()
    out_file.parent.mkdir(parents=True, exist_ok=True)     # the output folder may not exist yet
    files = "[" + ", ".join(f"'{Path(f).as_posix()}'" for f in day_files) + "]"
    tmp = out_file.with_suffix(".tmp")
    con.execute(f"""COPY (
        SELECT id, arg_max(reason_code, reason_ts) AS reason_code, max(reason_ts) AS reason_ts
        FROM read_parquet({files}) GROUP BY id) TO '{tmp.as_posix()}' (FORMAT PARQUET)""")
    tmp.replace(out_file)
    return con.execute(f"SELECT count(*) FROM read_parquet('{out_file.as_posix()}')").fetchone()[0]


# --------------------------------------------------------------------- download
def fetch_day(d: date, raw_dir: Path) -> list[Path]:
    """Download one raw day from the Hugging Face dataset (NOT tested here)."""
    from huggingface_hub import snapshot_download         # pip install huggingface_hub
    # the folders may be zero-padded or not (month=10/day=05 vs month=10/day=5): fetch both spellings
    spellings = {f"month={d.month}/day={d.day}", f"month={d.month:02d}/day={d.day:02d}"}
    snapshot_download(repo_id=HF_REPO, repo_type="dataset", local_dir=raw_dir,
                      allow_patterns=[f"raw_data/year={d.year}/{s}/*" for s in spellings])
    found: list[Path] = []
    for s in spellings:
        found += (raw_dir / "raw_data" / f"year={d.year}" / s).glob("*.parquet")
    return sorted(found)


def run(start: date, end: date, work_dir: Path, out_file: Path, keep_raw: bool = False) -> int:
    """Day by day: download -> extract -> delete the raw files. Already-done days are skipped,
    so the job can be stopped and restarted. Then combine into `out_file`."""
    days_dir, raw_dir = work_dir / "reasons_by_day", work_dir / "raw_tmp"
    d = start
    while d <= end:
        day_file = days_dir / f"{d.isoformat()}.parquet"
        if not day_file.exists():
            files = fetch_day(d, raw_dir)
            if files:
                n = extract_files(files, day_file)
                print(f"{d}: {len(files)} raw files -> {n} stops with a cause")
            else:
                print(f"{d}: no raw data")
            if not keep_raw:
                for s in {f"month={d.month}/day={d.day}", f"month={d.month:02d}/day={d.day:02d}"}:
                    shutil.rmtree(raw_dir / "raw_data" / f"year={d.year}" / s, ignore_errors=True)
        d += timedelta(days=1)
    return combine(sorted(days_dir.glob("*.parquet")), out_file)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2025, 10, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    p.add_argument("--work-dir", type=Path, default=Path("data/reasons"))
    p.add_argument("--out", type=Path, default=Path("data/processed/reasons.parquet"))
    a = p.parse_args()
    print(f"{run(a.start, a.end, a.work_dir, a.out)} stops with a delay cause in {a.out}")
