"""daily_update.py - add the newest days to data/delays.duckdb from the RAW piebro/deutsche-bahn-data files.

Raw files (Hugging Face, `raw_data/year=/month=/day=/*.parquet`, ~170 MB per day) hold the DB IRIS
API answers: `timetables/v1/plan` (the timetable, one XML per station and hour) and `timetables/v1/fchg`
(changes: new times, cancellations, delay-cause messages). Both key a stop by the same `id`.

Per run:
  1. days to add = the day after the newest day in the database .. two days ago. A day is finished
     only once the NEXT day's raw files exist (late trains are reported after midnight).
  2. download raw days  first-1 .. last+1  (the day before carries early timetable snapshots), parse each
     into small per-day plan/fchg files (cached, raw files deleted).
  3. join plan + fchg into rows with the same columns as piebro's monthly files, build a small database
     with delay_store.build_db_file (the SAME cleaning as the full build) and merge it into a copy of
     the old database; swap the copy in. Re-running a day replaces that day's rows (idempotent).

Idea and data: piebro/deutsche-bahn-data (code MIT; data CC BY 4.0 by Deutsche Bahn, see README).
Cancellation = the arrival carries a cancellation time (`clt`), as in piebro's processed files.
"""
from __future__ import annotations

import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import polars as pl

from delay_history import delay_store as ds

try:
    from lxml import etree as ET
except ImportError:                                           # pragma: no cover
    import xml.etree.ElementTree as ET

HF_REPO = "piebro/deutsche-bahn-data"
PLAN, FCHG = "timetables/v1/plan", "timetables/v1/fchg"
PLAN_SCHEMA = {"id": pl.Utf8, "ts": pl.Datetime, "eva": pl.Utf8, "xml_station": pl.Utf8, "train_type": pl.Utf8,
               "train_number": pl.Utf8, "line_number": pl.Utf8, "arr_planned": pl.Utf8}
FCHG_SCHEMA = {"id": pl.Utf8, "ts": pl.Datetime, "arr_changed": pl.Utf8, "arr_cancelled": pl.Boolean,
               "reason_code": pl.Int32, "reason_ts": pl.Utf8}
BERLIN = ZoneInfo("Europe/Berlin")


# ------------------------------------------------------------------------------ parsing
def plan_rows(xml: bytes, url: str, ts) -> list[dict]:
    """One row per stop that HAS an arrival (the first stop of a train has none and is skipped)."""
    m = re.search(r"/plan/(\d+)/", url or "")
    if not m:
        return []
    root = ET.fromstring(xml)
    eva, station, rows = m.group(1).rjust(8, "0"), root.get("station"), []
    for s in root.findall("s"):
        ar = s.find("ar")
        if ar is None or not ar.get("pt"):
            continue
        tl, dp = s.find("tl"), s.find("dp")
        rows.append({"id": s.get("id"), "ts": ts, "eva": eva, "xml_station": station,
                     "train_type": tl.get("c") if tl is not None else None,
                     "train_number": tl.get("n") if tl is not None else None,
                     "line_number": ar.get("l") or (dp.get("l") if dp is not None else None),
                     "arr_planned": ar.get("pt")})
    return rows


def fchg_rows(xml: bytes, ts) -> list[dict]:
    """One row per stop with an arrival change, an arrival cancellation or a delay-cause message
    (<m t="d" c="43" ts="yymmddhhmm"/>; the latest message on the stop wins)."""
    root = ET.fromstring(xml)
    rows = []
    for s in root.findall("s"):
        sid = s.get("id")
        ar = s.find("ar")
        ct = ar.get("ct") if ar is not None else None
        clt = ar.get("clt") if ar is not None else None
        code, code_ts = None, None
        for m in s.iter("m"):
            c = m.get("c")
            if m.get("t") != "d" or not c or not c.isdigit():
                continue
            t = m.get("ts") or ""
            if code_ts is None or t > code_ts:
                code, code_ts = int(c), t
        if sid and (ct or clt or code is not None):
            rows.append({"id": sid, "ts": ts, "arr_changed": ct, "arr_cancelled": clt is not None,
                         "reason_code": code, "reason_ts": code_ts})
    return rows


def parse_raw_file(path: Path) -> tuple[list[dict], list[dict]]:
    cur = duckdb.connect().execute(
        """SELECT timestamp, url, api_name, response_data FROM read_parquet(?)
           WHERE api_name IN (?, ?) AND CAST(status_code AS VARCHAR) = '200' AND response_data IS NOT NULL""",
        [str(path), PLAN, FCHG])
    plan, fchg = [], []
    while batch := cur.fetchmany(500):
        for ts, url, api, xml in batch:
            try:                                              # a truncated answer must not stop the day
                data = xml.encode()
                (plan if api == PLAN else fchg).extend(plan_rows(data, url, ts) if api == PLAN else fchg_rows(data, ts))
            except ET.ParseError:
                continue
    return plan, fchg


def parse_day(d: date, raw_files: list[Path], parsed_root: Path) -> None:
    """Raw day -> parsed_root/<day>/plan.parquet + fchg.parquet (atomic: a crashed run leaves no half day)."""
    plan, fchg = [], []
    for f in raw_files:
        p, c = parse_raw_file(f)
        plan += p
        fchg += c
    tmp = parsed_root / f".{d.isoformat()}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    pl.DataFrame(plan, schema=PLAN_SCHEMA).write_parquet(tmp / "plan.parquet")
    pl.DataFrame(fchg, schema=FCHG_SCHEMA).write_parquet(tmp / "fchg.parquet")
    final = parsed_root / d.isoformat()
    shutil.rmtree(final, ignore_errors=True)
    tmp.rename(final)


# ------------------------------------------------------------------------------- join
def build_rows(parsed_days: list[Path], first: date, last: date, station_names: dict[str, str], out: Path) -> int:
    """Join plan + fchg of the parsed days into one parquet of arrivals planned on first..last
    (columns as in piebro's monthly files). The newest snapshot of a stop wins, as in his pipeline."""
    con = duckdb.connect()
    plans = "[" + ", ".join(f"'{(p / 'plan.parquet').as_posix()}'" for p in parsed_days) + "]"
    fchgs = "[" + ", ".join(f"'{(p / 'fchg.parquet').as_posix()}'" for p in parsed_days) + "]"
    out.parent.mkdir(parents=True, exist_ok=True)
    names_file = out.with_name("station_names.parquet")          # a file, so no pyarrow is needed
    pl.DataFrame({"eva": list(station_names), "name": list(station_names.values())},
                 schema={"eva": pl.Utf8, "name": pl.Utf8}).write_parquet(names_file)
    tmp = out.with_suffix(".tmp")
    con.execute(f"""
        COPY (
          WITH plan AS (
              SELECT * EXCLUDE (rn) FROM (
                  SELECT *, row_number() OVER (PARTITION BY id ORDER BY ts DESC) AS rn
                  FROM read_parquet({plans}, union_by_name = true)) WHERE rn = 1),
          chg AS (                                             -- times/cancellation: newest snapshot that has them
              SELECT id, arr_changed, arr_cancelled FROM (
                  SELECT *, row_number() OVER (PARTITION BY id ORDER BY ts DESC) AS rn
                  FROM read_parquet({fchgs}, union_by_name = true)
                  WHERE arr_changed IS NOT NULL OR arr_cancelled) WHERE rn = 1),
          why AS (                                             -- cause: latest message anywhere on the stop
              SELECT id, arg_max(reason_code, reason_ts) AS reason_code
              FROM read_parquet({fchgs}, union_by_name = true) WHERE reason_code IS NOT NULL GROUP BY id)
          SELECT coalesce(n.name, p.xml_station) AS station_name, p.eva AS eva,
                 p.train_number AS train_number, p.line_number AS line_number, p.train_type AS train_type,
                 strptime(p.arr_planned, '%y%m%d%H%M') AS arrival_planned_time,
                 strptime(c.arr_changed, '%y%m%d%H%M') AS arrival_change_time,
                 coalesce(c.arr_cancelled, false) AS arrival_is_canceled,
                 p.id AS id, w.reason_code AS reason_code
          FROM plan p LEFT JOIN chg c USING (id) LEFT JOIN why w USING (id) LEFT JOIN read_parquet('{names_file.as_posix()}') n ON n.eva = p.eva
          WHERE strptime(p.arr_planned, '%y%m%d%H%M') >= TIMESTAMP '{first} 00:00:00'
            AND strptime(p.arr_planned, '%y%m%d%H%M') <  TIMESTAMP '{last + timedelta(days=1)} 00:00:00'
        ) TO '{tmp.as_posix()}' (FORMAT PARQUET)""")
    tmp.replace(out)
    return con.execute(f"SELECT count(*) FROM read_parquet('{out.as_posix()}')").fetchone()[0]


# ----------------------------------------------------------------------------- database
def merge_into_db(db_path: Path, rows_parquet: Path, first: date, last: date, work_dir: Path,
                  keep_days: int | None) -> tuple[int, date]:
    """Build a small db from the new rows (the SAME cleaning as the full build), then
    old rows outside first..last + new rows -> delays.duckdb.next -> swap in. Returns (rows added, new max day)."""
    small = work_dir / "new_days.duckdb"
    small.unlink(missing_ok=True)
    ds.build_db_file(rows_parquet.as_posix(), small, start_day=first.isoformat())
    nxt = db_path.with_name(db_path.name + ".next")
    nxt.unlink(missing_ok=True)
    con = duckdb.connect(str(nxt))
    con.execute("SET threads=4")
    con.execute("SET memory_limit='2GB'")
    con.execute(f"SET temp_directory='{(work_dir / 'duckdb_tmp').as_posix()}'")
    con.execute(f"ATTACH '{db_path.as_posix()}' AS old (READ_ONLY)")
    con.execute(f"ATTACH '{small.as_posix()}' AS new (READ_ONLY)")
    added = con.execute("SELECT count(*) FROM new.delays").fetchone()[0]
    if not added:
        con.close()
        nxt.unlink(missing_ok=True)
        raise RuntimeError("the new days produced no rows - refusing to change the database")
    con.execute(f"""
        CREATE TABLE delays AS
        SELECT * FROM (
            SELECT * FROM old.delays WHERE arr_day < DATE '{first}' OR arr_day > DATE '{last}' OR arr_day > DATE '{last}'
            UNION ALL BY NAME SELECT * FROM new.delays)
        ORDER BY train_no, eva""")
    if keep_days:
        con.execute(f"DELETE FROM delays WHERE arr_day < (SELECT max(arr_day) FROM delays) - {int(keep_days)}")
    new_max = con.execute("SELECT max(arr_day) FROM delays").fetchone()[0]
    con.execute("DETACH old")
    con.execute("DETACH new")
    con.close()
    nxt.replace(db_path)                                       # Windows: stop the app first (the file is open)
    small.unlink(missing_ok=True)
    return added, new_max


def first_day_to_add(con) -> tuple[date, date]:
    """(newest day in the db, first day to (re)build). A newest day that holds far fewer arrivals than the days
    before it is a PARTLY filled day (the monthly files end at midnight, a crashed run ...): rebuild it too."""
    rows = con.execute("""SELECT arr_day, count(*) FROM delays
                          WHERE arr_day >= (SELECT max(arr_day) FROM delays) - 8 GROUP BY 1 ORDER BY 1""").fetchall()
    newest = rows[-1][0]
    before = sorted(n for _, n in rows[:-1])
    if before and rows[-1][1] < 0.5 * before[len(before) // 2]:
        return newest, newest
    return newest, newest + timedelta(days=1)


# ------------------------------------------------------------------------------- driver
def fetch_day(d: date, raw_dir: Path) -> list[Path]:
    """Download one raw day (both folder spellings: day=5 and day=05)."""
    from huggingface_hub import snapshot_download             # uv sync --extra download
    spellings = {f"month={d.month}/day={d.day}", f"month={d.month:02d}/day={d.day:02d}"}
    snapshot_download(repo_id=HF_REPO, repo_type="dataset", local_dir=raw_dir,
                      allow_patterns=[f"raw_data/year={d.year}/{s}/*" for s in spellings])
    found: list[Path] = []
    for s in spellings:
        found += (raw_dir / "raw_data" / f"year={d.year}" / s).glob("*.parquet")
    return sorted(found)


def run(db_path: Path, work_dir: Path, today: date | None = None, keep_days: int | None = 400,
        fetch=fetch_day, log=print, from_day: date | None = None, to_day: date | None = None) -> str:
    """from_day/to_day refill a range (e.g. one day that came out incomplete); only that range is replaced."""
    today = today or datetime.now(BERLIN).date()
    con = duckdb.connect(str(db_path), read_only=True)
    max_day = con.execute("SELECT max(arr_day) FROM delays").fetchone()[0]
    names = dict(con.execute("SELECT eva, any_value(station_name) FROM delays GROUP BY eva").fetchall())
    con.close()
    first, last = from_day or max_day + timedelta(days=1), to_day or today - timedelta(days=2)
    if last > today - timedelta(days=2):
        raise ValueError(f"{last} is too recent: a day can be added once the day after it is complete")
    if first > last:
        return f"up to date (data to {max_day}; the next day can be added once its following day is complete)"
    parsed_root, raw_dir = work_dir / "parsed", work_dir / "raw_tmp"
    parsed_root.mkdir(parents=True, exist_ok=True)
    need = [first - timedelta(days=1) + timedelta(days=i) for i in range((last - first).days + 3)]
    for d in need:
        if (parsed_root / d.isoformat()).exists():
            continue                                           # parsed before (cache)
        files = fetch(d, raw_dir)
        if not files:
            if d == need[0]:
                continue                                       # the day before may be missing: fine
            raise RuntimeError(f"no raw files for {d}; try again later (the dataset may not have uploaded it yet)")
        parse_day(d, files, parsed_root)
        log(f"{d}: {len(files)} raw files parsed")
        for s in {f"month={d.month}/day={d.day}", f"month={d.month:02d}/day={d.day:02d}"}:
            shutil.rmtree(raw_dir / "raw_data" / f"year={d.year}" / s, ignore_errors=True)
    parsed = [parsed_root / d.isoformat() for d in need if (parsed_root / d.isoformat()).exists()]
    n = build_rows(parsed, first, last, names, work_dir / "new_rows.parquet")
    log(f"{n:,} arrivals planned {first} .. {last}")
    added, new_max = merge_into_db(db_path, work_dir / "new_rows.parquet", first, last, work_dir, keep_days)
    for p in parsed_root.iterdir():                            # keep only what the next run can still use
        if p.is_dir() and not p.name.startswith(".") and date.fromisoformat(p.name) < last:
            shutil.rmtree(p, ignore_errors=True)
    return f"added {added:,} arrivals, data now to {new_max}. Restart the app so it opens the new file."


def seconds_until(now: datetime, hour: int = 4, minute: int = 30) -> float:
    """Seconds from `now` (Berlin time) until the next hour:minute."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
