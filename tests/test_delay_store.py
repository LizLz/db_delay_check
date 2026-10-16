import random
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from delay_history import delay_store as ds

EVA = "08000105"          # Frankfurt Hbf
EVA_TIEF = "08098105"     # second level of the same station


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("d")
    random.seed(1)
    rows = []   # train_number, train_type, eva, station_name, planned, change, canceled, reason

    def add(num, typ, eva, name, planned, delay, canceled=False, reason=None):
        change = None if delay is None else planned + timedelta(minutes=delay)
        rows.append((num, typ, eva, name, planned, change, canceled, reason))

    last_day = datetime(2025, 12, 31)
    # ICE 0594: every day Oct..Dec at 17:30; delays in a fixed pattern; Dec 20+ cancelled often
    for i in range(0, 92):
        day = datetime(2025, 10, 1) + timedelta(days=i)
        planned = day.replace(hour=17, minute=30)
        if i == 85:
            add("0594", "ICE", EVA, "Frankfurt(Main)Hbf", planned, None, True)   # cancelled
        elif i == 86:
            continue                                                             # no service
        else:
            d = [None, 3, -4, 17, 9, 30, 0][i % 7]     # None = no change msg (on time), -4 = early
            add("0594", "ICE", EVA, "Frankfurt(Main)Hbf", planned, d, False, 31 if d == 30 else None)
        # same number, different hour: must NOT match the 17:30 leg
        add("0594", "ICE", EVA, "Frankfurt(Main)Hbf", day.replace(hour=6, minute=0), 50)
    # RE 4711: cancelled 4 of the last 5 days -> High cancellation, 1 run
    for i in range(5):
        day = last_day - timedelta(days=4 - i)
        add("4711", "RE", "08000191", "Mainz Hbf", day.replace(hour=8, minute=0),
            12 if not i % 2 else None, canceled=(i in (0, 1, 2, 4)))
    # RB 9: only one non-cancelled run -> not enough runs
    add("9", "RB", "08000191", "Mainz Hbf", last_day.replace(hour=9), 5)
    # persistence: 60 trains, Nov delay x, Dec delay ~ 0.6x + noise
    for t in range(60):
        base = random.choice([0, 1, 2, 4, 8, 12, 20])
        for m, scale in ((11, 1.0), (12, 0.6)):
            for k in range(12):
                day = datetime(2025, m, 1 + k)
                dly = max(0, int(base * scale + random.randint(-1, 1)))
                add(str(1000 + t), "ICE", "08000096", "Stuttgart Hbf",
                    day.replace(hour=10, minute=t % 60), dly)
    # a train sharing the station via the second EVA level
    for i in range(10):
        day = last_day - timedelta(days=i)
        add("77", "IC", EVA_TIEF, "Frankfurt(Main)Hbf", day.replace(hour=12), 5)

    con = duckdb.connect()
    con.execute("""CREATE TABLE t(train_number VARCHAR, train_type VARCHAR, eva VARCHAR,
        station_name VARCHAR, arrival_planned_time TIMESTAMP, arrival_change_time TIMESTAMP,
        is_canceled BOOLEAN, reason_code INTEGER)""")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?,?)", rows)
    pq = tmp / "archive.parquet"
    con.execute(f"COPY t TO '{pq}' (FORMAT parquet)")
    db = tmp / "delays.duckdb"
    ds.build_db_file(pq, db)
    return ds.DelayStore(db, min_arrivals=1)


def test_leg_median_clamps_and_counts_on_time(store):
    # window = last 7 days = Dec 25..31 -> i = 85 (cancelled), 86 (no service), 87..91
    r = store.leg_stats("594", EVA, datetime(2026, 1, 5, 17, 30), "ICE", window=7)
    assert r["median_delay"] is not None
    assert r["days_matched"] == 6                     # no-service day absent
    assert r["cancelled_days"] == 1
    assert len(r["days"]) == 6
    assert not any(d["day"] == "2025-12-26" for d in r["days"])
    cancelled = [d for d in r["days"] if d["cancelled"]]
    assert cancelled and cancelled[0]["delay"] is None
    assert all(d["delay"] >= 0 for d in r["days"] if not d["cancelled"])   # early -> 0


def test_other_hour_does_not_match(store):
    r = store.leg_stats("594", EVA, datetime(2026, 1, 5, 17, 30), "ICE", window=30)
    assert max(d["delay"] or 0 for d in r["days"]) <= 30          # the 50-min 06:00 run is out


def test_reason_code_surfaces(store):
    r = store.leg_stats("0594", EVA, datetime(2026, 1, 5, 17, 30), "ICE", window=30)
    assert any(d["reason_code"] == 31 for d in r["days"])


def test_high_cancellation_with_one_run_left(store):
    r = store.leg_stats("4711", "08000191", datetime(2026, 1, 5, 8, 0), "RE", window=7)
    assert "High cancellation" in r["badges"]
    assert r["median_delay"] == 0             # the single (on-time) run is still shown on the card
    assert len(r["days"]) == 5                # bars still available


def test_single_run_still_gets_a_bar(store):
    r = store.leg_stats("9", "08000191", datetime(2026, 1, 5, 9, 0), "RB", window=7)
    assert r["median_delay"] == 5 and r["badges"] == [] and len(r["days"]) == 1


def test_unknown_train_returns_none(store):
    assert store.leg_stats("99999", EVA, datetime(2026, 1, 5, 9, 0), "ICE") is None


def test_second_eva_level_is_same_station(store):
    r = store.leg_stats("77", EVA, datetime(2026, 1, 5, 12, 0), "IC", window=10)
    assert r is not None and r["days_matched"] == 10          # asked with the Hbf EVA


def test_hourly_profile_gaps_are_none_not_zero(store):
    p = store.hourly_profile(days=92)
    assert len(p["series"]["ICE"]) == 24
    assert p["series"]["ICE"][3] is None                       # no data at 03:00
    assert p["series"]["All"][17]["n"] >= ds.MIN_N_PER_HOUR_POINT


def test_persistence(store):
    p = store.persistence(min_runs=10)
    assert p["x_month"] == "2025-11" and p["y_month"] == "2025-12"
    assert p["n_trains"] >= 50
    assert 0.3 < p["slope"] < 0.9 and p["pearson_r"] > 0.8
    assert p["repeat_rate"] is not None and 0 <= p["repeat_rate"] <= 1
    assert len(p["points"]) <= 3000


def test_station_search(store):
    names = lambda q: [s["name"] for s in store.station_search(q)]
    assert names("fr") == ["Frankfurt(Main)Hbf"]
    assert names("frankfurt h") == ["Frankfurt(Main)Hbf"]
    assert names("f") == [] and names("zz") == []
    assert names("stu") == ["Stuttgart Hbf"]
    entry = store.station_search("fr")[0]
    assert set(entry["evas"]) == {EVA, EVA_TIEF}              # levels collapsed into one entry


def test_connection_status():
    assert ds.connection_status(8, d1_live=10)["status"] == "likely_missed"
    assert ds.connection_status(8, d1_live=5)["status"] == "tight"       # 3 min left
    assert ds.connection_status(8, d1_live=0)["status"] == "ok"
    h = [18, 17, 4, 7, 17, 14, 85]
    assert ds.connection_status(8, history=h)["p_miss"] == 0.71
    assert ds.connection_status(3, history=[1])["status"] == "tight"


def test_real_piebro_schema_and_glob(tmp_path):
    """Exact monthly_processed_data schema, two monthly files read through a glob."""
    con = duckdb.connect()
    con.execute("""CREATE TABLE t(station_name VARCHAR, xml_station_name VARCHAR, eva VARCHAR,
        train_number VARCHAR, line_number VARCHAR, final_destination_station VARCHAR,
        delay_in_min INTEGER, "time" TIMESTAMP, arrival_is_canceled BOOLEAN,
        departure_is_canceled BOOLEAN, train_type VARCHAR, train_line_ride_id VARCHAR,
        train_line_station_num INTEGER, arrival_planned_time TIMESTAMP,
        arrival_change_time TIMESTAMP, departure_planned_time TIMESTAMP,
        departure_change_time TIMESTAMP, id VARCHAR)""")
    k = 0
    for m in (10, 11):
        for day in range(1, 8):
            p = datetime(2025, m, day, 9, 0)
            # origin stop: no arrival at all (must be ignored)
            con.execute("INSERT INTO t VALUES ('Bonn Hbf','Bonn Hbf','8000044','123',NULL,'Koeln',"
                        "0,?,NULL,false,'ICE','r',1,NULL,NULL,?,NULL,?)", [p, p, f"o{k}"]); k += 1
            # destination stop: 8 min late; DEPARTURE cancelled flag must not matter
            con.execute("INSERT INTO t VALUES ('Koeln Hbf','Koeln Hbf','8000207','123',NULL,'Koeln',"
                        "8,?,false,true,'ICE','r',2,?,?,NULL,NULL,?)",
                        [p + timedelta(minutes=68), p + timedelta(minutes=60),
                         p + timedelta(minutes=68), f"d{k}"]); k += 1
        mdir = tmp_path / "monthly"
        mdir.mkdir(exist_ok=True)
        con.execute(f"COPY t TO '{mdir}/data-2025-{m}.parquet' (FORMAT parquet)")
        con.execute("DELETE FROM t")
    db = tmp_path / "x.duckdb"
    ds.build_db_file(Path(tmp_path / "monthly" / "data-2025-1*.parquet"), db)
    s = ds.DelayStore(db, min_arrivals=1)
    r = s.leg_stats("123", "8000207", datetime(2026, 1, 1, 10, 0), "ICE", window=60)
    assert r["days_matched"] == 14 and r["cancelled_days"] == 0      # origin rows ignored
    assert r["median_delay"] == 8                                     # departure flag ignored
    assert s.station_search("ko")[0]["evas"] == ["08000207"]          # eva zero-padded


def test_min_arrivals_polling_list_and_bus_exclusion(store, tmp_path):
    # dropdown threshold: Mainz has only 6 arrivals, Stuttgart 1440
    big = ds.DelayStore(store.db_path, min_arrivals=100)
    assert big.station_search("ma") == [] and big.station_search("stu")
    top = store.polling_stations(top=2)
    assert top[0]["name"] == "Stuttgart Hbf" and len(top) == 2

    con = duckdb.connect()
    con.execute("CREATE TABLE t(train_number VARCHAR, train_type VARCHAR, eva VARCHAR, "
                "station_name VARCHAR, arrival_planned_time TIMESTAMP, "
                "arrival_change_time TIMESTAMP, is_canceled BOOLEAN)")
    for typ in ("ICE", "Bus", "Bsv"):
        con.execute("INSERT INTO t VALUES ('1', ?, '8000105', 'X Hbf', "
                    "TIMESTAMP '2025-11-01 10:00', NULL, false)", [typ])
    con.execute(f"COPY t TO '{tmp_path}/a.parquet' (FORMAT parquet)")
    ds.build_db_file(tmp_path / "a.parquet", tmp_path / "b.duckdb")
    n = ds.DelayStore(tmp_path / "b.duckdb", min_arrivals=1).con.execute(
        "SELECT count(*) FROM delays").fetchone()[0]
    assert n == 1                                    # buses dropped


def test_reasons_join_by_stop_id(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE t(id VARCHAR, train_number VARCHAR, train_type VARCHAR, eva VARCHAR, "
                "station_name VARCHAR, arrival_planned_time TIMESTAMP, "
                "arrival_change_time TIMESTAMP, arrival_is_canceled BOOLEAN)")
    for d in range(1, 6):
        p = datetime(2025, 11, d, 10, 0)
        con.execute("INSERT INTO t VALUES (?, '5', 'ICE', '8000105', 'X Hbf', ?, ?, false)",
                    [f"ride{d}-2511{d:02d}1000-3", p, p + timedelta(minutes=20)])
    con.execute(f"COPY t TO '{tmp_path}/m.parquet' (FORMAT parquet)")
    con.execute("CREATE TABLE r(id VARCHAR, reason_code INTEGER, reason_ts TIMESTAMP)")
    con.execute("INSERT INTO r VALUES ('ride2-2511021000-3', 31, TIMESTAMP '2025-11-02 10:05')")
    con.execute(f"COPY r TO '{tmp_path}/r.parquet' (FORMAT parquet)")
    ds.build_db_file(tmp_path / "m.parquet", tmp_path / "x.duckdb",
                     reasons_parquet=tmp_path / "r.parquet")
    s = ds.DelayStore(tmp_path / "x.duckdb", min_arrivals=1)
    days = s.leg_stats("5", "8000105", datetime(2026, 1, 1, 10, 0), "ICE", window=60)["days"]
    got = {d["day"]: d["reason_code"] for d in days}
    assert got["2025-11-02"] == 31 and got["2025-11-03"] is None


def test_all_cancelled_still_returns_a_result(store):
    # a leg whose every matched day is cancelled: no median, but the card is still built
    r = store.leg_stats("4711", "08000191", datetime(2026, 1, 5, 8, 0), "RE", window=7)
    assert r is not None and "High cancellation" in r["badges"]


def test_station_ids_for_bahn_search(store):
    e = store.station_search("fr")[0]
    assert e["extId"] == "8000105" or e["extId"] == EVA.lstrip("0")
    assert e["id"] == f"A=1@O={e['name']}@L={e['extId']}@"


def test_tolerance_override(store):
    # the 06:00 ICE 594 run (50 min late) is 690 min away from 17:30: out at any tolerance
    r = store.leg_stats("594", EVA, datetime(2026, 1, 5, 17, 30), None, window=30, tolerance_min=30)
    assert r is not None and max(d["delay"] or 0 for d in r["days"]) <= 30


def test_sbahn_keyed_by_line_label(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE t(train_number VARCHAR, line_number VARCHAR, train_type VARCHAR, "
                "eva VARCHAR, station_name VARCHAR, arrival_planned_time TIMESTAMP, "
                "arrival_change_time TIMESTAMP, arrival_is_canceled BOOLEAN)")
    for d in range(1, 4):
        p = datetime(2025, 11, d, 8, 0)
        con.execute("INSERT INTO t VALUES ('28123','S 5','S','8000105','X Hbf',?,?,false)", [p, p])
        con.execute("INSERT INTO t VALUES ('28999','6','S','8000105','X Hbf',?,?,false)",
                    [p.replace(hour=9), p.replace(hour=9)])
        con.execute("INSERT INTO t VALUES ('00123',NULL,'ICE','8000105','X Hbf',?,?,false)",
                    [p.replace(hour=10), p.replace(hour=10)])
    con.execute(f"COPY t TO '{tmp_path}/m.parquet' (FORMAT parquet)")
    ds.build_db_file(tmp_path / "m.parquet", tmp_path / "x.duckdb")
    s = ds.DelayStore(tmp_path / "x.duckdb")
    assert s.leg_stats("S5", "8000105", datetime(2026, 1, 1, 8, 0), window=60)["days_matched"] == 3
    assert s.leg_stats("S6", "8000105", datetime(2026, 1, 1, 9, 0), window=60)["days_matched"] == 3
    assert s.leg_stats("123", "8000105", datetime(2026, 1, 1, 10, 0), window=60)["days_matched"] == 3


def test_cli_build_and_smoke(store, tmp_path, capsys):
    pq = tmp_path / "m.parquet"
    duckdb.connect().execute(
        "COPY (SELECT '594' AS train_number, 'ICE' AS train_type, '8000105' AS eva, 'Frankfurt Hbf' AS station_name,"
        " TIMESTAMP '2025-11-03 17:30:00' + INTERVAL (i) DAY AS arrival_planned_time,"
        " TIMESTAMP '2025-11-03 17:42:00' + INTERVAL (i) DAY AS arrival_change_time,"
        " false AS arrival_is_canceled FROM range(10) t(i)) "
        f"TO '{pq}' (FORMAT parquet)")
    db = tmp_path / "x.duckdb"
    assert ds._main(["build", str(pq), str(db)]) == 0
    assert "10 arrivals" in capsys.readouterr().out
    assert ds._main(["smoke", str(db), "--search", "fr", "--train", "594", "--eva", "8000105",
                     "--time", "17:30"]) == 0
    out = capsys.readouterr().out
    assert "Frankfurt Hbf" in out and "median +12 min" in out

def test_station_id_prefers_the_main_station_over_the_underground_level(tmp_path):
    import duckdb
    con = duckdb.connect()
    con.execute("CREATE TABLE t(train_number VARCHAR, train_type VARCHAR, eva VARCHAR, station_name VARCHAR,"
                " arrival_planned_time TIMESTAMP, arrival_change_time TIMESTAMP, arrival_is_canceled BOOLEAN)")
    rows = [("1", "S", "08098096", "Stuttgart Hbf", datetime(2026, 1, 1, 8, i), datetime(2026, 1, 1, 8, i), False)
            for i in range(5)]                          # the underground level has MORE arrivals
    rows += [("2", "ICE", "08000096", "Stuttgart Hbf", datetime(2026, 1, 1, 9, 0), datetime(2026, 1, 1, 9, 0), False)]
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?)", rows)
    con.execute(f"COPY t TO '{tmp_path}/m.parquet' (FORMAT parquet)")
    ds.build_db_file(tmp_path / "m.parquet", tmp_path / "x.duckdb")
    hit = ds.DelayStore(tmp_path / "x.duckdb").station_search("stutt")[0]
    assert hit["extId"] == "8000096" and set(hit["evas"]) == {"08098096", "08000096"}
