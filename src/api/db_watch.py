"""db_watch.py - restart the app when the daily update has swapped in a new database file.

The app opens delays.duckdb once at start-up. After scripts/update_data.py replaces the file, the app still
reads the OLD file until it restarts. In Docker the app container has `restart: unless-stopped`, so the
simplest reliable reload is: notice the new file, stop the process, let Docker start it again (a few seconds).
Switched on with RESTART_ON_NEW_DB=1.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


def fingerprint(path: Path) -> tuple | None:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def default_stop() -> None:
    os.kill(os.getpid(), signal.SIGTERM)             # uvicorn shuts down cleanly; Docker restarts the container


def watch(path: Path, interval: float = 60.0, on_change=default_stop, sleep=time.sleep, stop: threading.Event | None = None) -> None:
    """Blocks; call it in a daemon thread. A replaced file changes the inode, size or mtime."""
    start = fingerprint(path)
    while not (stop and stop.is_set()):
        sleep(interval)
        now = fingerprint(path)
        if now is not None and now != start:
            log.warning("database file changed (%s): restarting to load the new data", path)
            on_change()
            return


def start_in_background(path: Path, interval: float = 60.0) -> threading.Thread:
    t = threading.Thread(target=watch, args=(path, interval), name="db-watch", daemon=True)
    t.start()
    return t
