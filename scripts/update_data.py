"""update_data.py - add the newest days to data/delays.duckdb (see delay_history/daily_update.py).

    uv run python scripts/update_data.py                       # Windows: stop the app first
    uv run python scripts/update_data.py --db data/delays.duckdb --keep-days 400
    uv run python scripts/update_data.py --from-day 2026-10-01 --to-day 2026-10-01   # refill one day
"""
import argparse
from datetime import date
from datetime import date
from pathlib import Path

from delay_history import daily_update

p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
p.add_argument("--db", type=Path, default=Path("data/delays.duckdb"))
p.add_argument("--work-dir", type=Path, default=Path("data/update_work"))
p.add_argument("--keep-days", type=int, default=400, help="drop rows older than this many days before the newest (0 = keep all)")
p.add_argument("--from-day", type=date.fromisoformat, help="refill from this day (YYYY-MM-DD)")
p.add_argument("--to-day", type=date.fromisoformat, help="refill up to this day; only this range is replaced")
a = p.parse_args()
print(daily_update.run(a.db, a.work_dir, keep_days=a.keep_days or None, from_day=a.from_day, to_day=a.to_day))
