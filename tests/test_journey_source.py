from datetime import datetime
from pathlib import Path

import pytest

from journeys import source as js

FIX = Path(__file__).parent.parent / "fixtures" / "journeys.json"
WHEN = datetime(2026, 10, 12, 16, 30)
A, B = "A=1@O=Stuttgart Hbf@L=8000096@", "A=1@O=Nürnberg Hbf@L=8000284@"


def test_search_body_matches_the_reference_apps_fields():
    b = js.build_search_body(A, B, WHEN)
    assert (b["abfahrtsHalt"], b["ankunftsHalt"]) == (A, B)
    assert b["anfrageZeitpunkt"] == "2026-10-12T16:30:00" and b["ankunftSuche"] == "ABFAHRT"
    assert b["klasse"] == "KLASSE_2" and b["produktgattungen"] == js.ALL_PRODUCTS
    assert b["reisende"][0]["typ"] == "ERWACHSENER" and b["reisende"][0]["anzahl"] == 1


def test_dticket_modes_set_the_two_flags_and_the_fast_connection_pruning():
    off, only, all_ = (js.build_search_body(A, B, WHEN, m) for m in ("off", "only", "all"))
    assert (off["deutschlandTicketVorhanden"], off["nurDeutschlandTicketVerbindungen"]) == (False, False)
    assert (only["deutschlandTicketVorhanden"], only["nurDeutschlandTicketVerbindungen"]) == (True, True)
    assert (all_["deutschlandTicketVorhanden"], all_["nurDeutschlandTicketVerbindungen"]) == (True, False)
    # the slower regional trains must stay in the list when the ticket is declared for every connection
    assert off["schnelleVerbindungen"] and only["schnelleVerbindungen"] and not all_["schnelleVerbindungen"]
    with pytest.raises(ValueError):
        js.build_search_body(A, B, WHEN, "maybe")


def test_fixture_source_imitates_bahn_de_per_mode():
    src = js.FixtureSource(FIX)
    off = src.fetch(A, B, WHEN, "off")["verbindungen"]
    only = src.fetch(A, B, WHEN, "only")["verbindungen"]
    every = src.fetch(A, B, WHEN, "all")["verbindungen"]
    assert len(off) == 3 and not any("meldungenAsObject" in v for v in off)      # no marker without a ticket
    assert all("angebotsPreis" in v for v in off)
    assert len(only) == 1 and "angebotsPreis" not in only[0]                      # covered: no price
    assert len(every) == 3 and sum("angebotsPreis" not in v for v in every) == 1
    assert off[0]["verbindungsAbschnitte"][0]["abfahrt"]["sollzeit"].startswith("2026-10-12")   # moved to the searched day


def test_departure_is_floored_to_five_minutes_and_paging_reference_is_passed():
    b = js.build_search_body(A, B, datetime(2026, 10, 12, 16, 33, 41), paging_ref="LATER-TOKEN")
    assert b["anfrageZeitpunkt"] == "2026-10-12T16:30:00" and b["pagingReference"] == "LATER-TOKEN"
    assert "pagingReference" not in js.build_search_body(A, B, WHEN)


# ------------------------------------------------------------------ BahnSource (no network: fake sessions)
class FakeResp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data if data is not None else {"verbindungen": []}, headers or {}

    def json(self):
        if self._data == "bad":
            raise ValueError("not json")
        return self._data


class FakeSession:
    def __init__(self, log, script):
        self.log, self.script = log, script

    def post(self, url, json=None):
        self.log.append((url, json))
        r = self.script.pop(0) if self.script else FakeResp()
        if isinstance(r, Exception):
            raise r
        return r


def make_bahn(script, **kw):
    """BahnSource whose sessions replay `script` (one entry per POST) and record every call."""
    log, clock = [], kw.pop("clock", None) or Clock()
    src = js.BahnSource(session_factory=lambda profile: FakeSession(log, script), clock=clock, **kw)
    return src, log, clock


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


BW = datetime(2026, 9, 20, 13, 7)
BA, BB = "A=1@O=Stuttgart Hbf@L=8000096@", "A=1@O=Frankfurt (Main) Hbf@L=8000105@"


def test_bahn_source_posts_the_search_body_and_returns_the_answer():
    src, log, _ = make_bahn([FakeResp(data={"verbindungen": [{"x": 1}]})])
    assert src.fetch(BA, BB, BW, "only") == {"verbindungen": [{"x": 1}]}
    url, body = log[0]
    assert url == js.BAHN_URL and body["abfahrtsHalt"] == BA and body["anfrageZeitpunkt"] == "2026-09-20T13:05:00"
    assert body["nurDeutschlandTicketVerbindungen"] is True


def test_bahn_source_caches_for_five_minutes_per_bucket():
    src, log, clock = make_bahn([FakeResp(), FakeResp()])
    src.fetch(BA, BB, BW)
    src.fetch(BA, BB, BW.replace(minute=8))              # same 5-minute bucket: no second call
    assert len(log) == 1
    clock.t += 301
    src.fetch(BA, BB, BW)                                # cache expired
    assert len(log) == 2


def test_a_403_moves_to_the_next_browser_profile_and_succeeds():
    src, log, _ = make_bahn([FakeResp(403), FakeResp(data={"verbindungen": [1]})])
    assert src.fetch(BA, BB, BW) == {"verbindungen": [1]}
    assert len(log) == 2 and src._profile == 1


def test_all_profiles_blocked_raises_then_pauses_briefly():
    src, log, clock = make_bahn([FakeResp(403)] * 3)
    with pytest.raises(js.JourneySourceError, match="blocked every"):
        src.fetch(BA, BB, BW)
    n = len(log)
    with pytest.raises(js.JourneySourceError, match="paused"):
        src.fetch(BA, BB, BW.replace(hour=15))           # during the pause nothing is sent
    assert len(log) == n
    clock.t += 31                                        # pause over: tries again
    src._profile = 0
    assert src.fetch(BA, BB, BW.replace(hour=16)) == {"verbindungen": []}


@pytest.mark.parametrize("resp,message", [
    (FakeResp(429, headers={"Retry-After": "12"}), "rate limit.*12"),
    (FakeResp(503), "503"),
    (FakeResp(404), "refused.*404"),
    (FakeResp(200, data="bad"), "unreadable"),
    (OSError("dns"), "unreachable.*OSError"),
])
def test_failures_become_journey_source_errors(resp, message):
    src, _, _ = make_bahn([resp])
    with pytest.raises(js.JourneySourceError, match=message):
        src.fetch(BA, BB, BW)


def test_a_saved_answer_is_served_when_bahn_fails_but_not_when_too_old():
    src, _, clock = make_bahn([FakeResp(data={"verbindungen": ["old"]}), FakeResp(503), FakeResp(503)])
    src.fetch(BA, BB, BW)
    clock.t += 600                                       # cache expired, stale still fine
    assert src.fetch(BA, BB, BW) == {"verbindungen": ["old"]}
    clock.t += 4000                                      # older than an hour
    with pytest.raises(js.JourneySourceError):
        src.fetch(BA, BB, BW)


def test_make_source_picks_the_fixture_or_the_real_source(monkeypatch):
    monkeypatch.setenv("JOURNEY_SOURCE", "fixture")
    assert isinstance(js.make_source(), js.FixtureSource)
    monkeypatch.delenv("JOURNEY_SOURCE")
    assert isinstance(js.make_source(), js.BahnSource)


def test_fixture_dates_after_the_placeholder_day_shift_too(tmp_path):
    (tmp_path / "f.json").write_text('{"verbindungen": [{"t": "2000-01-01T23:00:00", "u": "2000-01-02T06:00:00"}]}')
    out = js.FixtureSource(tmp_path / "f.json").fetch(BA, BB, datetime(2026, 12, 31, 12, 0))
    assert out["verbindungen"][0] == {"t": "2026-12-31T23:00:00", "u": "2027-01-01T06:00:00"}
