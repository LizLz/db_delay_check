"""check_raw_day.py - does piebro's RAW data really contain delay-cause messages?

    uv run python scripts/check_raw_day.py 2025-10-15
    uv run python scripts/check_raw_day.py 2025-10-15 --download   # actually fetch one file

Step 1 (no download): list the raw files of that day and their sizes.
Step 2 (--download): fetch ONE raw file, count the fchg responses that carry <m ...> messages,
        parse them with extract_reasons, and check how many of the stop ids also exist in
        your processed monthly file (the join key the database needs).
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import duckdb

HF_REPO = "piebro/deutsche-bahn-data"


def day_dirs(d: date) -> list[str]:
    # the month/day folders may or may not be zero-padded; try both
    return [f"raw_data/year={d.year}/month={d.month}/day={d.day}",
            f"raw_data/year={d.year}/month={d.month:02d}/day={d.day:02d}"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("day", type=date.fromisoformat)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--root", type=Path, default=Path("data/raw_check"))
    ap.add_argument("--monthly", type=Path, default=Path("data/monthly/monthly_processed_data"))
    a = ap.parse_args()

    from huggingface_hub import HfApi, hf_hub_download
    api = HfApi()
    files = []
    for d in day_dirs(a.day):
        try:
            files = [f for f in api.list_repo_tree(HF_REPO, repo_type="dataset", path_in_repo=d, recursive=True)
                     if getattr(f, "size", None) is not None]
        except Exception as e:                       # folder missing -> try the other spelling
            print(f"{d}: {type(e).__name__}")
            continue
        if files:
            print(f"folder: {d}")
            break
    if not files:
        print("no raw files found for that day - the layout or the day is different")
        return 1
    total = sum(f.size for f in files) / 1e6
    print(f"{len(files)} files, {total:,.0f} MB in total; smallest {min(f.size for f in files)/1e6:.1f} MB,"
          f" largest {max(f.size for f in files)/1e6:.1f} MB")
    if not a.download:
        print("add --download to fetch one file and inspect it")
        return 0

    pick = min(files, key=lambda f: f.size)          # the smallest file keeps the test cheap
    local = hf_hub_download(HF_REPO, pick.path, repo_type="dataset", local_dir=a.root)
    print("downloaded", local)
    con = duckdb.connect()
    con.sql(f"""SELECT api_name, status_code, count(*) AS responses,
                       count(*) FILTER (WHERE response_data LIKE '%<m %') AS with_messages,
                       count(*) FILTER (WHERE response_data LIKE '%t="d"%') AS with_delay_cause
                FROM read_parquet('{local}') GROUP BY ALL ORDER BY responses DESC""").show()

    from delay_history import extract_reasons as er
    out = a.root / "reasons_check.parquet"
    n = er.extract_files([Path(local)], out)
    print(f"{n} stops with a delay-cause message")
    if n == 0:
        return 0
    con.sql(f"SELECT reason_code, count(*) AS n FROM '{out}' GROUP BY 1 ORDER BY 2 DESC LIMIT 10").show()
    monthly = a.monthly / f"data-{a.day.year}-{a.day.month:02d}.parquet"
    if monthly.exists():
        hit = con.execute(f"""SELECT count(*) FROM '{out}' r
                              WHERE r.id IN (SELECT id FROM read_parquet('{monthly.as_posix()}'))""").fetchone()[0]
        print(f"{hit} of {n} cause ids also exist in {monthly.name}  ({hit / n:.0%} join rate)")
    else:
        print(f"{monthly} not found - cannot test the join")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
