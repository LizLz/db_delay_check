"""peek_bahn.py - look at what bahn.de really answers, so field names can be checked against reality.

  uv run python scripts/peek_bahn.py                                  # Stuttgart -> Frankfurt, today 13:00
  uv run python scripts/peek_bahn.py --date 2026-10-14 --time 08:00
  uv run python scripts/peek_bahn.py --from-id "A=1@O=Berlin Hbf@L=8011160@" --to-id "A=1@O=Muenchen Hbf@L=8000261@"

Prints a short summary and saves the full answer to data/bahn_peek.json (send me that file or its first
connection if something on the page looks wrong: missing price, missing live times, wrong numbers).
"""
import argparse
import json
from datetime import date, datetime
from pathlib import Path

from journeys.source import BahnSource, JourneySourceError

p = argparse.ArgumentParser()
p.add_argument("--from-id", default="A=1@O=Stuttgart Hbf@L=8000096@")
p.add_argument("--to-id", default="A=1@O=Frankfurt (Main) Hbf@L=8000105@")
p.add_argument("--date", default=date.today().isoformat())
p.add_argument("--time", default="13:00")
p.add_argument("--dticket", default="off", choices=["off", "only", "all"])
a = p.parse_args()

when = datetime.fromisoformat(f"{a.date}T{a.time}:00")
try:
    data = BahnSource().fetch(a.from_id, a.to_id, when, a.dticket)
except JourneySourceError as e:
    raise SystemExit(f"bahn.de search failed: {e}")

Path("data").mkdir(exist_ok=True)
Path("data/bahn_peek.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
conns = data.get("verbindungen") or []
print(f"{len(conns)} connections for {when:%Y-%m-%d %H:%M} (top-level keys: {sorted(data)})")
for i, v in enumerate(conns[:5]):
    legs = v.get("verbindungsAbschnitte") or []
    print(f"\n#{i}: price={v.get('angebotsPreis')}  messages={[m.get('code') for m in v.get('meldungenAsObject') or []]}"
          f"  keys={sorted(v)[:12]}")
    for l in legs:
        vm = l.get("verkehrsmittel") or {}
        print(f"   {vm.get('mittelText') or vm.get('name')!s:12} nummer={vm.get('nummer')!s:6} {vm.get('produktGattung')!s:9}"
              f" dep={l.get('abfahrt')}  arr={l.get('ankunft')}")
print("\nfull answer saved to data/bahn_peek.json")
