"""sync_months.py - download the monthly piebro files you don't have yet, optionally rebuild.

    uv run python -m delay_history.sync_months                      # download missing months since 2025-10
    uv run python -m delay_history.sync_months --build              # ... and rebuild data/delays.duckdb
    uv run python -m delay_history.sync_months --refresh-latest     # also re-download the newest month it
                                                      # already has (it grows during the month)

Needs `huggingface_hub` (uv add huggingface_hub). The download part was NOT run in the
sandbox where this was written (Hugging Face is not reachable from there); the file-name
logic is plain regex on `monthly_processed_data/data-YYYY-MM.parquet`.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

HF_REPO = "piebro/deutsche-bahn-data"
PREFIX = "monthly_processed_data/"
NAME = re.compile(r"^monthly_processed_data/data-(\d{4})-(\d{2})\.parquet$")


def remote_months(files: list[str], first: str) -> dict[str, str]:
    """{'2026-09': 'monthly_processed_data/data-2026-09.parquet', ...} from `first` onward."""
    out = {}
    for f in files:
        m = NAME.match(f)
        if m and f"{m[1]}-{m[2]}" >= first:
            out[f"{m[1]}-{m[2]}"] = f
    return dict(sorted(out.items()))


def local_months(root: Path) -> set[str]:
    return {p.stem.removeprefix("data-")
            for p in (root / "monthly_processed_data").glob("data-*.parquet")}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", type=Path, default=Path("data/monthly"))
    p.add_argument("--first", default="2025-10", help="first month to keep (YYYY-MM)")
    p.add_argument("--build", action="store_true", help="rebuild data/delays.duckdb afterwards")
    p.add_argument("--db", type=Path, default=Path("data/delays.duckdb"))
    p.add_argument("--refresh-latest", action="store_true")
    a = p.parse_args()

    from huggingface_hub import HfApi, hf_hub_download
    remote = remote_months(HfApi().list_repo_files(HF_REPO, repo_type="dataset"), a.first)
    if not remote:
        print("no monthly files found - did the repo layout change?")
        return 1
    have = local_months(a.root)
    todo = [m for m in remote if m not in have]
    if a.refresh_latest and have:
        newest = max(have & set(remote), default=None)
        if newest and newest not in todo:
            todo.append(newest)
    for m in todo:
        print(f"downloading {m} ...", flush=True)
        hf_hub_download(HF_REPO, remote[m], repo_type="dataset", local_dir=a.root,
                        force_download=m in have)
    print(f"months on disk: {sorted(local_months(a.root))}; new/updated: {todo or 'none'}")

    if a.build and (todo or not a.db.exists()):
        from delay_history import delay_store as ds
        pattern = (a.root / "monthly_processed_data" / "data-*.parquet").as_posix()
        ds.build_db_file(pattern, a.db, start_day=f"{a.first}-01")
        print(f"rebuilt {a.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
