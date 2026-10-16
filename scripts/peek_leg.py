import json

d = json.load(open("data/bahn_peek.json", encoding="utf-8"))
for v in d["verbindungen"]:
    for leg in v["verbindungsAbschnitte"]:
        if (leg.get("verkehrsmittel") or {}).get("produktGattung") in ("SBAHN", "REGIONAL"):
            print(json.dumps(leg, ensure_ascii=False, indent=1)[:3500])
            raise SystemExit
print("no local-train leg in this search")