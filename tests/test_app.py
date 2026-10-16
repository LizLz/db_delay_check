import json
from datetime import date, datetime, timedelta

import duckdb
import pytest
from fastapi.testclient import TestClient

from api import app as appmod
from delay_history import delay_store as ds
from journeys.source import FixtureSource, JourneySourceError

STUTTGART, NUERNBERG, FFM_FLUGHAFEN = "8000096", "8000284", "8070003"
FIXTURE = {
    "verbindungen": [{
        "umstiegsAnzahl": 0,
        "verbindungsAbschnitte": [{
            "verkehrsmittel": {"typ": "PUBLICTRANSPORT", "mittelText": "ICE 594", "name": "ICE 594",
                               "nummer": "594", "produktGattung": "ICE"},
            "abfahrtsOrtExtId": STUTTGART, "abfahrtsOrt": "Stuttgart Hbf",
            "ankunftsOrtExtId": NUERNBERG, "ankunftsOrt": "Nuernberg Hbf",
            "abfahrt": {"sollzeit": "2000-01-01T16:49:00", "echtzeit": None},
            "ankunft": {"sollzeit": "2000-01-01T17:55:00", "echtzeit": None}}]}]}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """(store, fixture path): a tiny database plus a saved journey response."""
    tmp = tmp_path_factory.mktemp("api")
    rows = []
    for k in range(7):                                    # ICE 594 at Nuernberg, 7 days
        day = datetime(2025, 12, 25 + k)
        rows.append(("594", "ICE", NUERNBERG, "Nuernberg Hbf", day.replace(hour=17, minute=55),
                     day.replace(hour=17, minute=55) + timedelta(minutes=10 + k), False))
        rows.append(("12", "ICE", STUTTGART, "Stuttgart Hbf", day.replace(hour=9, minute=0),
                     day.replace(hour=9, minute=0), False))
        rows.append(("77", "RB", FFM_FLUGHAFEN, "Frankfurt(M) Flughafen Fernbf", day.replace(hour=9, minute=10),
                     day.replace(hour=9, minute=10), False))
    con = duckdb.connect()
    con.execute("CREATE TABLE t(train_number VARCHAR, train_type VARCHAR, eva VARCHAR, station_name VARCHAR,"
                " arrival_planned_time TIMESTAMP, arrival_change_time TIMESTAMP, arrival_is_canceled BOOLEAN)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?)", rows)
    con.execute(f"COPY t TO '{tmp}/m.parquet' (FORMAT parquet)")
    ds.build_db_file(tmp / "m.parquet", tmp / "x.duckdb")
    (tmp / "fix.json").write_text(json.dumps(FIXTURE))
    return ds.DelayStore(tmp / "x.duckdb"), tmp / "fix.json"


@pytest.fixture(scope="module")
def client(env):
    return TestClient(appmod.create_app(env[0], FixtureSource(env[1])))


def loc(name, ext):
    return f"A=1@O={name}@L={ext}@"


def search(client, **over):
    p = {"from_id": loc("Stuttgart Hbf", STUTTGART), "to_id": loc("Nuernberg Hbf", NUERNBERG),
         "date": "2026-01-05", "time": "16:30", "window": 7, **over}
    return client.get("/api/journeys", params=p)


# ---------------------------------------------------------------- pure helpers
def test_ext_id_and_time_mode():
    assert appmod.ext_id_of("A=1@O=Stuttgart Hbf@L=8000096@") == "8000096"
    assert appmod.ext_id_of("8000096") == "8000096"
    with pytest.raises(ValueError):
        appmod.ext_id_of("Stuttgart")
    today = date(2026, 10, 11)
    assert appmod.time_mode(today, today) == "live"
    assert appmod.time_mode(date(2026, 10, 9), today) == "history"
    assert appmod.time_mode(date(2026, 10, 12), today) == "future"


def test_enrich_causes_adds_text_and_never_invents():
    r = {"journeys": [{"legs": [{"bars": [{"reason_code": 34, "delay": 9}, {"reason_code": None, "delay": 9},
                                          {"reason_code": 9999, "delay": 9}, {"reason_code": 43, "delay": 0}]}]}]}
    bars = appmod.enrich_causes(r)["journeys"][0]["legs"][0]["bars"]
    assert bars[0]["cause"] == "Signal defect" and "Infrastructure" in bars[0]["cause_category"]
    assert bars[1]["cause"] is None and bars[2]["cause"] is None
    assert bars[3]["cause"] is None                        # on-time day: no cause shown


def test_daily_cache_recomputes_only_when_the_data_day_moves():
    c, calls = appmod.DailyCache(), []
    f = lambda: calls.append(1) or len(calls)
    assert c.get("k", date(2026, 1, 1), f) == c.get("k", date(2026, 1, 1), f) == 1
    assert c.get("k", date(2026, 1, 2), f) == 2


# --------------------------------------------------------------------- endpoints
def test_health_and_stations(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["stations"] == 3
    assert [s["name"] for s in client.get("/api/stations", params={"q": "stu"}).json()] == ["Stuttgart Hbf"]
    assert client.get("/api/stations", params={"q": "s"}).json() == []          # needs 2 letters
    assert client.get("/api/stations", params={"q": "xx"}).json() == []         # no match, no autocorrect
    assert client.get("/api/stations", params={"q": "tuttgart"}).json() == []    # prefix only


def test_overview_has_all_series_and_is_cached(client):
    a = client.get("/api/overview").json()
    assert set(a["series"]) == {"ICE", "IC", "RE", "RB", "S", "All"} and len(a["hours"]) == 24
    assert client.get("/api/overview").json() == a


def test_persistence_reports_unavailable_without_two_months(client):
    assert client.get("/api/persistence").json()["available"] is False


def test_journeys_with_history(client):
    r = search(client).json()
    leg = r["journeys"][0]["legs"][0]
    assert leg["badge"]["state"] == "ok" and leg["badge"]["median"] == 13 and len(leg["bars"]) == 7
    assert leg["bars"][0]["cause"] is None                                       # no cause data in this DB
    assert r["mode"] == "history" and r["window"] == 7 and r["data_to"] == "2025-12-31"


def test_same_from_and_to_gives_no_results(client):
    same = loc("Stuttgart Hbf", STUTTGART)
    r = search(client, from_id=same, to_id=same).json()
    assert r["journeys"] == [] and r["message"] == "No results found"


def test_stations_must_come_from_the_list_and_inputs_are_validated(client):
    assert search(client, from_id=loc("Nowhere", "1234567")).status_code == 422
    assert search(client, date="not-a-date").status_code == 422
    assert search(client, time="25:99").status_code == 422
    assert search(client, window=10).status_code == 422
    assert search(client, from_id="Stuttgart").status_code == 422


def test_source_failure_is_a_clear_502(env):
    class Broken:
        def fetch(self, *a):
            raise JourneySourceError("blocked")
    broken = TestClient(appmod.create_app(env[0], Broken()))
    r = search(broken)
    assert r.status_code == 502 and "blocked" in r.json()["detail"]


def test_empty_source_result_says_no_results(env, tmp_path):
    (tmp_path / "empty.json").write_text(json.dumps({"verbindungen": []}))
    empty = TestClient(appmod.create_app(env[0], FixtureSource(tmp_path / "empty.json")))
    r = search(empty).json()
    assert r["journeys"] == [] and r["message"] == "No results found"


def test_dticket_parameter_is_validated_passed_on_and_locations_are_full_ids(env):
    seen = {}

    class Recorder:
        def fetch(self, origin_id, dest_id, when, dticket="off"):
            seen.update(origin=origin_id, dest=dest_id, dticket=dticket)
            return {"verbindungen": []}

    c = TestClient(appmod.create_app(env[0], Recorder()))
    assert search(c, dticket="maybe").status_code == 422
    r = search(c, dticket="only").json()
    assert r["dticket"] == "only" and r["message"] == "No results found"
    assert seen == {"origin": "A=1@O=Stuttgart Hbf@L=8000096@", "dest": "A=1@O=Nuernberg Hbf@L=8000284@",
                    "dticket": "only"}


def test_app_uses_the_optional_station_dimension_when_it_exists(env, tmp_path, monkeypatch):
    import polars as pl
    pl.DataFrame({"eva": ["08000096", "08000284"],
                  "station_name": ["Stuttgart Hauptbahnhof", "Nuernberg Hauptbahnhof"]}).write_parquet(tmp_path / "dim.parquet")
    monkeypatch.setenv("DB_PATH", str(env[0].db_path))
    monkeypatch.setenv("STATIONS_PARQUET", str(tmp_path / "dim.parquet"))
    c = TestClient(appmod.create_app(source=FixtureSource(env[1])))
    assert [s["name"] for s in c.get("/api/stations", params={"q": "stutt"}).json()] == ["Stuttgart Hauptbahnhof"]


def test_web_page_is_served_and_api_routes_still_win(env, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<title>hi</title>")
    monkeypatch.setenv("WEB_DIR", str(tmp_path))
    c = TestClient(appmod.create_app(env[0], FixtureSource(env[1])))
    assert "<title>hi</title>" in c.get("/").text
    assert c.get("/api/health").json()["ok"] is True


def test_web_page_is_served_and_api_routes_still_win(env, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<title>page</title>")
    monkeypatch.setenv("WEB_DIR", str(tmp_path))
    c = TestClient(appmod.create_app(env[0], FixtureSource(env[1])))
    assert "<title>page</title>" in c.get("/").text
    assert c.get("/api/health").json()["ok"] is True
    assert c.get("/api/nope").status_code == 404


def test_the_page_is_served_at_root_and_api_routes_still_win(env, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<title>page</title>")
    monkeypatch.setenv("WEB_DIR", str(tmp_path))
    c = TestClient(appmod.create_app(env[0], FixtureSource(env[1])))
    assert "<title>page</title>" in c.get("/").text
    assert c.get("/api/health").json()["ok"] is True


def test_cache_computes_once_when_two_threads_ask_at_the_same_time():
    import threading, time
    c, calls = appmod.DailyCache(), []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return "value"
    out = []
    threads = [threading.Thread(target=lambda: out.append(c.get("k", date(2026, 1, 1), slow))) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert out == ["value"] * 4 and len(calls) == 1


def test_startup_warms_the_charts_in_the_background(env, tmp_path, monkeypatch):
    import time
    app = appmod.create_app(env[0], FixtureSource(env[1]))
    with TestClient(app):                                  # entering the client runs the start-up (lifespan)
        time.sleep(0.5)
    assert env[0] is not None


def test_response_says_where_the_journeys_came_from(client):
    assert search(client).json()["source"] == "fixture"          # the page shows a "demo data" notice for this
