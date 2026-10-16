from datetime import datetime, timedelta

import duckdb
import pytest

from delay_history import delay_store as ds
from journeys import assembler as ja

STUTTGART, NUERNBERG, FFM_FLUGHAFEN = "8000096", "8000284", "8070003"
D = "2026-01-05"      # the searched day (history comes from the last 7 days of data)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("j")
    rows = []

    def add(num, typ, eva, name, planned, delay, canceled=False):
        change = None if delay is None else planned + timedelta(minutes=delay)
        rows.append((num, typ, eva, name, planned, change, canceled))

    d1 = [17, 17, 4, 7, 17, 14, 85]          # ICE 594 at Nuernberg, Dec 25..31 -> median 17
    d2 = [10] * 7                            # ICE 104 at Frankfurt Flughafen -> median 10
    d3 = [0, 0, 1, 0, 2, 0, 0]               # RB 77 -> median 0
    for k in range(7):
        day = datetime(2025, 12, 25 + k)
        add("594", "ICE", NUERNBERG, "Nuernberg Hbf", day.replace(hour=17, minute=55), d1[k])
        add("104", "ICE", FFM_FLUGHAFEN, "Frankfurt(M) Flughafen Fernbf", day.replace(hour=18, minute=50), d2[k])
        add("77", "RB", NUERNBERG, "Nuernberg Hbf", day.replace(hour=9, minute=10), d3[k])
    con = duckdb.connect()
    con.execute("CREATE TABLE t(train_number VARCHAR, train_type VARCHAR, eva VARCHAR, station_name VARCHAR,"
                " arrival_planned_time TIMESTAMP, arrival_change_time TIMESTAMP, arrival_is_canceled BOOLEAN)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?)", rows)
    con.execute(f"COPY t TO '{tmp}/m.parquet' (FORMAT parquet)")
    ds.build_db_file(tmp / "m.parquet", tmp / "x.duckdb")
    return ds.DelayStore(tmp / "x.duckdb")


def leg(number, product, name, origin, dest, dep, arr, *, typ="PUBLICTRANSPORT",
        live_dep=None, live_arr=None, cancelled=False):
    return {
        "verkehrsmittel": {"typ": typ, "mittelText": name, "name": name, "nummer": number,
                           "produktGattung": product},
        "abfahrtsOrtExtId": origin[0], "abfahrtsOrt": origin[1],
        "ankunftsOrtExtId": dest[0], "ankunftsOrt": dest[1],
        "abfahrt": {"sollzeit": f"{D}T{dep}:00", "echtzeit": f"{D}T{live_dep}:00" if live_dep else None},
        "ankunft": {"sollzeit": f"{D}T{arr}:00", "echtzeit": f"{D}T{live_arr}:00" if live_arr else None},
        "destinationCancelled": cancelled,
    }


S, N, F = (STUTTGART, "Stuttgart Hbf"), (NUERNBERG, "Nuernberg Hbf"), (FFM_FLUGHAFEN, "Frankfurt(M) Flughafen Fernbf")
ICE594 = lambda **k: leg("594", "ICE", "ICE 594", S, N, "16:49", "17:55", **k)


def run(store, *legs, window=7, extra=None):
    v = {"verbindungsAbschnitte": list(legs), "umstiegsAnzahl": 1, **(extra or {})}
    return ja.assemble({"verbindungen": [v]}, store, window)["journeys"]


def test_badges_red_and_gray_and_runs(store):
    j = run(store, ICE594(), leg("104", "ICE", "ICE 104", N, F, "18:05", "18:50"))[0]
    a, b = j["legs"]
    assert a["badge"]["text"] == "+17 min" and a["badge"]["red"] is True
    assert (a["badge"]["runs"], a["badge"]["window"]) == (7, 7)          # "(7/7 days)"
    assert b["badge"]["text"] == "+10 min" and b["badge"]["red"] is True
    assert len(a["bars"]) == 7 and a["bars"][-1]["delay"] == 85
    assert j["delay_score"] == 10                                         # final leg's median
    rb = run(store, leg("77", "REGIONAL", "RB 77", S, N, "08:00", "09:10"))[0]["legs"][0]["badge"]
    assert rb["text"] == "+0 min" and rb["red"] is False                  # gray


def test_comfortable_transfer_uses_history_for_miss_probability(store):
    j = run(store, ICE594(), leg("104", "ICE", "ICE 104", N, F, "18:05", "18:50"))[0]
    t = j["transfers"][0]
    assert t["planned_minutes"] == 10 and not t["tight"] and not j["tight_transfer"]
    assert t["p_miss"] == 0.71                       # 5 of 7 days later than 10 min


def test_tight_by_plan(store):
    j = run(store, ICE594(), leg("104", "ICE", "ICE 104", N, F, "17:59", "18:50"))[0]
    assert j["transfers"][0]["planned_minutes"] == 4 and j["tight_transfer"]


def test_tight_after_real_time_delay(store):
    j = run(store, ICE594(live_arr="18:02"), leg("104", "ICE", "ICE 104", N, F, "18:05", "18:50"))[0]
    t = j["transfers"][0]
    assert t["planned_minutes"] == 10 and t["effective_minutes"] == 3     # 10 - 7
    assert t["tight"] and not t["likely_missed"] and t["p_miss"] is None  # live data wins


def test_likely_missed_when_delay_exceeds_transfer(store):
    j = run(store, ICE594(live_arr="18:20"), leg("104", "ICE", "ICE 104", N, F, "18:05", "18:50"))[0]
    assert j["transfers"][0]["likely_missed"] and j["tight_transfer"]


def test_next_trains_own_delay_gives_back_time(store):
    j = run(store, ICE594(live_arr="18:02"),
            leg("104", "ICE", "ICE 104", N, F, "18:05", "18:50", live_dep="18:12"))[0]
    assert j["transfers"][0]["effective_minutes"] == 10                   # 10 - 7 + 7


def test_transfer_time_includes_the_walk_but_risk_uses_what_is_left_after_it(store):
    walk = leg(None, None, "Walk", N, N, "17:55", "18:01", typ="WALK")
    j = run(store, ICE594(), walk, leg("104", "ICE", "ICE 104", N, F, "18:05", "18:50"))[0]
    t = j["transfers"][0]
    assert t["planned_minutes"] == 10 and t["walk_minutes"] == 6          # as bahn.de shows it: 10 min, 6 of them walking
    assert t["slack_minutes"] == 4 and t["tight"] and j["tight_transfer"]  # only 4 min are left once you have walked
    assert len(j["legs"]) == 2                                            # walking itself is not listed as a leg


def test_walk_that_fills_the_gap_is_tight_and_a_delay_breaks_it(store):
    walk = leg(None, None, "Walk", N, N, "17:55", "18:10", typ="WALK")
    nxt = lambda **k: leg("104", "ICE", "ICE 104", N, F, "18:11", "18:50", **k)
    t = run(store, ICE594(), walk, nxt())[0]["transfers"][0]
    assert (t["planned_minutes"], t["walk_minutes"], t["slack_minutes"]) == (16, 15, 1) and t["tight"]
    late = run(store, ICE594(live_arr="18:02"), walk, nxt())[0]["transfers"][0]    # first train 7 min late
    assert late["effective_minutes"] == 9 and late["likely_missed"]       # 9 min of gap left, 15 needed for the walk                                    # walking not listed


def test_unknown_train_is_still_listed_with_no_history(store):
    j = run(store, leg("999", "ICE", "ICE 999", S, N, "16:49", "17:55"))
    assert len(j) == 1 and j[0]["legs"][0]["badge"] == {"state": "no_history", "text": "No history"}
    assert j[0]["delay_score"] is None and j[0]["legs"][0]["bars"] == []


def test_bus_is_listed_untracked(store):
    j = run(store, leg("5", "BUS", "Bus 5", S, N, "16:49", "17:55"))
    assert j[0]["legs"][0]["badge"]["state"] == "no_history"


def test_upstream_cancelled_connection_is_dropped(store):
    resp = {"verbindungen": [
        {"verbindungsAbschnitte": [ICE594(cancelled=True)]},
        {"verbindungsAbschnitte": [ICE594()], "risNotizen": [{"key": "text.realtime.connection.cancelled"}]},
        {"verbindungsAbschnitte": [ICE594()]}],
        "verbindungReference": {"earlier": "E", "later": "L"}}
    out = ja.assemble(resp, store)
    assert len(out["journeys"]) == 1 and (out["earlier_ref"], out["later_ref"]) == ("E", "L")


def test_tolerance_depends_on_product(store):
    # RB 77 planned 09:10; a regional leg planned 3 h earlier must not borrow its history
    far = run(store, leg("77", "REGIONAL", "RB 77", S, N, "05:00", "06:10"))[0]["legs"][0]["badge"]
    near = run(store, leg("77", "REGIONAL", "RB 77", S, N, "08:00", "09:00"))[0]["legs"][0]["badge"]
    assert far["state"] == "no_history" and near["state"] == "ok"


def test_price_is_passed_through_when_present_and_none_otherwise(store):
    legs = [ICE594()]
    v_priced = {"verbindungsAbschnitte": legs, "angebotsPreis": {"betrag": 212.6, "waehrung": "EUR"}}
    v_plain = {"verbindungsAbschnitte": legs}
    v_junk = {"verbindungsAbschnitte": legs, "angebotsPreis": {"betrag": "n/a"}}
    out = ja.assemble({"verbindungen": [v_priced, v_plain, v_junk]}, store)["journeys"]
    assert out[0]["price"] == {"amount": 212.6, "currency": "EUR", "from": True}
    assert out[1]["price"] is None and out[2]["price"] is None


def test_dticket_marker_and_partial_price(store):
    legs = [ICE594()]
    covered = {"verbindungsAbschnitte": legs, "meldungenAsObject": [{"code": "MDA-NUR-DT"}]}
    partial = {"verbindungsAbschnitte": legs, "hasTeilpreis": True, "angebotsPreis": {"betrag": 30}}
    plain = {"verbindungsAbschnitte": legs, "angebotsPreis": {"betrag": 59.9}}
    out = ja.assemble({"verbindungen": [covered, partial, plain]}, store, dticket="all")["journeys"]
    assert out[0]["dticket_covered"] is True and out[0]["price"] is None
    assert out[1]["price_partial"] is True and out[1]["dticket_covered"] is False
    assert out[2]["price_partial"] is False and out[2]["price"]["amount"] == 59.9
    # partial pricing only makes sense when the ticket was declared for every connection
    off = ja.assemble({"verbindungen": [partial]}, store, dticket="off")["journeys"][0]
    assert off["price_partial"] is False


def test_journeys_come_out_in_departure_order(store):
    late = {"verbindungsAbschnitte": [leg("594", "ICE", "ICE 594", S, N, "18:30", "19:35")]}
    early = {"verbindungsAbschnitte": [leg("594", "ICE", "ICE 594", S, N, "09:10", "10:15")]}
    mid = {"verbindungsAbschnitte": [leg("594", "ICE", "ICE 594", S, N, "13:00", "14:05")]}
    out = ja.assemble({"verbindungen": [late, early, mid]}, store)["journeys"]
    assert [j["departure"][11:16] for j in out] == ["09:10", "13:00", "18:30"]
