"""Export small aggregated Parquet tables from the delay database for Power BI.

Run from the project root (stop nothing, the database is only read):

    uv run python scripts/export_bi.py
    uv run python scripts/export_bi.py --days 365 --min-arrivals 500

Definitions (same as the web app): delay = arrival delay at the destination in
minutes, delayed = 6 minutes or more, cancelled trains are excluded from delays
but counted. Medians are only valid for exactly the grain of the table they are
in; in Power BI use the additive columns (arrivals, delayed, cancelled,
delay_sum_capped) for anything that is sliced or summed.
"""
import argparse
from pathlib import Path

import duckdb

DELAYED_MIN = 6
CAP_MIN = 60  # delays are capped at this value in delay_sum_capped (keeps rare huge delays from dominating averages)

# raw train_type -> family. Operator codes (HLB, BRB, NWB, VIA, ...) are regional railways.
TRAIN_GROUP_SQL = """
    CASE
      WHEN train_type IS NULL OR trim(train_type) = '' OR regexp_matches(train_type, '^[-0-9 ]') THEN 'Unknown'
      WHEN regexp_matches(upper(train_type), '^ICE[0-9]*$') THEN 'ICE'
      WHEN regexp_matches(upper(train_type), '^(IC|EC|ECE|RJ|RJX|NJ|EN|TGV|THA|FLX)[0-9]*$') THEN 'IC / EC / international'
      WHEN regexp_matches(upper(train_type), '^(L-)?S[0-9]*$') THEN 'S-Bahn'
      WHEN regexp_matches(upper(train_type), '^(L-)?(RE|IRE|MEX|RRX)[0-9]*$') THEN 'Regional Express'
      WHEN regexp_matches(upper(train_type), '^(L-)?(RB|R)[0-9]*$') THEN 'Regionalbahn'
      ELSE 'Other regional operators'
    END"""

REGION_COLUMNS = ("state", "bundesland", "federal_state", "region", "land")


def _station_dim(con, db_stations: Path | None):
    """Return (sql_source, region_expr) for the station dimension."""
    if db_stations and db_stations.exists():
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{db_stations.as_posix()}')").fetchall()]
        print("dim_stations.parquet columns:", cols)
        region = next((c for c in cols if c.lower() in REGION_COLUMNS), None)
        eva = next((c for c in cols if c.lower() in ("eva", "eva_nr", "evanumber", "stop_id")), None)
        if region and eva:
            return (f"(SELECT CAST({eva} AS BIGINT) AS eva, {region} AS region "
                    f"FROM read_parquet('{db_stations.as_posix()}'))")
        print("no region/eva column found in the stations file; the region column will be empty.")
    return None


def _cause_rows(codes):
    """(reason_code text, readable cause, category) per code, using the same mapping as the web app."""
    try:
        from delay_history import cause_codes as cc
    except Exception as e:  # run from the project root with `uv run` so the package is importable
        print(f"cause_codes not importable ({e}); dim_cause will only repeat the codes.")
        cc = None
    rows = []
    for code in sorted(codes, key=str):
        text = cat_label = None
        if cc is not None:
            try:
                text = cc.cause_text(code)
                cat = cc.cause_category(code)
                cat_label = cc.CATEGORY_LABELS.get(cat) if cat else None
            except Exception:
                pass
        rows.append((str(code), text or f"Code {code}", cat_label or "Other"))
    return rows


def export(db: Path, out: Path, days: int, min_arrivals: int, stations: Path | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db), read_only=True)
    last = con.execute("SELECT max(CAST(arr_day AS DATE)) FROM delays").fetchone()[0]
    con.execute(f"""
        CREATE TEMP VIEW d AS
        SELECT CAST(arr_day AS DATE) AS day, CAST(eva AS BIGINT) AS eva, station_name, train_type,
               arr_hour AS hour, arr_delay_min, is_canceled, reason_code
        FROM delays
        WHERE CAST(arr_day AS DATE) > DATE '{last}' - INTERVAL {days} DAY
    """)
    add = f"""
        count(*) AS arrivals,
        count(*) FILTER (WHERE NOT is_canceled AND arr_delay_min >= {DELAYED_MIN}) AS delayed,
        count(*) FILTER (WHERE is_canceled) AS cancelled,
        coalesce(sum(least(greatest(arr_delay_min, 0), {CAP_MIN})) FILTER (WHERE NOT is_canceled), 0) AS delay_sum_capped
    """
    agg = add + ", quantile_cont(arr_delay_min, 0.5) FILTER (WHERE NOT is_canceled) AS median_delay"

    def copy(sql: str, name: str) -> None:
        path = out / f"{name}.parquet"
        con.execute(f"COPY ({sql}) TO '{path.as_posix()}' (FORMAT parquet)")
        n = con.execute(f"SELECT count(*) FROM read_parquet('{path.as_posix()}')").fetchone()[0]
        print(f"{name}: {n:,} rows -> {path}")

    # stations with enough traffic only (keeps the file small and the medians meaningful)
    con.execute(f"""
        CREATE TEMP VIEW big AS
        SELECT eva FROM d GROUP BY eva HAVING count(*) >= {min_arrivals}
    """)
    copy(f"""SELECT day, eva, any_value(station_name) AS station_name, {agg}
             FROM d WHERE eva IN (SELECT eva FROM big) GROUP BY day, eva""", "fact_daily_station")
    copy(f"""SELECT isodow(day) AS weekday, hour, {agg} FROM d GROUP BY 1, 2""", "fact_hour_weekday")
    copy(f"""SELECT {TRAIN_GROUP_SQL} AS train_group, hour, {add}
             FROM d GROUP BY 1, 2""", "fact_train_type_hour")
    copy(f"""SELECT day, CAST(reason_code AS VARCHAR) AS reason_code, count(*) AS arrivals
             FROM d WHERE NOT is_canceled AND arr_delay_min >= {DELAYED_MIN} AND reason_code IS NOT NULL
             GROUP BY 1, 2""", "fact_causes")

    codes = [r[0] for r in con.execute("SELECT DISTINCT reason_code FROM d WHERE reason_code IS NOT NULL").fetchall()]
    con.execute("CREATE TEMP TABLE cause_t (reason_code VARCHAR, cause VARCHAR, category VARCHAR)")
    con.executemany("INSERT INTO cause_t VALUES (?, ?, ?)", _cause_rows(codes))
    copy("SELECT * FROM cause_t", "dim_cause")

    src = _station_dim(con, stations)
    region = "NULL" if src is None else "s.region"
    join = "" if src is None else f"LEFT JOIN {src} s USING (eva)"
    copy(f"""SELECT eva, any_value(station_name) AS station_name, {region} AS region, count(*) AS arrivals_total
             FROM d {join} WHERE eva IN (SELECT eva FROM big) GROUP BY eva{', s.region' if src else ''}""", "dim_station")
    copy(f"""SELECT DISTINCT day AS date, year(day) AS year, month(day) AS month, isodow(day) AS weekday,
                    strftime(day, '%a') AS weekday_name FROM d""", "dim_date")
    print(f"done. data to {last}. Load the .parquet files in Power BI (Get data -> Parquet).")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", type=Path, default=Path("data/delays.duckdb"))
    p.add_argument("--out", type=Path, default=Path("data/bi"))
    p.add_argument("--days", type=int, default=365, help="how many days back from the newest day")
    p.add_argument("--min-arrivals", type=int, default=500, help="drop stations with fewer arrivals in the period")
    p.add_argument("--stations", type=Path, default=Path("data/processed/dim_stations.parquet"))
    a = p.parse_args()
    export(a.db, a.out, a.days, a.min_arrivals, a.stations)
