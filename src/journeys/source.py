"""journey_source.py - where the journey list for a search comes from.

The API only needs one thing from a source: given an origin, a destination, a departure
time and the Deutschlandticket mode, return a bahn.de-shaped response (see
journey_assembler.py). Two sources:

  FixtureSource  reads a saved response (fixtures/journeys.json), moves it to the searched
                 date and imitates bahn.de's Deutschlandticket behaviour. Lets the whole app
                 run without any network call. Today's default.
  BahnSource     the real thing: POST https://www.bahn.de/web/api/angebote/fahrplan with the
                 body from build_search_body(). bahn.de sits behind bot protection that looks at
                 the TLS fingerprint, so the request goes through curl_cffi, which can look like
                 a real browser. Adds a 5-minute cache, a 30 s pause after a block, and a stale
                 fallback (a saved answer up to 1 h old) when bahn.de refuses.

Swapping sources changes nothing else in the app.

dticket modes (the same three the reference app uses):
  "off"   search without the ticket: every connection, full prices
  "only"  only connections valid with the Deutschlandticket
  "all"   every connection, but declare the ticket: fully covered connections come back flagged
          (message code MDA-NUR-DT) and without a price, mixed ones priced for the paid legs only
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)
PLACEHOLDER_DAY = "2000-01-01"          # the date inside the fixture file
DT_MODES = ("off", "only", "all")
ALL_PRODUCTS = ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"]


class JourneySourceError(RuntimeError):
    """The source could not deliver journeys (network down, blocked, bad response)."""


class JourneySource(Protocol):
    def fetch(self, origin_id: str, dest_id: str, when: datetime, dticket: str = "off") -> dict: ...


def build_search_body(origin_id: str, dest_id: str, when: datetime, dticket: str = "off",
                      paging_ref: str | None = None) -> dict:
    """The JSON body bahn.de's journey search expects (field names as the reference app sends
    them). origin_id / dest_id are full location ids: 'A=1@O=Name@L=8000096@'.
    One adult, second class, no discount: what bahn.de prices for when nothing is picked.

    The departure minute is floored to a 5-minute bucket, so two searches a minute apart share
    one cache entry; results can start up to 4 minutes before the time asked for.
    paging_ref: a verbindungReference.earlier/later token from a previous response (next page)."""
    if dticket not in DT_MODES:
        raise ValueError(f"dticket must be one of {DT_MODES}")
    when = when.replace(minute=when.minute - when.minute % 5, second=0, microsecond=0)
    body = {
        "abfahrtsHalt": origin_id,
        "ankunftsHalt": dest_id,
        "anfrageZeitpunkt": when.strftime("%Y-%m-%dT%H:%M:00"),
        "ankunftSuche": "ABFAHRT",                       # `when` is a departure time
        "klasse": "KLASSE_2",
        "produktgattungen": list(ALL_PRODUCTS),
        "reisende": [{"typ": "ERWACHSENER",
                      "ermaessigungen": [{"art": "KEINE_ERMAESSIGUNG", "klasse": "KLASSENLOS"}],
                      "alter": [], "anzahl": 1}],
        # "prefer fast connections" prunes the slower regional trains that the ticket covers; it
        # must be off when the ticket is declared for every connection ("all")
        "schnelleVerbindungen": dticket != "all",
        "sitzplatzOnly": False,
        "bikeCarriage": False,
        "reservierungsKontingenteVorhanden": False,
        "deutschlandTicketVorhanden": dticket != "off",
        "nurDeutschlandTicketVerbindungen": dticket == "only",
    }
    if paging_ref:
        body["pagingReference"] = paging_ref
    return body


def _covered(v: dict) -> bool:
    return any(m.get("code") == "MDA-NUR-DT" for m in v.get("meldungenAsObject") or [])


class FixtureSource:
    name = "fixture"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self, origin_id: str, dest_id: str, when: datetime, dticket: str = "off") -> dict:
        if dticket not in DT_MODES:
            raise ValueError(f"dticket must be one of {DT_MODES}")
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise JourneySourceError(f"fixture not readable: {e}") from e
        # the fixture's times carry a placeholder day; move them to the day that was searched
        # (2000-01-01 -> the searched day, 2000-01-02 -> the day after, so overnight trips work too)
        data = json.loads(re.sub(r"2000-01-(\d\d)", lambda m: (when.date() + timedelta(days=int(m.group(1)) - 1)).isoformat(), text))
        # imitate bahn.de: the covered-marker is only sent when a ticket was declared, and covered
        # connections then have no price; "only" keeps just the covered ones
        out = []
        for v in data.get("verbindungen", []):
            covered = _covered(v)
            if dticket == "off":
                v = {k: val for k, val in v.items() if k != "meldungenAsObject"}
            elif covered:
                v = {k: val for k, val in v.items() if k != "angebotsPreis"}
            if dticket == "only" and not covered:
                continue
            out.append(v)
        return {**data, "verbindungen": out}



# ------------------------------------------------------------------ the real source
BAHN_URL = "https://www.bahn.de/web/api/angebote/fahrplan"
# Akamai (bahn.de's bot protection) blocks one browser fingerprint at a time, so keep spares and
# move to the next one when a profile gets a 403. Same list and order as the reference app.
PROFILES = ("firefox135", "safari17_0", "chrome")
HEADERS = {
    "Accept": "application/json",
    # without this header Akamai answers /angebote/fahrplan with 403 (seen in the reference app)
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def _curl_session(profile: str):
    try:
        from curl_cffi import requests
    except ImportError as e:                                # pragma: no cover
        raise JourneySourceError("curl_cffi is not installed: run  uv add curl_cffi") from e
    return requests.Session(impersonate=profile, timeout=20, headers=HEADERS)


class BahnSource:
    """Journeys straight from bahn.de.

    session_factory(profile) -> an object with .post(url, json=...) returning something with
    .status_code, .json() and .headers. Tests pass a fake; the default is a curl_cffi session.
    Every failure becomes a JourneySourceError, which the API turns into a clear 502."""
    name = "bahn"


    def __init__(self, ttl: int = 300, stale_ttl: int = 3600, cache_max: int = 256, block_cooldown: int = 30,
                 max_concurrency: int = 4, session_factory: Callable = _curl_session, clock: Callable = time.monotonic):
        self.ttl, self.stale_ttl, self.cache_max, self.block_cooldown = ttl, stale_ttl, cache_max, block_cooldown
        self._factory, self._clock = session_factory, clock
        self._cache: OrderedDict = OrderedDict()            # key -> (saved_at, response)
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(max_concurrency)  # never more than a few calls at once
        self._local = threading.local()                     # a session is not shared between threads
        self._profile = 0                                   # index into PROFILES, shared: a block moves everyone
        self._paused_until = 0.0

    # ---- public
    def fetch(self, origin_id: str, dest_id: str, when: datetime, dticket: str = "off") -> dict:
        body = build_search_body(origin_id, dest_id, when, dticket)
        key = (origin_id, dest_id, body["anfrageZeitpunkt"], dticket)
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
        if hit and now - hit[0] <= self.ttl:
            return hit[1]
        try:
            data = self._post(body)
        except JourneySourceError:
            if hit and now - hit[0] <= self.stale_ttl:      # an older answer beats an error page
                log.warning("bahn.de unavailable: serving a %ds-old answer", now - hit[0])
                return hit[1]
            raise
        with self._lock:
            self._cache[key] = (self._clock(), data)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max:
                self._cache.popitem(last=False)
        return data

    # ---- one upstream call
    def _session(self, profile: str):
        sessions = getattr(self._local, "sessions", None)
        if sessions is None:
            sessions = self._local.sessions = {}
        if profile not in sessions:
            sessions[profile] = self._factory(profile)
        return sessions[profile]

    def _post(self, body: dict) -> dict:
        if self._clock() < self._paused_until:
            raise JourneySourceError("bahn.de blocked us a moment ago; paused briefly, try again in a few seconds")
        with self._slots:
            for _ in PROFILES:
                profile = PROFILES[self._profile % len(PROFILES)]
                try:
                    resp = self._session(profile).post(BAHN_URL, json=body)
                except JourneySourceError:
                    raise
                except Exception as e:                      # DNS, connect, timeout ...
                    raise JourneySourceError(f"bahn.de unreachable ({type(e).__name__})") from e
                status = resp.status_code
                if status == 403:                           # this fingerprint is blocked: next profile, fresh session
                    log.warning("bahn.de 403 with profile %s: switching", profile)
                    with self._lock:
                        self._profile += 1
                    getattr(self._local, "sessions", {}).pop(profile, None)
                    continue
                if status == 429:
                    wait = resp.headers.get("Retry-After", "") if getattr(resp, "headers", None) else ""
                    raise JourneySourceError(f"bahn.de rate limit reached" + (f", retry in {wait} s" if wait.isdigit() else ""))
                if status >= 500:
                    raise JourneySourceError(f"bahn.de answered {status}")
                if status >= 400:
                    raise JourneySourceError(f"bahn.de refused the search ({status})")
                try:
                    return resp.json()
                except Exception as e:
                    raise JourneySourceError("bahn.de returned an unreadable answer") from e
            self._paused_until = self._clock() + self.block_cooldown
            raise JourneySourceError("bahn.de blocked every browser profile")


def make_source() -> JourneySource:
    """JOURNEY_SOURCE=bahn (default) or fixture. The fixture is a saved demo answer, not real data."""
    if os.environ.get("JOURNEY_SOURCE", "bahn").lower() == "fixture":
        return FixtureSource(os.environ.get("JOURNEY_FIXTURE", "fixtures/journeys.json"))
    return BahnSource()
