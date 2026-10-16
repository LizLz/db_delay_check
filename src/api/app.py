"""app.py - the web API. Run:  uv run uvicorn api.app:app --reload

Endpoints (all JSON):
  GET /api/health
  GET /api/stations?q=fr                      dropdown: prefix match, [] under 2 letters
  GET /api/overview?days=30                   chart 1: median arrival delay by hour per train type
  GET /api/persistence                        chart 2: last month vs the month before + linear fit
  GET /api/journeys?from_id=..&to_id=..&date=YYYY-MM-DD&time=HH:MM&window=7|15|30&dticket=off|only|all

Settings come from environment variables (or a .env loaded by your shell):
  DB_PATH            default data/delays.duckdb
  JOURNEY_SOURCE     bahn (default, real bahn.de search) or fixture (saved demo answer)
  JOURNEY_FIXTURE    default fixtures/journeys.json   (used when JOURNEY_SOURCE=fixture)
  WEB_DIR            default web                       (the page; served at / when the folder exists)
  WEB_DIR            default web                      (the page; served at /)
  STATIONS_PARQUET   default data/processed/dim_stations.parquet   (optional; used when it exists)
  WEB_DIR            default web   (the page; served at / when the folder exists)

The database is opened once at start-up, read-only. Every request uses its own cursor.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.staticfiles import StaticFiles
from fastapi.staticfiles import StaticFiles

from delay_history import cause_codes
from delay_history import delay_store as ds
from journeys import assembler as ja
from journeys.source import DT_MODES, JourneySource, JourneySourceError, make_source

from api import report as rp
from api import db_watch

BERLIN = ZoneInfo("Europe/Berlin")
WINDOWS = (7, 15, 30)
LOCATION_ID = re.compile(r"L=(\d+)@")


# ------------------------------------------------------------------ small pure helpers
def ext_id_of(location_id: str) -> str:
    """'A=1@O=Stuttgart Hbf@L=8000096@' -> '8000096'. A bare number is accepted too."""
    m = LOCATION_ID.search(location_id)
    if m:
        return m.group(1)
    if location_id.isdigit():
        return location_id
    raise ValueError("not a station id")


def time_mode(day: date, today: date) -> str:
    """live = today (real-time data exists), history = a past day, future = a coming day."""
    return "live" if day == today else "history" if day < today else "future"


def enrich_causes(result: dict) -> dict:
    """Add readable cause text + category to every per-day bar that has a cause code.
    Unknown codes get no text (never invented)."""
    for journey in result.get("journeys", []):
        for leg in journey["legs"]:
            for bar in leg["bars"]:
                code = bar.get("reason_code")
                if not bar.get("cancelled") and not bar.get("delay"):
                    code = None            # an on-time (or early) day has nothing to explain
                bar["cause"] = cause_codes.cause_text(code)
                cat = cause_codes.cause_category(code)
                bar["cause_category"] = cause_codes.CATEGORY_LABELS.get(cat) if cat else None
    return result


class DailyCache:
    """Charts change once a day (when new data lands): compute once, reuse until the data day moves.
    One computation per key at a time: a request that arrives while the start-up warm-up is still
    computing the same chart waits for it instead of starting a second, identical query."""
    def __init__(self):
        self._data: dict = {}
        self._key_locks: dict = {}
        self._lock = threading.Lock()

    def _hit(self, key, data_day):
        hit = self._data.get(key)
        return hit if hit and hit[0] == data_day else None

    def get(self, key, data_day, compute):
        with self._lock:
            hit = self._hit(key, data_day)
            if hit:
                return hit[1]
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        with key_lock:                          # compute outside self._lock: a slow query must not block other keys
            with self._lock:
                hit = self._hit(key, data_day)
            if hit:
                return hit[1]
            value = compute()
            with self._lock:
                self._data[key] = (data_day, value)
            return value


# ------------------------------------------------------------------------- the app
def create_app(store: ds.DelayStore | None = None, source: JourneySource | None = None) -> FastAPI:
    if store is None:
        # optional canonical station names (+ coordinates for the weather later), built by
        # scripts/build_station_dimension.py; without the file the names come from the delay data
        dim = Path(os.environ.get("STATIONS_PARQUET", "data/processed/dim_stations.parquet"))
        store = ds.DelayStore(Path(os.environ.get("DB_PATH", "data/delays.duckdb")),
                              dim_stations_parquet=dim if dim.exists() else None)
    source = source or make_source()
    cache = DailyCache()
    rp.load_dotenv()
    reports = rp.ReportService(cache_path=Path(os.environ.get("REPORT_CACHE", "data/report_cache.json")))
    known_ext_ids = {e.lstrip("0") for s in store.stations for e in s["evas"]}
    name_of = {e.lstrip("0"): s["name"] for s in store.stations for e in s["evas"]}

    def location_id(ext: str) -> str:
        """The full location id bahn.de's journey search wants, built from our station list."""
        return f"A=1@O={name_of[ext]}@L={ext}@"

    def warm_up() -> None:
        """Compute the home-page charts once at start-up (they take seconds on a full year of data),
        so the first visitor gets them from the cache instead of waiting."""
        try:
            overview(days=30)
            persistence()
            report()
        except Exception:                       # never block start-up; the first request will show the error
            logging.getLogger(__name__).exception("warm-up failed")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if os.environ.get("WARM_UP", "1") != "0":
            threading.Thread(target=warm_up, name="warm-up", daemon=True).start()
        if os.environ.get("RESTART_ON_NEW_DB") == "1":          # Docker: pick up the daily update by restarting
            db_watch.start_in_background(store.db_path, float(os.environ.get("DB_WATCH_SECONDS", "60")))
        yield

    app = FastAPI(title="Delay Assistant API", lifespan=lifespan)

    @app.get("/api/health")
    def health():
        return {"ok": True, "first_day": store.min_day.isoformat(), "last_day": store.max_day.isoformat(),
                "stations": len(store.stations)}

    @app.get("/api/stations")
    def stations(q: str = Query("", max_length=60)):
        return store.station_search(q)           # [] under 2 letters or without a prefix match

    @app.get("/api/overview")
    def overview(days: int = Query(30, ge=7, le=90)):
        return cache.get(("overview", days), store.max_day,
                         lambda: store.hourly_profile(days=days, cur=store.cursor()))

    @app.get("/api/persistence")
    def persistence():
        result = cache.get(("persistence",), store.max_day, lambda: store.persistence(cur=store.cursor()))
        if result is None:
            return {"available": False, "reason": "needs two full months of data"}
        return {"available": True, **result}
    
    @app.get("/api/report")
    def report():
        def facts():
            pe = persistence()
            causes = cache.get(("causes", 30), store.max_day, lambda: store.top_causes(30, cur=store.cursor()))
            patterns = cache.get(("patterns", 30), store.max_day, lambda: store.time_patterns(30, cur=store.cursor()))
            return rp.build_facts(overview(days=30), pe if pe.get("available") else None, causes, patterns)
        return reports.get(store.max_day, facts)
    
    @app.get("/api/journeys")


    def journeys(from_id: str, to_id: str,
                 date_: str = Query(..., alias="date"),
                 time: str = Query("12:00"),
                 window: int = Query(7),
                 dticket: str = Query("off")):
        # ---- validate: the user must have picked stations from the list
        try:
            origin, dest = ext_id_of(from_id), ext_id_of(to_id)
            day = date.fromisoformat(date_)
            hh, mm = (int(x) for x in time.split(":"))
            when = datetime(day.year, day.month, day.day, hh, mm)
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid station id, date or time") from None
        if window not in WINDOWS:
            raise HTTPException(422, f"window must be one of {WINDOWS}")
        if dticket not in DT_MODES:
            raise HTTPException(422, f"dticket must be one of {DT_MODES}")
        if origin not in known_ext_ids or dest not in known_ext_ids:
            raise HTTPException(422, "please pick both stations from the list")

        today = datetime.now(BERLIN).date()
        meta = {"mode": time_mode(day, today), "window": window, "dticket": dticket,
                "source": getattr(source, "name", "unknown"),    # "bahn" or "fixture": the page flags demo data
                "data_from": store.min_day.isoformat(), "data_to": store.max_day.isoformat()}
        if origin == dest:
            return {"journeys": [], "message": "No results found", **meta}
        try:
            response = source.fetch(location_id(origin), location_id(dest), when, dticket)
        except JourneySourceError as e:
            raise HTTPException(502, f"journey source unavailable: {e}") from None
        result = ja.assemble(response, store, window, dticket)
        if not result["journeys"]:
            result["message"] = "No results found"
        return {**enrich_causes(result), **meta}

    # the web page: plain files from web/, served last so every /api route above wins
    web = Path(os.environ.get("WEB_DIR", "web"))
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    # the web page itself, served from the same address (must come after the /api routes)
    web = Path(os.environ.get("WEB_DIR", "web"))
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    # the page itself: web/index.html, style.css, app.js. Mounted LAST so the /api routes above win.
    web_dir = Path(os.environ.get("WEB_DIR", "web"))
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


@functools.cache
def _default_app() -> FastAPI:
    return create_app()


def __getattr__(name):
    """`uvicorn app:app` asks for the attribute `app`: build the real app on first access.
    Importing this module for tests builds nothing and never opens the database."""
    if name == "app":
        return _default_app()
    raise AttributeError(name)
