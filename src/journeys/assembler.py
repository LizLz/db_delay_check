"""journey_assembler.py - bahn.de journey-search response  ->  our result cards.

Pure functions: no HTTP, no FastAPI. Feed it the JSON that the journey source returned
(or a saved fixture) plus a DelayStore, get back the list the frontend renders.

Response shape used (field names as seen in the reference app's normalize_leg):
  verbindungen[] -> verbindungsAbschnitte[] (legs), umstiegsAnzahl, verbindungsDauerInSeconds,
                    risNotizen[], verbindungReference{earlier, later}
  leg: verkehrsmittel{typ, mittelText, name, nummer, produktGattung},
       abfahrt{sollzeit, echtzeit}, ankunft{sollzeit, echtzeit},
       abfahrtsOrt/abfahrtsOrtExtId, ankunftsOrt/ankunftsOrtExtId,
       originCancelled, destinationCancelled
Verify these against a real response before relying on them.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from delay_history import delay_store as ds
import re

BERLIN = ZoneInfo("Europe/Berlin")
# products that never appear in the delay data: a lookup could only false-match
# (e.g. tram line "1" hitting train number 1)
UNTRACKED_PRODUCTS = {"BUS", "TRAM", "UBAHN", "SCHIFF", "ANRUFPFLICHTIG"}
# long-distance products get the wide time-of-day match, everything else a tight one
LONG_DISTANCE_PRODUCTS = {"ICE", "EC_IC", "IR"}
TOL_LONG, TOL_SHORT = 120, 30
TIGHT_TRANSFER_MIN = 5      # spec: planned OR real-time transfer under 5 min


def to_berlin_naive(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo is None else dt.astimezone(BERLIN).replace(tzinfo=None)


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60

ZE_IN_ID = re.compile(r"#ZE#(\d+)#")        # "...#CA#ICE#ZE#520#..." inside a bahn.de journey id

def train_number(abschnitt: dict, vm: dict, product: str | None) -> str | None:
    """The real train number, the one the delay data is keyed by. Never a line name.
    bahn.de's `nummer` is the number for ICE/IC ("1096") but the LINE for local trains ("S8", "RB40"),
    where the real number sits in `name` ("80761") or inside the leg's journey id (#ZE#80761#).
    Anything that is not clearly a number gives None, which the page shows as "No history"."""
    for value in (vm.get("nummer"), vm.get("name")):
        s = str(value or "").replace(" ", "")
        if s.isdigit():
            return str(int(s))
    for value in abschnitt.values():                       # journey id field, whatever bahn.de calls it
        if isinstance(value, str) and (m := ZE_IN_ID.search(value)):
            return str(int(m.group(1)))
    if product in LONG_DISTANCE_PRODUCTS:                  # "ICE 1096": the digits are the number
        m = re.search(r"(\d+)\s*$", str(vm.get("mittelText") or vm.get("name") or ""))
        if m:
            return str(int(m.group(1)))
    return None
    
# ------------------------------------------------------------------------- legs
def normalize_leg(abschnitt: dict) -> dict:
    vm = abschnitt.get("verkehrsmittel") or {}
    dep = abschnitt.get("abfahrt") or {}
    arr = abschnitt.get("ankunft") or {}
    number = train_number(abschnitt, vm, vm.get("produktGattung"))
    leg = {
        "walking": vm.get("typ") != "PUBLICTRANSPORT",
        "name": vm.get("mittelText") or vm.get("name"),
        "number": number,
        "product": vm.get("produktGattung"),
        "origin": {"extId": abschnitt.get("abfahrtsOrtExtId"), "name": abschnitt.get("abfahrtsOrt")},
        "destination": {"extId": abschnitt.get("ankunftsOrtExtId"), "name": abschnitt.get("ankunftsOrt")},
        "planned_departure": dep.get("sollzeit"),
        "planned_arrival": arr.get("sollzeit"),
        "cancelled": bool(abschnitt.get("originCancelled") or abschnitt.get("destinationCancelled")),
    }
    # live times are only passed on where they deviate from the schedule
    if dep.get("echtzeit") and dep["echtzeit"] != dep.get("sollzeit"):
        leg["live_departure"] = dep["echtzeit"]
    if arr.get("echtzeit") and arr["echtzeit"] != arr.get("sollzeit"):
        leg["live_arrival"] = arr["echtzeit"]
    return leg


DT_COVERED_CODE = "MDA-NUR-DT"     # bahn.de message: the whole connection is covered by the Deutschlandticket


def dticket_covered(verbindung: dict) -> bool:
    """True when bahn.de marks the connection as fully covered by the Deutschlandticket.
    bahn.de sends the marker only when the search declared a ticket (deutschlandTicketVorhanden),
    and such connections carry no price. Field names as used by the reference app
    (meldungenAsObject[].code); still worth confirming on one real response."""
    return any(m.get("code") == DT_COVERED_CODE for m in verbindung.get("meldungenAsObject") or [])


def price_of(verbindung: dict) -> dict | None:
    """The 'from X EUR' price of a whole journey, when the response carries one.
    Prices belong to the journey, not to a single train, and exist only for current and
    future days. angebotsPreis.betrag is the field the reference app reads; the currency
    key (waehrung) is a guess. Anything else -> None."""
    p = verbindung.get("angebotsPreis") or {}
    amount = p.get("betrag")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    return {"amount": round(float(amount), 2), "currency": p.get("waehrung") or "EUR", "from": True}


def cancelled_upstream(verbindung: dict) -> bool:
    """bahn.de's greyed-out 'Reise nicht möglich' connections."""
    if any(n.get("key") == "text.realtime.connection.cancelled"
           for n in verbindung.get("risNotizen") or []):
        return True
    return any(a.get("originCancelled") or a.get("destinationCancelled")
               for a in verbindung.get("verbindungsAbschnitte") or []
               if (a.get("verkehrsmittel") or {}).get("typ") == "PUBLICTRANSPORT")


def attach_history(leg: dict, store: ds.DelayStore, window: int, cur) -> None:
    """leg['history'] = DelayStore.leg_stats(...) or None (no history / not tracked)."""
    leg["history"] = None
    if (leg["walking"] or leg["product"] in UNTRACKED_PRODUCTS or not leg["number"]
            or not leg["planned_arrival"] or not leg["destination"]["extId"]):
        return
    leg["history"] = store.leg_stats(
        leg["number"], str(leg["destination"]["extId"]).rjust(8, "0"),
        to_berlin_naive(leg["planned_arrival"]),
        train_type=None, window=window, cur=cur,
        tolerance_min=TOL_LONG if leg["product"] in LONG_DISTANCE_PRODUCTS else TOL_SHORT)


def delay_badge(stats: dict | None, window: int) -> dict:
    """What the card shows for one leg. Never hidden by sample size."""
    if stats is None:
        return {"state": "no_history", "text": "No history"}
    flags = list(stats["badges"])                       # e.g. ["High cancellation"]
    if stats["median_delay"] is None:                   # every matched day was cancelled
        return {"state": "all_cancelled", "text": "–", "flags": flags,
                "runs": stats["days_matched"], "window": window}
    return {"state": "ok", "text": f"+{stats['median_delay']} min",
            "median": stats["median_delay"], "red": stats["is_delayed"],   # red >= 6, else gray
            "runs": stats["days_matched"], "window": window,               # shown as (7/7 days)
            "flags": flags}


# --------------------------------------------------------------------- transfers
def _walk_minutes(legs: list[dict], a: int, b: int) -> float:
    return sum(_minutes(to_berlin_naive(w["planned_departure"]), to_berlin_naive(w["planned_arrival"]))
               for w in legs[a + 1:b]
               if w["walking"] and w["planned_arrival"] and w["planned_departure"])


def transfers(legs: list[dict]) -> list[dict]:
    """One entry per train-to-train transfer.

    planned_minutes / effective_minutes are the transfer time the way bahn.de shows it: the whole
    gap between the first train's arrival and the next train's departure, walking included.
    walk_minutes is the walk inside that gap (0 when there is none).

    Risk is judged on what is left AFTER the walk (slack_minutes): you still have to walk once
    the train has arrived. 'Tight transfer' = that slack, planned OR real-time, under 5 minutes."""
    idx = [i for i, l in enumerate(legs) if not l["walking"]]
    out = []
    for a, b in zip(idx, idx[1:]):
        prev, nxt = legs[a], legs[b]
        if not prev["planned_arrival"] or not nxt["planned_departure"]:
            continue
        gap = _minutes(to_berlin_naive(prev["planned_arrival"]), to_berlin_naive(nxt["planned_departure"]))
        walk = _walk_minutes(legs, a, b)
        slack = gap - walk
        d1 = (round(_minutes(to_berlin_naive(prev["planned_arrival"]),
                             to_berlin_naive(prev["live_arrival"])))
              if prev.get("live_arrival") else None)
        d2 = (round(_minutes(to_berlin_naive(nxt["planned_departure"]),
                             to_berlin_naive(nxt["live_departure"])))
              if nxt.get("live_departure") else 0)
        history = ((prev.get("history") or {}).get("delays_for_miss_probability")) or []
        status = ds.connection_status(round(slack), d1, d2, history)
        eff_slack = status["effective_transfer_min"]            # after the real-time delay, when known
        out.append({
            "station": prev["destination"]["name"],
            "from_leg": a, "to_leg": b,
            "planned_minutes": round(gap),                      # as bahn.de shows it (walk included)
            "effective_minutes": eff_slack + round(walk),       # same, after the real-time delay
            "walk_minutes": round(walk),
            "slack_minutes": round(slack),                      # what is left after the walk
            "effective_slack_minutes": eff_slack,
            "tight": min(slack, eff_slack) < TIGHT_TRANSFER_MIN,
            "likely_missed": status["status"] == "likely_missed",
            "p_miss": status["p_miss"],                         # history: arrival delay > slack, None when unknown
        })
    return out

# ---------------------------------------------------------------------- assemble
def assemble(response: dict, store: ds.DelayStore, window: int = 7, dticket: str = "off") -> dict:
    """dticket: the mode the search ran in - "off" (no ticket), "only" (bahn.de returned only
    Deutschlandticket-valid connections) or "all" (every connection, ticket declared: covered
    ones are flagged, partly covered ones repriced for the paid legs only)."""
    cur = store.cursor()                    # one cursor per request (connections aren't thread-safe)
    journeys = []
    for v in response.get("verbindungen") or []:
        if cancelled_upstream(v):
            continue
        legs = [normalize_leg(a) for a in v.get("verbindungsAbschnitte") or []]
        train_legs = [l for l in legs if not l["walking"]]
        if not train_legs:
            continue
        for l in train_legs:
            attach_history(l, store, window, cur)
        tr = transfers(legs)
        final = train_legs[-1].get("history")
        journeys.append({
            "departure": train_legs[0]["planned_departure"],
            "arrival": train_legs[-1]["planned_arrival"],
            "duration_seconds": v.get("verbindungsDauerInSeconds"),
            "transfer_count": v.get("umstiegsAnzahl", len(train_legs) - 1),
            "legs": [{
                "name": l["name"], "origin": l["origin"]["name"], "destination": l["destination"]["name"],
                "planned_departure": l["planned_departure"], "planned_arrival": l["planned_arrival"],
                "live_departure": l.get("live_departure"), "live_arrival": l.get("live_arrival"),
                "badge": delay_badge(l["history"], window),
                # one bar per matched day; days without a record are simply absent
                "bars": (l["history"] or {}).get("days", []),
            } for l in train_legs],
            "price": price_of(v),                              # None when bahn.de gave none
            # covered journeys come back without a price; the page shows "Included" instead
            "dticket_covered": dticket_covered(v),
            # partly covered: bahn.de already dropped the covered legs, so the price is for the rest
            "price_partial": dticket == "all" and bool(v.get("hasTeilpreis")) and not dticket_covered(v),
            "transfers": tr,
            "tight_transfer": any(t["tight"] for t in tr),     # sign shown before the delay badge
            # headline = median arrival delay at the user's destination (final leg)
            "delay_score": final["median_delay"] if final else None,
        })
    journeys.sort(key=lambda j: j["departure"] or "")          # departure time order, whatever order the source used
    ref = response.get("verbindungReference") or {}
    return {"journeys": journeys, "earlier_ref": ref.get("earlier"), "later_ref": ref.get("later")}
