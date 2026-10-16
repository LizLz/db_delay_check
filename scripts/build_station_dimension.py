import io, requests, polars as pl
from pathlib import Path

URL = "https://unpkg.com/db-stations@<PINNED_VERSION>/data.ndjson"   # pin it
OUT = Path("data/processed/dim_stations.parquet")

def build_station_dimension() -> pl.DataFrame:
    r = requests.get(URL, headers={"User-Agent": "delay-pipeline"}, timeout=30)
    r.raise_for_status()
    raw = pl.read_ndjson(io.BytesIO(r.content))
    dim = (raw.select(
               pl.col("id").cast(pl.Int64, strict=False).alias("eva"),
               pl.col("name").alias("station_name"),
               pl.col("location").struct.field("latitude").alias("latitude"),
               pl.col("location").struct.field("longitude").alias("longitude"))
           .drop_nulls(["eva", "station_name"])
           .unique("eva", keep="first"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    dim.write_parquet(OUT)
    print(f"{dim.height} stations, {dim['latitude'].null_count()} without coordinates")
    return dim