import threading

from api import db_watch


def test_watch_fires_when_the_file_is_replaced(tmp_path):
    db = tmp_path / "delays.duckdb"
    db.write_bytes(b"old")
    fired, calls = [], []

    def fake_sleep(_):
        calls.append(1)
        if len(calls) == 2:                                   # the "update" swaps in a new file
            new = tmp_path / "delays.duckdb.next"
            new.write_bytes(b"new and longer")
            new.replace(db)

    db_watch.watch(db, interval=0, on_change=lambda: fired.append(1), sleep=fake_sleep)
    assert fired == [1] and len(calls) == 2


def test_watch_ignores_an_unchanged_or_missing_file(tmp_path):
    db = tmp_path / "x.duckdb"
    db.write_bytes(b"a")
    stop, n = threading.Event(), []

    def fake_sleep(_):
        n.append(1)
        if len(n) == 3:
            db.unlink()                                       # missing file: not a change
        if len(n) == 5:
            stop.set()

    fired = []
    db_watch.watch(db, interval=0, on_change=lambda: fired.append(1), sleep=fake_sleep, stop=stop)
    assert fired == []
