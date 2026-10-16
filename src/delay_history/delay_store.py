"""delay_store.py - DuckDB-backed read layer for the DB delay project.

Replaces: compute_hourly_delay_matrix, run_forensic_analysis_service, the XGBoost/SHAP
code and dropdowns.json. Everything below follows the decisions in the spec:

  * delay          = ARRIVAL delay at the leg destination, negatives clamped to 0
  * aggregate      = MEDIAN, cancelled runs excluded from the median but counted
  * leg matching   = train number + (train type) + arrival EVA + time-of-day proximity,
                     one closest match per calendar day
  * overview chart = median arrival delay by planned ARRIVAL hour, last 30 days
  * persistence    = median delay per train, month before last (x) vs last month (y)
  * stations       = plain prefix search, dropdown after 2 letters, [] if no match

Parquet columns used (piebro/deutsche-bahn-data monthly_processed_data schema):
  train_number, train_type, eva, station_name, arrival_planned_time,
  arrival_change_time, arrival_is_canceled (or is_canceled)
  and optionally reason_code (not in the dataset; added by your own cause collector).
parquet_path may be a glob, e.g. "data/monthly/data-2025-1*.parquet".
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from statistics import median

import duckdb

START_DAY = "2025-10-01"
LONG_DISTANCE = {"ICE", "IC", "EC", "ECE", "RJ", "RJX", "NJ", "EN", "FLX", "TGV"}
CHART_TYPES = ("ICE", "IC", "RE", "RB", "S")
MIN_RUNS_FOR_MISS_PROBABILITY = 2   # history-based P(miss) needs >= 2 delay records
HIGH_CANCELLATION_SHARE = 0.20    # strictly above this: "High cancellation"
MIN_N_PER_HOUR_POINT = 30         # hide hourly points built from fewer arrivals
DELAYED_MIN = 6                   # DB definition: >= 6 min is delayed
RUSH_HOURS = (6, 7, 8, 16, 17, 18)   # 06:00-08:59 and 16:00-18:59
# Rail-replacement buses ride the train product classes, so they sit in the data with
# normal-looking stops. Verify the real labels first:
#   SELECT train_type, count(*) FROM '<month>.parquet' GROUP BY 1 ORDER BY 2 DESC
NON_RAIL_TYPES = ("Bus", "Bsv", "obu")


# --------------------------------------------------------------------------- build
def build_db_file(parquet_path: Path, db_path: Path, start_day: str = START_DAY,
                  exclude_types: tuple[str, ...] = NON_RAIL_TYPES,
                  reasons_parquet: Path | None = None) -> None:
    """Materialise a clean, sorted table once per pipeline run (atomic swap).
    Sorting by (train_no, eva) lets DuckDB skip row groups on leg lookups.

    reasons_parquet (optional): small file with columns  id, reason_code, reason_ts
    = the latest delay-cause message per stop id, parsed from the raw `fchg` responses.
    Joined on the stop `id`. Without it, reason_code stays NULL (no cause shown)."""
    tmp = db_path.with_name(db_path.name + ".building")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute("SET threads=4")
    con.execute("SET memory_limit='2GB'")
    cols = {r[0] for r in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}')").fetchall()}
    reason = "CAST(src.reason_code AS INTEGER)" if "reason_code" in cols else "CAST(NULL AS INTEGER)"
    join = ""
    if reasons_parquet:
        if "id" not in cols:
            raise RuntimeError("reasons_parquet needs an `id` column in the main parquet")
        join = (f"LEFT JOIN (SELECT id AS rid, CAST(reason_code AS INTEGER) AS rcode "
                f"FROM read_parquet('{reasons_parquet}')) r ON src.id = r.rid")
        reason = f"coalesce(r.rcode, {reason})"
    # piebro monthly files: arrival_is_canceled (what is needed: the arrival at the
    # leg destination). Your own merged archive may call it is_canceled.
    if "arrival_is_canceled" in cols:
        cancel = "coalesce(arrival_is_canceled, false)"
    elif "is_canceled" in cols:
        cancel = "coalesce(is_canceled, false)"
    else:
        raise RuntimeError("no arrival_is_canceled / is_canceled column in the parquet")
    # German S-Bahn: bahn.de sends the line label ("S5") where other trains carry a run
    # number, so key those rows by line. Some networks report the bare digit ("5") in
    # line_number -> "S5". Everything else: the run number without leading zeros.
    run_no = "ltrim(CAST(train_number AS VARCHAR), '0')"
    if "line_number" in cols:
        german = "lpad(CAST(eva AS VARCHAR), 8, '0') LIKE '080%'"
        train_no = (f"CASE WHEN train_type = 'S' AND {german} AND line_number LIKE 'S%' "
                    f"THEN replace(line_number, ' ', '') "
                    f"WHEN train_type = 'S' AND {german} AND regexp_matches(line_number, '^[0-9]+$') "
                    f"THEN 'S' || line_number ELSE {run_no} END")
    else:
        train_no = run_no
    excl = ",".join("'" + t.replace("'", "''") + "'" for t in exclude_types) or "''"
    con.execute("SET preserve_insertion_order=false")      # lets the big sort spill to disk
    con.execute(f"SET temp_directory='{db_path.parent / 'duckdb_tmp'}'")
    con.execute(f"""
        CREATE TABLE delays AS
        SELECT
            {train_no}                                                      AS train_no,
            train_type,
            lpad(CAST(eva AS VARCHAR), 8, '0')                              AS eva,
            station_name,
            arrival_planned_time,
            CAST(arrival_planned_time AS DATE)                              AS arr_day,
            hour(arrival_planned_time)                                      AS arr_hour,
            hour(arrival_planned_time) * 60 + minute(arrival_planned_time)  AS arr_mod,
            -- no change message = on time; early = 0
            greatest(coalesce(date_diff('minute', arrival_planned_time,
                                        arrival_change_time), 0), 0)        AS arr_delay_min,
            {cancel}                                                        AS is_canceled,
            {reason}                                                        AS reason_code
        FROM read_parquet('{parquet_path}') src
        {join}
        WHERE arrival_planned_time IS NOT NULL
          AND arrival_planned_time >= TIMESTAMP '{start_day}'
          AND coalesce(train_type, '') NOT IN ({excl})
        ORDER BY train_no, eva
    """)
    n = con.execute("SELECT count(*) FROM delays").fetchone()[0]
    con.close()
    if not n:
        raise RuntimeError("delays table is empty - refusing to swap it in")
    os.replace(tmp, db_path)


# ----------------------------------------------------------------------- stations
def _clean(s: str) -> str:
    s = s.replace("hauptbahnhof", "hbf")
    s = re.sub(r"\(.*?\)", " ", s)              # "(Main)", "(M)"
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def search_keys(name: str) -> list[str]:
    """Munchen + Muenchen keys, so both spellings match by prefix."""
    low = name.casefold()
    expanded = (low.replace("ä", "ae").replace("ö", "oe")
                   .replace("ü", "ue").replace("ß", "ss"))
    return sorted({_clean(_strip_accents(low)), _clean(_strip_accents(expanded))})


def norm_query(q: str) -> str:
    return _clean(_strip_accents(q.casefold().replace("ß", "ss")))


# ------------------------------------------------------------------------- store
class DelayStore:
    def __init__(self, db_path: Path, dim_stations_parquet: Path | None = None,
                 min_arrivals: int = 1):   # 1 = list every station, as in the spec
        self.db_path = Path(db_path)
        # read_only: the pipeline can swap the file atomically without colliding
        self.con = duckdb.connect(str(db_path), read_only=True)
        self.con.execute("SET memory_limit='2GB'")
        self.con.execute("SET threads=4")
        self.min_day, self.max_day = self.con.execute(
            "SELECT min(arr_day), max(arr_day) FROM delays").fetchone()
        self._build_station_index(dim_stations_parquet, min_arrivals)

    def cursor(self):
        """One cursor per worker thread (a DuckDB connection is not thread-safe)."""
        return self.con.cursor()

    # ---- station dropdown --------------------------------------------------
    def _build_station_index(self, dim_parquet: Path | None, min_arrivals: int) -> None:
        if dim_parquet and Path(dim_parquet).exists():
            rows = self.con.execute(f"""
                SELECT d.eva, coalesce(s.station_name, d.station_name) AS name, d.cnt
                FROM (SELECT eva, any_value(station_name) AS station_name, count(*) AS cnt
                      FROM delays GROUP BY eva) d
                LEFT JOIN (SELECT lpad(CAST(eva AS VARCHAR), 8, '0') AS eva, station_name
                           FROM read_parquet('{dim_parquet}')) s USING (eva)
                WHERE coalesce(s.station_name, d.station_name) IS NOT NULL
            """).fetchall()
        else:
            rows = self.con.execute("""
                SELECT eva, any_value(station_name), count(*) FROM delays
                WHERE station_name IS NOT NULL GROUP BY eva""").fetchall()

        groups: dict[str, dict] = {}     # one dropdown entry per folded name:
        for eva, name, cnt in rows:      # multi-level Hbf EVAs collapse into one
            if cnt < min_arrivals:       # too little history to be a useful search result
                continue
            g = groups.setdefault(_clean(_strip_accents(name.casefold())),
                                  {"name": name, "evas": [], "total": 0, "best": 0, "best_eva": eva})
            g["evas"].append(eva)
            g["total"] += cnt
            # the id bahn.de wants is the main station's (80xxxxx). Big Hbf also have an "underground
            # level" id (8098xxx) that is busier with S-Bahn but is not the one journey searches use,
            # so those only win when nothing else exists
            rank = (not eva.startswith("08098"), cnt)
            if rank > g.setdefault("rank", (False, 0)):
                g["name"], g["best"], g["best_eva"], g["rank"] = name, cnt, eva, rank
        self.stations = sorted(
            ({"name": g["name"], "evas": g["evas"], "eva": g["best_eva"], "total": g["total"],
              "keys": search_keys(g["name"]),
              "is_hbf": "hbf" in _clean(g["name"].casefold()).split()}
             for g in groups.values()),
            key=lambda s: (not s["is_hbf"], -s["total"]))

    def station_search(self, query: str, limit: int = 10) -> list[dict]:
        k = norm_query(query)
        if len(k) < 2:
            return []
        hits = [s for s in self.stations if any(key.startswith(k) for key in s["keys"])]
        out = []
        for s in hits[:limit]:
            ext = s["eva"].lstrip("0")                 # bahn.de's extId is unpadded
            out.append({"name": s["name"], "evas": s["evas"], "extId": ext,
                        "id": f"A=1@O={s['name']}@L={ext}@"})   # location id for the journey search
        return out

    def polling_stations(self, top: int = 300) -> list[dict]:
        """Busiest rail stations by arrival volume: the list for the cause collector.
        Same idea as ranking by GTFS stop events, but we already have the counts.
        At 60 API calls/min, 300 stations is one pass in about 5 minutes."""
        rows = self.con.execute("""
            SELECT eva, any_value(station_name), count(*) AS n
            FROM delays GROUP BY eva ORDER BY n DESC LIMIT ?""", [top]).fetchall()
        return [{"eva": e, "name": n, "arrivals": c} for e, n, c in rows]

    def evas_of(self, eva: str) -> list[str]:
        """All EVAs that belong to the same station entry as `eva`."""
        eva = eva.rjust(8, "0")
        for s in self.stations:
            if eva in s["evas"]:
                return s["evas"]
        return [eva]

    # ---- overview chart ----------------------------------------------------
    def hourly_profile(self, days: int = 30, cur=None) -> dict:
        """Median arrival delay by planned arrival hour, per train type + 'All'.
        Cancelled excluded, negatives already clamped. Hours with too little data
        are None (a gap in the line), NOT 0."""
        cur = cur or self.con
        start = self.max_day - timedelta(days=days - 1)
        ph = ",".join("?" * len(CHART_TYPES))
        rows = cur.execute(f"""
            WITH base AS (
                SELECT arr_hour, train_type, arr_delay_min FROM delays
                WHERE NOT is_canceled AND arr_day >= ?)
            SELECT arr_hour, train_type AS series, median(arr_delay_min), count(*)
            FROM base WHERE train_type IN ({ph}) GROUP BY 1, 2
            UNION ALL
            SELECT arr_hour, 'All', median(arr_delay_min), count(*) FROM base GROUP BY 1
        """, [start, *CHART_TYPES]).fetchall()
        series = {s: [None] * 24 for s in (*CHART_TYPES, "All")}
        for hour, s, med, n in rows:
            if n >= MIN_N_PER_HOUR_POINT:
                series[s][hour] = {"median": round(float(med), 1), "n": n}
        return {"hours": [f"{h:02d}:00" for h in range(24)], "series": series,
                "start": start.isoformat(), "end": self.max_day.isoformat()}
    
    def top_causes(self, days: int = 30, cur=None) -> dict:
        """Recorded delay causes of delayed (>= 6 min) arrivals in the last `days` days:
        {"delayed": all delayed arrivals, "codes": {reason_code: count}} (only arrivals with a code)."""
        cur = cur or self.con
        start = self.max_day - timedelta(days=days - 1)
        delayed = cur.execute(
            "SELECT count(*) FROM delays WHERE NOT is_canceled AND arr_day >= ? AND arr_delay_min >= ?",
            [start, DELAYED_MIN]).fetchone()[0]
        rows = cur.execute(
            "SELECT reason_code, count(*) FROM delays WHERE NOT is_canceled AND arr_day >= ? "
            "AND arr_delay_min >= ? AND reason_code IS NOT NULL GROUP BY 1",
            [start, DELAYED_MIN]).fetchall()
        return {"days": days, "delayed": delayed, "codes": {int(c): int(n) for c, n in rows}}

    def time_patterns(self, days: int = 30, cur=None, min_n: int = 200) -> dict:
        """When are trains late? Weekday vs weekend, rush hours vs the rest, the worst weekday and the
        worst hours, over the last `days` days (cancelled excluded). "delayed" = arrival >= 6 min late."""
        cur = cur or self.con
        start = self.max_day - timedelta(days=days - 1)
        base = "FROM delays WHERE NOT is_canceled AND arr_day >= ?"
        cells = cur.execute(f"""SELECT isodow(arr_day), arr_hour, count(*),
                                        count(*) FILTER (WHERE arr_delay_min >= {DELAYED_MIN}) {base} GROUP BY 1, 2""",
                            [start]).fetchall()
        share = lambda rows: (round(100 * sum(r[3] for r in rows) / sum(r[2] for r in rows))
                                if rows and sum(r[2] for r in rows) else None)
        groups = {"weekday": [c for c in cells if c[0] <= 5], "weekend": [c for c in cells if c[0] >= 6],
                    "rush": [c for c in cells if c[1] in RUSH_HOURS], "other": [c for c in cells if c[1] not in RUSH_HOURS]}
        med = lambda where: cur.execute(f"SELECT median(arr_delay_min) {base} AND {where}", [start]).fetchone()[0]
        wd = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        by_day = {d: share([c for c in cells if c[0] == d]) for d in range(1, 8)}
        by_day = {d: v for d, v in by_day.items() if v is not None}
        hours = []
        for h in range(24):
            rows = [c for c in cells if c[1] == h]
            if rows and sum(r[2] for r in rows) >= min_n:
                hours.append((h, share(rows)))
        out = {"days": days,
                "weekday_delayed_percent": share(groups["weekday"]), "weekend_delayed_percent": share(groups["weekend"]),
                "weekday_median_min": round(float(med("isodow(arr_day) <= 5") or 0), 1),
                "weekend_median_min": round(float(med("isodow(arr_day) >= 6") or 0), 1),
                "rush_hours": "06:00-08:59 and 16:00-18:59",
                "rush_delayed_percent": share(groups["rush"]), "other_hours_delayed_percent": share(groups["other"])}
        if by_day:
            worst_d, best_d = max(by_day, key=by_day.get), min(by_day, key=by_day.get)
            out["worst_weekday"] = {"name": wd[worst_d - 1], "delayed_percent": by_day[worst_d]}
            out["best_weekday"] = {"name": wd[best_d - 1], "delayed_percent": by_day[best_d]}
        out["worst_hours"] = [{"window": f"{h:02d}:00-{h:02d}:59", "delayed_percent": pc}
                                for h, pc in sorted(hours, key=lambda x: -x[1])[:3]]
        if hours:
            h, pc = min(hours, key=lambda x: x[1])
            out["best_hour"] = {"window": f"{h:02d}:00-{h:02d}:59", "delayed_percent": pc}
        return out

    # ---- per-leg stats (search results + bar chart) ------------------------
    def leg_stats(self, train_number: str, eva: str, planned_arrival,
                  train_type: str | None = None, window: int = 7, cur=None,
                  tolerance_min: int | None = None) -> dict | None:
        """planned_arrival: datetime in Berlin local time.  Returns None when the
        train has no record at all in the window (shown as 'no data')."""
        cur = cur or self.con
        evas = self.evas_of(eva)
        tol = tolerance_min if tolerance_min is not None else (
            120 if (train_type or "").upper() in LONG_DISTANCE or train_type is None else 30)
        start = self.max_day - timedelta(days=window - 1)
        mod = planned_arrival.hour * 60 + planned_arrival.minute
        type_sql, type_args = ("AND train_type = ?", [train_type]) if train_type else ("", [])
        rows = cur.execute(f"""
            WITH cand AS (
                SELECT arr_day AS day, arr_delay_min AS delay, is_canceled, reason_code,
                       least(abs(arr_mod - ?), 1440 - abs(arr_mod - ?)) AS d
                FROM delays
                WHERE train_no = ? AND eva IN ({",".join("?" * len(evas))}) {type_sql}
                  AND arr_day >= ?)
            SELECT DISTINCT ON (day) day, delay, is_canceled, reason_code
            FROM cand WHERE d <= ?
            ORDER BY day, d
        """, [mod, mod, train_number.lstrip("0"), *evas, *type_args, start, tol]).fetchall()
        if not rows:
            return None

        ok = [d for _, d, canc, _ in rows if not canc]
        cancelled = len(rows) - len(ok)
        share = cancelled / len(rows)
        badges = []
        if share > HIGH_CANCELLATION_SHARE:
            badges.append("High cancellation")
        # No minimum sample anywhere in search: with even one non-cancelled run the card
        # shows that run's delay and the panel draws that one bar (the badge carries
        # n/days). Only a train with NO record at all (leg_stats -> None) has nothing to draw.
        has_median = len(ok) >= 1
        return {
            "median_delay": int(median(ok) + 0.5) if has_median else None,   # None: all cancelled
            "is_delayed": bool(has_median and median(ok) >= DELAYED_MIN),    # red vs gray
            "days_matched": len(rows),
            "non_cancelled_runs": len(ok),
            "cancelled_days": cancelled,
            "cancel_share": round(share, 3),
            "badges": badges,
            # days with no record (no service) are simply absent -> no bar at all
            "days": [{"day": day.isoformat(),
                      "delay": None if canc else delay,
                      "cancelled": bool(canc),
                      "reason_code": reason}
                     for day, delay, canc, reason in rows],
            "window_start": start.isoformat(), "window_end": self.max_day.isoformat(),
            "delays_for_miss_probability": ok,
        }

    # ---- persistence chart --------------------------------------------------
    def persistence(self, min_runs: int = 10, max_points: int = 3000, cur=None) -> dict | None:
        """x = median delay in the month BEFORE last, y = median delay in LAST month,
        one dot per (train type, train number, arrival station)."""
        cur = cur or self.con
        d = self.max_day
        last_start = d.replace(day=1)
        if (d + timedelta(days=1)).month == d.month:        # last data month incomplete
            last_start = (last_start - timedelta(days=1)).replace(day=1)
        last_end = (last_start + timedelta(days=32)).replace(day=1)
        prev_start = (last_start - timedelta(days=1)).replace(day=1)
        if prev_start < date.fromisoformat(START_DAY).replace(day=1):
            return None                                    # not two full months yet

        cur.execute(f"""
            CREATE OR REPLACE TEMP TABLE _pairs AS
            WITH pm AS (
                SELECT train_type, train_no, eva, date_trunc('month', arr_day)::DATE AS m,
                       median(arr_delay_min) AS med
                FROM delays
                WHERE NOT is_canceled AND arr_day >= ? AND arr_day < ?
                GROUP BY ALL HAVING count(*) >= ?)
            SELECT a.train_type, a.train_no, a.eva, a.med AS x, b.med AS y
            FROM pm a JOIN pm b USING (train_type, train_no, eva)
            WHERE a.m = ? AND b.m = ?
        """, [prev_start, last_end, min_runs, prev_start, last_start])

        n, slope, intercept, r, r2, delayed_before, repeated, late_x, late_y, shorter = cur.execute(f"""
            SELECT count(*), regr_slope(y, x), regr_intercept(y, x), corr(x, y), regr_r2(y, x),
                   count(*) FILTER (WHERE x >= {DELAYED_MIN}),
                   count(*) FILTER (WHERE x >= {DELAYED_MIN} AND y >= {DELAYED_MIN}),
                   median(x) FILTER (WHERE x >= {DELAYED_MIN}),
                   median(y) FILTER (WHERE x >= {DELAYED_MIN}),
                   count(*) FILTER (WHERE x >= {DELAYED_MIN} AND y < x)
            FROM _pairs""").fetchone()
        if n < 10:
            return None
        # Spearman with average ranks for ties (many trains sit at exactly 0)
        spearman = cur.execute("""
            SELECT corr(rx, ry) FROM (
                SELECT rank() OVER (ORDER BY x) + (count(*) OVER (PARTITION BY x) - 1) / 2.0 AS rx,
                       rank() OVER (ORDER BY y) + (count(*) OVER (PARTITION BY y) - 1) / 2.0 AS ry
                FROM _pairs)""").fetchone()[0]
        pts = cur.execute("SELECT train_type, x, y FROM _pairs ORDER BY random() LIMIT ?",
                          [max_points]).fetchall()
        f = lambda v: None if v is None else round(float(v), 3)
        return {
            "x_month": prev_start.strftime("%Y-%m"), "y_month": last_start.strftime("%Y-%m"),
            "n_trains": n, "slope": f(slope), "intercept": f(intercept),
            "pearson_r": f(r), "r2": f(r2), "spearman": f(spearman),
            "delayed_before": delayed_before,
            "still_delayed": repeated,
            "repeat_rate": round(repeated / delayed_before, 3) if delayed_before else None,
            "late_median_before": f(late_x), "late_median_after": f(late_y),
            "shorter_rate": round(shorter / delayed_before, 3) if delayed_before else None,
            "points": [{"type": t, "x": x, "y": y} for t, x, y in pts],
        }


# ------------------------------------------------------------------- transfers
def connection_status(planned_transfer_min: int, d1_live: int | None = None,
                      d2_live: int = 0, history: list[int] = ()) -> dict:
    """planned_transfer_min = planned departure of leg 2 minus planned arrival of leg 1."""
    if d1_live is not None:                       # real-time delay of leg 1 is known
        eff = planned_transfer_min - d1_live + (d2_live or 0)
        status = "likely_missed" if eff <= 0 else "tight" if eff < 5 else "ok"
        return {"status": status, "effective_transfer_min": eff, "p_miss": None}
    tight = planned_transfer_min < 5
    if len(history) < MIN_RUNS_FOR_MISS_PROBABILITY:
        return {"status": "tight" if tight else "unknown", "p_miss": None,
                "effective_transfer_min": planned_transfer_min}
    p = sum(d > planned_transfer_min for d in history) / len(history)
    return {"status": "tight" if tight else "ok", "p_miss": round(p, 2),
            "effective_transfer_min": planned_transfer_min}


# --------------------------------------------------------------------------- CLI
def _main(argv: list[str] | None = None) -> int:
    """  uv run python -m delay_history.delay_store build "data/monthly/data-2026-0[89].parquet" data/delays.duckdb
         uv run python -m delay_history.delay_store smoke data/delays.duckdb [--train 594 --eva 8000105 --time 17:30]"""
    import argparse
    from datetime import datetime

    p = argparse.ArgumentParser(prog="delay_store")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the DuckDB table from monthly parquet file(s)")
    b.add_argument("parquet", help="path or glob of parquet file(s)")
    b.add_argument("db")
    b.add_argument("--start", default=START_DAY, help="first planned-arrival day to keep")
    b.add_argument("--reasons", help="optional reasons.parquet from extract_reasons.py")
    s = sub.add_parser("smoke", help="print a health report of a built database")
    s.add_argument("db")
    s.add_argument("--search", default="fr", help="station prefix to try in the dropdown")
    s.add_argument("--train"), s.add_argument("--eva"), s.add_argument("--time", default="12:00")
    a = p.parse_args(argv)

    if a.cmd == "build":
        import time
        t0 = time.time()
        # keep the glob a plain string: Path() would turn "/" into "\" on Windows
        build_db_file(a.parquet.replace("\\", "/"), Path(a.db), start_day=a.start,
                      reasons_parquet=a.reasons.replace("\\", "/") if a.reasons else None)
        con = duckdb.connect(a.db, read_only=True)
        n, lo, hi = con.execute("SELECT count(*), min(arr_day), max(arr_day) FROM delays").fetchone()
        print(f"built {a.db}: {n:,} arrivals, {lo} .. {hi}, "
              f"{Path(a.db).stat().st_size / 1e6:.0f} MB, {time.time() - t0:.0f}s")
        return 0

    st = DelayStore(Path(a.db))
    print(f"days: {st.min_day} .. {st.max_day}   stations: {len(st.stations):,}")
    print(f"dropdown '{a.search}': {[x['name'] for x in st.station_search(a.search, 5)]}")
    prof = st.hourly_profile()
    for t in ("ICE", "RE", "All"):
        pts = [(h, c["median"], c["n"]) for h, c in enumerate(prof["series"][t]) if c]
        print(f"hourly {t}: {len(pts)}/24 hours with data; peak: "
              f"{max(pts, key=lambda x: x[1]) if pts else None}  (hour, median, n)")
    pers = st.persistence()
    print("persistence:", "not available yet (needs two full months)" if pers is None else
          {k: pers[k] for k in ("x_month", "y_month", "n_trains", "slope", "pearson_r", "repeat_rate")})
    print("busiest stations:", [(x["name"], x["arrivals"]) for x in st.polling_stations(3)])
    if a.train and a.eva:
        h, m = map(int, a.time.split(":"))
        r = st.leg_stats(a.train, a.eva, datetime(2026, 1, 1, h, m), window=30)
        if r is None:
            print(f"leg {a.train} @ {a.eva} {a.time}: no history")
        else:
            print(f"leg {a.train} @ {a.eva} {a.time}: median +{r['median_delay']} min, "
                  f"{r['days_matched']} days, cancelled {r['cancelled_days']}, badges {r['badges']}")
            for d in r["days"][-7:]:
                print("   ", d)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
