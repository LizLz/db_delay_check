from datetime import date, datetime
from pathlib import Path

import duckdb
import polars as pl

from delay_history import daily_update as du
from delay_history import delay_store as ds

PLAN_XML = """<?xml version='1.0' encoding='UTF-8'?><timetable station='Teststadt Hbf'>
<s id="111-2610122300-1"><tl t="p" o="80" c="ICE" n="594"/><ar pt="2610130010" l="" ppth="A|B"/><dp pt="2610130012"/></s>
<s id="222-2610122350-1"><tl t="p" o="80" c="RE" n="4711"/><ar pt="2610130100"/></s>
<s id="333-2610130500-1"><tl t="p" o="80" c="S" n="999"/><dp pt="2610130500" l="S5"/></s>
<s id="444-2610130600-1"><tl t="p" o="80" c="IC" n="2000"/><ar pt="2610140100"/></s>
</timetable>"""
FCHG_XML = """<timetable station="Teststadt Hbf" eva="8000001">
<s id="111-2610122300-1" eva="8000001"><ar ct="2610130022"/><m t="d" c="43" ts="2610130010"/></s>
<s id="222-2610122350-1" eva="8000001"><ar clt="2610130030"/></s>
<s id="111-2610122300-1" eva="8000001"><m t="d" c="31" ts="2610130015"/></s>
<s id="999-9999999999-1" eva="8000001"><ar ct="2610130001"/></s>
</timetable>"""


def raw_file(path: Path, rows):
    cols = ["timestamp", "url", "api_name", "query_params", "response_data", "status_code", "error", "duration_ms"]
    df = pl.DataFrame({c: [r[i] for r in rows] for i, c in enumerate(cols)},
                      schema={"timestamp": pl.Datetime, "url": pl.Utf8, "api_name": pl.Utf8, "query_params": pl.Utf8,
                              "response_data": pl.Utf8, "status_code": pl.Utf8, "error": pl.Utf8, "duration_ms": pl.Float64})
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


def make_old_db(tmp: Path) -> Path:
    """a database that ends on 2026-10-11, built exactly like the real one (monthly-style parquet)"""
    pq = tmp / "old.parquet"
    pl.DataFrame({
        "station_name": ["Teststadt Hbf"], "eva": [8000001], "train_number": ["594"], "line_number": [None],
        "train_type": ["ICE"], "arrival_planned_time": [datetime(2026, 10, 11, 12, 0)],
        "arrival_change_time": [datetime(2026, 10, 11, 12, 7)], "arrival_is_canceled": [False],
        "id": ["old-1"], "reason_code": pl.Series([None], dtype=pl.Int32)}).write_parquet(pq)
    db = tmp / "delays.duckdb"
    ds.build_db_file(pq.as_posix(), db, start_day="2025-10-01")
    return db


def test_parsing_functions():
    plan = du.plan_rows(PLAN_XML.encode(), "https://x/timetables/v1/plan/08000001/261013/00", datetime(2026, 10, 13))
    assert [r["id"] for r in plan] == ["111-2610122300-1", "222-2610122350-1", "444-2610130600-1"]   # no arrival -> skipped
    assert plan[0]["eva"] == "08000001" and plan[0]["train_type"] == "ICE" and plan[0]["xml_station"] == "Teststadt Hbf"
    fchg = du.fchg_rows(FCHG_XML.encode(), datetime(2026, 10, 13))
    by = {(r["id"], r["arr_changed"], r["arr_cancelled"], r["reason_code"]) for r in fchg}
    assert ("111-2610122300-1", "2610130022", False, 43) in by and ("222-2610122350-1", None, True, None) in by


def test_full_update_adds_days_and_is_idempotent(tmp_path):
    db = make_old_db(tmp_path)

    def fetch(d, raw_dir):
        # raw day d: plan + fchg answers; the same stops are visible on 13 and 14 (a late snapshot on 14)
        day = raw_dir / "raw_data" / f"year={d.year}" / f"month={d.month}" / f"day={d.day}"
        if d < date(2026, 10, 12) or d > date(2026, 10, 15):
            return []
        f = day / "date_0.parquet"
        raw_file(f, [
            (datetime(d.year, d.month, d.day, 1), "https://x/timetables/v1/plan/08000001/261013/00", du.PLAN, None, PLAN_XML, "200", None, 1.0),
            (datetime(d.year, d.month, d.day, 2), "https://x/timetables/v1/fchg/08000001", du.FCHG, None, FCHG_XML, "200", None, 1.0),
            (datetime(d.year, d.month, d.day, 3), "https://x/timetables/v1/fchg/08000001", du.FCHG, None, "<broken", "200", None, 1.0),
            (datetime(d.year, d.month, d.day, 4), "https://x/timetables/v1/fchg/08000001", du.FCHG, None, None, None, "timeout", 1.0)])
        return [f]

    work = tmp_path / "work"
    msg = du.run(db, work, today=date(2026, 10, 15), fetch=fetch, log=lambda *_: None)   # adds 12 and 13
    assert "added" in msg
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute("SELECT train_no, arr_day, arr_delay_min, is_canceled, reason_code, station_name FROM delays ORDER BY arr_day, train_no").fetchall()
    con.close()
    assert ("594", date(2026, 10, 11), 7, False, None, "Teststadt Hbf") in rows                 # old row kept
    assert ("594", date(2026, 10, 13), 12, False, 31, "Teststadt Hbf") in rows                  # 00:10 -> 00:22 = 12 min; the LATER cause message (31) wins
    assert ("4711", date(2026, 10, 13), 0, True, None, "Teststadt Hbf") in rows                 # cancelled
    assert all(r[1] <= date(2026, 10, 13) for r in rows)                                       # the 14th is not finished yet
    assert not any(r[0] == "2000" for r in rows)                                                # planned on the 14th
    n = len(rows)
    assert du.run(db, work, today=date(2026, 10, 15), fetch=fetch, log=lambda *_: None).startswith("up to date")
    # a later run adds the 14th
    assert "added" in du.run(db, work, today=date(2026, 10, 16), fetch=fetch, log=lambda *_: None)
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT max(arr_day), count(*) FROM delays").fetchone() == (date(2026, 10, 14), n + 1)
    con.close()


def test_refuses_to_swap_in_nothing(tmp_path):
    import pytest
    db = make_old_db(tmp_path)
    def fetch(d, raw_dir):
        f = raw_dir / f"{d}.parquet"
        raw_file(f, [(datetime(2026, 10, 13), "https://x/timetables/v1/fchg/08000001", du.FCHG, None, FCHG_XML, "200", None, 1.0)])
        return [f]
    before = db.stat().st_size
    with pytest.raises(RuntimeError, match="refusing"):
        du.run(db, tmp_path / "w", today=date(2026, 10, 15), fetch=fetch, log=lambda *_: None)
    assert db.stat().st_size == before and not db.with_name("delays.duckdb.next").exists()


def test_refill_replaces_only_the_range(tmp_path):
    db = make_old_db(tmp_path)

    def fetch(d, raw_dir):
        f = raw_dir / "raw_data" / f"year={d.year}" / f"month={d.month}" / f"day={d.day}" / "x.parquet"
        raw_file(f, [
            (datetime(d.year, d.month, d.day, 1), "https://x/timetables/v1/plan/08000001/261013/00", du.PLAN, None, PLAN_XML, "200", None, 1.0),
            (datetime(d.year, d.month, d.day, 2), "https://x/timetables/v1/fchg/08000001", du.FCHG, None, FCHG_XML, "200", None, 1.0)])
        return [f]

    quiet = dict(fetch=fetch, log=lambda *_: None)
    du.run(db, tmp_path / "w", today=date(2026, 10, 16), **quiet)                        # adds 12..14
    con = duckdb.connect(str(db), read_only=True)
    before = con.execute("SELECT count(*) FROM delays").fetchone()[0]
    con.close()
    du.run(db, tmp_path / "w2", today=date(2026, 10, 16), from_day=date(2026, 10, 13), to_day=date(2026, 10, 13), **quiet)
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*), max(arr_day) FROM delays").fetchone() == (before, date(2026, 10, 14))   # no duplicates, 14th kept
    con.close()
    import pytest
    with pytest.raises(ValueError, match="too recent"):
        du.run(db, tmp_path / "w3", today=date(2026, 10, 16), to_day=date(2026, 10, 15), **quiet)


def test_partly_filled_newest_day_is_rebuilt(tmp_path):
    """the monthly files end at midnight: the newest day can hold 2 rows instead of 400,000"""
    pq = tmp_path / "old.parquet"
    days = [date(2026, 9, d) for d in range(24, 31)]
    rows = []
    for i, d in enumerate(days):
        for k in range(10):
            rows.append((d, f"t{i}-{k}"))
    rows += [(date(2026, 10, 1), "a"), (date(2026, 10, 1), "b")]                        # only 2 on the newest day
    pl.DataFrame({
        "station_name": ["S"] * len(rows), "eva": [8000001] * len(rows), "train_number": [str(i) for i in range(len(rows))],
        "line_number": [None] * len(rows), "train_type": ["ICE"] * len(rows),
        "arrival_planned_time": [datetime(d.year, d.month, d.day, 12) for d, _ in rows],
        "arrival_change_time": [datetime(d.year, d.month, d.day, 12, 3) for d, _ in rows],
        "arrival_is_canceled": [False] * len(rows), "id": [i for _, i in rows],
        "reason_code": pl.Series([None] * len(rows), dtype=pl.Int32)}).write_parquet(pq)
    db = tmp_path / "delays.duckdb"
    ds.build_db_file(pq.as_posix(), db, start_day="2025-10-01")
    con = duckdb.connect(str(db), read_only=True)
    assert du.first_day_to_add(con) == (date(2026, 10, 1), date(2026, 10, 1))               # rebuild day 1, not day 2
    con.close()


def test_repair_range_replaces_only_that_range(tmp_path):
    db = make_old_db(tmp_path)
    def fetch(d, raw_dir):
        if not (date(2026, 10, 10) <= d <= date(2026, 10, 14)):
            return []
        f = raw_dir / f"{d}.parquet"
        raw_file(f, [(datetime(d.year, d.month, d.day, 1), "https://x/timetables/v1/plan/08000001/261013/00", du.PLAN, None, PLAN_XML, "200", None, 1.0),
                     (datetime(d.year, d.month, d.day, 2), "https://x/timetables/v1/fchg/08000001", du.FCHG, None, FCHG_XML, "200", None, 1.0)])
        return [f]
    du.run(db, tmp_path / "w", today=date(2026, 10, 20), fetch=fetch, log=lambda *_: None,
           from_day=date(2026, 10, 13), to_day=date(2026, 10, 13))
    con = duckdb.connect(str(db), read_only=True)
    days = sorted(r[0] for r in con.execute("SELECT DISTINCT arr_day FROM delays").fetchall())
    con.close()
    assert days == [date(2026, 10, 11), date(2026, 10, 13)]                                   # old day kept, 13th added


def test_seconds_until_next_run():
    t = lambda h, m: datetime(2026, 10, 16, h, m)
    assert du.seconds_until(t(4, 0)) == 30 * 60
    assert du.seconds_until(t(4, 30)) == 24 * 3600                       # exactly now -> tomorrow
    assert du.seconds_until(t(23, 30)) == 5 * 3600 + 0 * 60 + 0          # 23:30 -> 04:30
