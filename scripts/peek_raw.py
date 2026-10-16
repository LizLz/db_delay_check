"""peek_raw.py - look at ONE raw hour file of the Hugging Face dataset: schema, api names, size."""
import sys
from datetime import date, timedelta

import duckdb
from huggingface_hub import HfApi, hf_hub_download

REPO = "piebro/deutsche-bahn-data"
d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=2)
api = HfApi()
prefixes = {f"raw_data/year={d.year}/month={d.month}/day={d.day}/",
            f"raw_data/year={d.year}/month={d.month:02d}/day={d.day:02d}/"}
files = [f for f in api.list_repo_files(REPO, repo_type="dataset") if any(f.startswith(p) for p in prefixes)]
print(f"{d}: {len(files)} files")
if not files:
    sys.exit("no raw files found for that day")
infos = api.get_paths_info(REPO, files, repo_type="dataset")
print(f"total size of the day: {sum(i.size or 0 for i in infos) / 1e6:.0f} MB")
path = hf_hub_download(REPO, files[0], repo_type="dataset")
con = duckdb.connect()
print("\ncolumns:")
for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall():
    print("  ", row[0], row[1])
print("\nrows by api_name / status_code:")
for row in con.execute("SELECT api_name, status_code, count(*) FROM read_parquet(?) GROUP BY ALL", [path]).fetchall():
    print("  ", row)
print("\none example row per api_name (long values cut to 300 chars):")
for (name,) in con.execute("SELECT DISTINCT api_name FROM read_parquet(?)", [path]).fetchall():
    cur = con.execute("SELECT * FROM read_parquet(?) WHERE api_name = ? LIMIT 1", [path, name])
    cols = [c[0] for c in cur.description]
    row = cur.fetchone()
    print(f"\n-- {name}")
    for c, v in zip(cols, row):
        print(f"   {c}: {str(v)[:300]!r}")