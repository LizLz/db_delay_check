"""cause_codes.py - IRIS delay-cause code -> English text + a coarse category.

Source of the code list: the community library Travel::Status::DE::IRIS (derf, v2.05,
`%translation` in IRIS/Result.pm). It is NOT an official DB document; DB can change codes
(that library notes 23 and 81 were deleted as of 2025-12-14). The English wording and the
categories below are ours.

Why categories: the biggest codes in the data (43, 48, 45, 44, 21) are KNOCK-ON causes
("a train in front was late", "late from the previous trip"). They explain how a delay
spread, not where it started. A "top 3 causes" list over raw codes would be those five
every time, so the report should group by category and treat knock-on separately.
"""
from __future__ import annotations

# code: (English text, category)
KNOCK_ON, INFRA, VEHICLE, STAFF, EXTERNAL, WEATHER, PASSENGERS, OTHER = (
    "knock_on", "infrastructure", "vehicle", "staff", "external_incident", "weather",
    "passengers", "other")

CAUSES: dict[int, tuple[str, str]] = {
    1: ("Details to follow", OTHER),
    2: ("Police operation", EXTERNAL),
    3: ("Fire brigade operation on the line", EXTERNAL),
    4: ("Short-notice staff shortage", STAFF),
    5: ("Medical care of a passenger", PASSENGERS),
    6: ("Emergency brake pulled", PASSENGERS),
    7: ("Unauthorised persons on the track", EXTERNAL),
    8: ("Emergency doctor on the line", EXTERNAL),
    9: ("Effects of a strike", STAFF),
    10: ("Animals on the line", EXTERNAL),
    11: ("Severe weather", WEATHER),
    12: ("Waiting for a delayed ship", OTHER),
    13: ("Passport and customs control", OTHER),
    14: ("Defect at the station", INFRA),
    15: ("Vandalism", EXTERNAL),
    16: ("Defusing of a WWII bomb", EXTERNAL),
    17: ("Damaged bridge", INFRA),
    18: ("Fallen tree on the line", WEATHER),
    19: ("Accident at a level crossing", EXTERNAL),
    20: ("Animals on the track", EXTERNAL),
    21: ("Waiting for connecting passengers", KNOCK_ON),
    22: ("Weather-related disruption", WEATHER),
    23: ("Operations stabilisation", OTHER),
    24: ("Delay abroad", KNOCK_ON),
    25: ("Additional coaches being added", VEHICLE),
    26: ("Coaches being removed", VEHICLE),
    27: ("Technical fault on the bus", VEHICLE),
    28: ("Objects on the line", EXTERNAL),
    29: ("Rail replacement bus set up", OTHER),
    30: ("Signal-box staff shortage", STAFF),
    31: ("Construction work", INFRA),
    32: ("Longer dwell time at the station", PASSENGERS),
    33: ("Overhead line defect", INFRA),
    34: ("Signal defect", INFRA),
    35: ("Line closure", INFRA),
    36: ("Technical fault on the train", VEHICLE),
    37: ("Short-notice vehicle failure", VEHICLE),
    38: ("Track defect", INFRA),
    39: ("Congestion / heavy traffic", KNOCK_ON),
    40: ("Signal-box (interlocking) defect", INFRA),
    41: ("Level-crossing defect", INFRA),
    42: ("Unscheduled speed restriction", INFRA),
    43: ("Delay of a train ahead", KNOCK_ON),
    44: ("Waiting for an oncoming train", KNOCK_ON),
    45: ("Another train had priority", KNOCK_ON),
    46: ("Another train had priority", KNOCK_ON),
    47: ("Late provision of the train", KNOCK_ON),
    48: ("Delay from the previous trip", KNOCK_ON),
    49: ("Short-notice staff shortage", STAFF),
    50: ("Short-notice staff illness", STAFF),
    51: ("Staff delayed from a previous trip", KNOCK_ON),
    52: ("Strike", STAFF),
    53: ("Effects of severe weather", WEATHER),
    54: ("Track availability currently limited", INFRA),
    55: ("Technical defect on another train", VEHICLE),
    56: ("Charging the traction battery", VEHICLE),
    57: ("Additional stop", OTHER),
    58: ("Diversion", INFRA),
    59: ("Snow and ice", WEATHER),
    60: ("Reduced speed due to weather", WEATHER),
    61: ("Defective door", VEHICLE),
    62: ("Train defect repaired", VEHICLE),
    63: ("Technical inspection of the train", VEHICLE),
    64: ("Points (switch) defect", INFRA),
    65: ("Landslide", WEATHER),
    66: ("Flooding", WEATHER),
    67: ("Official / authority measure", EXTERNAL),
    68: ("Many passengers, longer boarding", PASSENGERS),
    69: ("Train running at reduced speed", VEHICLE),
    99: ("Delays in operations", OTHER),
}

CATEGORY_LABELS = {
    KNOCK_ON: "Knock-on delays (earlier delays spreading)",
    INFRA: "Infrastructure (track, signals, points, construction)",
    VEHICLE: "Vehicle / train technical",
    STAFF: "Staff availability / strike",
    EXTERNAL: "External incidents (police, animals, obstacles)",
    WEATHER: "Weather",
    PASSENGERS: "Passengers / boarding",
    OTHER: "Other",
}


def cause_text(code: int | None) -> str | None:
    """English wording for a code, or None when there is no cause / the code is unknown
    (never invent a text: unknown codes simply show no cause)."""
    return CAUSES[code][0] if code in CAUSES else None


def cause_category(code: int | None) -> str | None:
    return CAUSES[code][1] if code in CAUSES else None
