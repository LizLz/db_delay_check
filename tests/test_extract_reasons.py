from datetime import datetime

import duckdb
import polars as pl

from delay_history import extract_reasons as er

SNAP1 = """<timetable station="X">
  <s id="r1-2510151030-4"><ar ct="2510151052"/><m id="a" t="d" c="43" ts="2510151040"/></s>
  <s id="r2-2510151100-2"><dp ct="2510151110"><m t="d" c="31" ts="2510151105"/></dp></s>
  <s id="r3-2510151200-1"><ar ct="2510151205"/><m t="q" c="88" ts="2510151201"/></s>
  <s id="r4-2510151300-3"><ar ct="2510151300"/></s>
</timetable>"""
# later snapshot: r1 gets a newer cause; r2's message is gone from this response
SNAP2 = """<timetable station="X">
  <s id="r1-2510151030-4"><ar ct="2510151058"/><m t="d" c="43" ts="2510151040"/><m t="d" c="57" ts="2510151050"/></s>
  <s id="r5-2510151400-6"><ar ct="2510151410"/><m t="d" c="x9" ts="2510151405"/></s>
</timetable>"""


def write_raw(path, snaps):
    rows = [("2025-10-15 11:00:00", "u", api, "{}", xml, status) for api, xml, status in snaps]
    con = duckdb.connect()
    con.execute("CREATE TABLE r(timestamp VARCHAR, url VARCHAR, api_name VARCHAR, query_params VARCHAR,"
                " response_data VARCHAR, status_code VARCHAR)")
    con.executemany("INSERT INTO r VALUES (?,?,?,?,?,?)", rows)
    con.execute(f"COPY r TO '{path}' (FORMAT parquet)")


def test_parse_keeps_only_delay_cause_messages_latest_wins():
    got = {sid: (c, ts) for sid, c, ts in er.parse_fchg(SNAP1)}
    assert got == {"r1-2510151030-4": (43, "2510151040"),
                   "r2-2510151100-2": (31, "2510151105")}      # nested under dp; q-type and none ignored
    got2 = {sid: c for sid, c, _ in er.parse_fchg(SNAP2)}
    assert got2 == {"r1-2510151030-4": 57}                      # newest ts; non-numeric code skipped


def test_extract_files_filters_and_deduplicates(tmp_path):
    write_raw(tmp_path / "h1.parquet", [
        ("timetables/v1/fchg", SNAP1, "200"),
        ("timetables/v1/plan", SNAP1, "200"),                  # plan responses are ignored
        ("timetables/v1/fchg", SNAP1, "500"),                  # failed requests are ignored
        ("timetables/v1/fchg", "<timetable><s id='z'><m t='d' c='1'", "200"),   # truncated xml
    ])
    write_raw(tmp_path / "h2.parquet", [("timetables/v1/fchg", SNAP2, "200")])
    out = tmp_path / "day.parquet"
    n = er.extract_files([tmp_path / "h1.parquet", tmp_path / "h2.parquet"], out)
    df = pl.read_parquet(out).sort("id")
    assert n == 2 and df["id"].to_list() == ["r1-2510151030-4", "r2-2510151100-2"]
    assert df["reason_code"].to_list() == [57, 31]              # latest across snapshots, per stop
    assert df.schema == er.SCHEMA


def test_empty_day_still_writes_a_valid_file(tmp_path):
    write_raw(tmp_path / "h.parquet", [("timetables/v1/fchg", "<timetable/>", "200")])
    assert er.extract_files([tmp_path / "h.parquet"], tmp_path / "d.parquet") == 0
    assert pl.read_parquet(tmp_path / "d.parquet").schema == er.SCHEMA


def test_combine_keeps_newest_message_per_stop(tmp_path):
    for name, rows in (("a", [("s1", 43, "2510150900"), ("s2", 31, "2510150910")]),
                       ("b", [("s1", 57, "2510151000")])):
        pl.DataFrame({"id": [r[0] for r in rows], "reason_code": [r[1] for r in rows],
                      "reason_ts": [r[2] for r in rows]}, schema=er.SCHEMA).write_parquet(tmp_path / f"{name}.parquet")
    out = tmp_path / "all.parquet"
    assert er.combine([tmp_path / "a.parquet", tmp_path / "b.parquet"], out) == 2
    got = dict(pl.read_parquet(out).select("id", "reason_code").iter_rows())
    assert got == {"s1": 57, "s2": 31}


def test_output_plugs_into_build_db_file(tmp_path):
    """End to end: reasons file -> delay_store -> the cause shows up on the bar."""
    from delay_history import delay_store as ds
    con = duckdb.connect()
    con.execute("CREATE TABLE t(id VARCHAR, train_number VARCHAR, train_type VARCHAR, eva VARCHAR,"
                " station_name VARCHAR, arrival_planned_time TIMESTAMP, arrival_change_time TIMESTAMP,"
                " arrival_is_canceled BOOLEAN)")
    p = datetime(2025, 10, 15, 10, 30)
    con.execute("INSERT INTO t VALUES ('r1-2510151030-4','594','ICE','8000105','X Hbf',?,?,false)",
                [p, datetime(2025, 10, 15, 10, 58)])
    con.execute(f"COPY t TO '{tmp_path}/m.parquet' (FORMAT parquet)")
    write_raw(tmp_path / "h.parquet", [("timetables/v1/fchg", SNAP2, "200")])
    er.extract_files([tmp_path / "h.parquet"], tmp_path / "reasons.parquet")
    ds.build_db_file(tmp_path / "m.parquet", tmp_path / "x.duckdb", reasons_parquet=tmp_path / "reasons.parquet")
    s = ds.DelayStore(tmp_path / "x.duckdb")
    r = s.leg_stats("594", "8000105", datetime(2026, 1, 1, 10, 30), window=400)
    assert r["days"][0]["reason_code"] == 57 and r["days"][0]["delay"] == 28


def test_combine_creates_missing_output_folder(tmp_path):
    pl.DataFrame({"id": ["s1"], "reason_code": [43], "reason_ts": ["2510150900"]},
                 schema=er.SCHEMA).write_parquet(tmp_path / "a.parquet")
    out = tmp_path / "does" / "not" / "exist" / "all.parquet"      # was an IOException on Windows
    assert er.combine([tmp_path / "a.parquet"], out) == 1 and out.exists()
