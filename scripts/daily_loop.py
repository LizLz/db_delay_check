"""daily_loop.py - run the data update every day at 04:30 Berlin time (the Docker `updater` service).

A failed run (Hugging Face down, a day not uploaded yet ...) leaves the database untouched; it is tried again
after RETRY_MINUTES, up to 6 times, and otherwise the next day's run catches up on all missing days.
"""
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from delay_history import daily_update

HOUR, MINUTE = int(os.environ.get("UPDATE_HOUR", "4")), int(os.environ.get("UPDATE_MINUTE", "30"))
RETRY_MINUTES = int(os.environ.get("RETRY_MINUTES", "60"))
DB = Path(os.environ.get("DB_PATH", "data/delays.duckdb"))
WORK = Path(os.environ.get("UPDATE_WORK_DIR", "data/update_work"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("updater")


def once() -> bool:
    for attempt in range(1, 7):
        try:
            log.info(daily_update.run(DB, WORK, log=log.info))
            return True
        except Exception:
            log.exception("update failed (attempt %d of 6)", attempt)
            time.sleep(RETRY_MINUTES * 60)
    return False


if __name__ == "__main__":
    if os.environ.get("UPDATE_ON_START") == "1":
        once()
    while True:
        wait = daily_update.seconds_until(datetime.now(daily_update.BERLIN), HOUR, MINUTE)
        log.info("next update in %.1f hours", wait / 3600)
        time.sleep(wait)
        once()
