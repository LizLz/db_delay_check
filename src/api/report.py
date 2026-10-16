"""report.py - the short written summary of the two home-page charts.

Facts first, words second: `build_facts` turns the two chart results into a small dict of numbers;
an LLM (DeepSeek through OpenRouter) may only rephrase them; `numbers_ok` rejects any text that
contains a number that is not in the facts; the template text is the fallback, so the page always
has a correct report, with or without a key or a network.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

from delay_history import cause_codes as cc

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-chat"
RETRY_AFTER_FAILURE_S = 15 * 60            # a failed AI call is retried after this, not kept all day
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

SYSTEM_PROMPT = """You write the summary for a public web page about Deutsche Bahn arrival delays. \
The user message is JSON with the facts of two charts, time patterns and the recorded delay causes. \
'Delayed' means arriving 6 minutes or more late.
Write exactly three paragraphs in plain English for non-experts, separated by a line that contains only --- \
Be concrete and specific; each paragraph has 4 to 6 sentences.
Paragraph 1 (when are trains late): name the period; the busiest hour windows for delays, using worst_hours \
(name each window as given, such as 'between 17:00 and 17:59', with its delayed_percent) and the calmest window \
best_hour; weekday against weekend (delayed_percent and median for each) and the worst and best weekday; \
rush hours against the other hours; then which train type has the highest peak, at what hour and how many \
minutes (a median is the typical delay; say that once), and which type peaks lowest. Do not mention any median \
across all trains by hour, only the shares of late arrivals.
Paragraph 2 (do delays repeat): name the two months and how many trains were compared. Say what share of the \
trains that were late in the earlier month were late again in the later month (late_again_percent). Then say \
whether the delays got shorter or longer: compare late_before_median_min with late_after_median_min and give \
shorter_percent, the share of those trains whose delay was shorter. Give the correlation value and explain in \
one plain clause that it shows how strongly a delay in the earlier month predicts one in the later month.
Paragraph 3 (recorded causes): say that Deutsche Bahn records a cause for only coverage_percent of delayed \
arrivals, so this is a partial picture. Say knock_on_percent of the recorded causes are knock-on delays \
(an earlier delay spreading to later trains), then name every top_other entry with its percentage, and say \
that weather accounts for weather_percent of the recorded causes. Do not claim anything about delays that \
have no recorded cause, and do not say that weather data is missing.
Rules: use ONLY numbers that appear in the JSON, written exactly as given; write hour windows as given; never \
write any other number (no scale like 'between 0 and 1', no counts of things, no years); units are minutes unless \
a field says percent; do not predict, give advice, or guess at causes that are not in the JSON; no lists, \
no headings; never write the word 'median' for all trains together."""


def load_dotenv(path: str | Path = ".env") -> None:
    """Tiny .env reader (KEY=value lines); never overrides a variable that is already set."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in "\"'" and v[-1:] == v[:1] and len(v) > 1:
            v = v[1:-1]                                    # quoted: keep everything inside
        else:
            v = v.split(" #", 1)[0].strip()                # unquoted: "bahn   # comment" -> "bahn"
        os.environ.setdefault(k.strip(), v)


def _month_name(ym: str) -> str:
    return MONTHS[int(ym[5:7]) - 1]


def build_facts(overview: dict, persistence: dict | None, causes: dict | None = None,
                patterns: dict | None = None) -> dict:
    """Only numbers that are really on the two charts."""
    facts: dict = {}
    allser = [(h, p["median"]) for h, p in enumerate(overview["series"]["All"]) if p]
    if allser:
        worst, best = max(allser, key=lambda t: t[1]), min(allser, key=lambda t: t[1])
        facts["overview"] = {
            "period_days": (date.fromisoformat(overview["end"]) - date.fromisoformat(overview["start"])).days + 1,
            "worst_hour_label": f"{worst[0]:02d}:00", "worst_hour": worst[0], "worst_hour_median_min": worst[1],
            "best_hour_label": f"{best[0]:02d}:00", "best_hour": best[0], "best_hour_median_min": best[1],
        }
        types = {}
        for t, pts in overview["series"].items():
            if t == "All":
                continue
            ps = [(h, p["median"]) for h, p in enumerate(pts) if p]
            if ps:
                h, m = max(ps, key=lambda x: x[1])
                types[t] = {"type": t, "peak_hour_label": f"{h:02d}:00", "peak_hour": h, "peak_median_min": m}
        if types:
            top = max(types, key=lambda t: types[t]["peak_median_min"])
            low = min(types, key=lambda t: types[t]["peak_median_min"])
            facts["overview"]["by_train_type"] = list(types.values())
            facts["overview"]["lowest_peak_type"] = low
            facts["overview"]["highest_peak_type"] = top
            facts["overview"]["highest_peak_type_hour"] = types[top]["peak_hour"]
            facts["overview"]["highest_peak_type_median_min"] = types[top]["peak_median_min"]
    if persistence:
        facts["persistence"] = {
            "earlier_month": _month_name(persistence["x_month"]), "later_month": _month_name(persistence["y_month"]),
            "trains_compared": persistence["n_trains"],
            "correlation": persistence["pearson_r"], "slope": persistence["slope"],
            "late_again_percent": None if persistence["repeat_rate"] is None else round(persistence["repeat_rate"] * 100),
            "late_threshold_min": 6,
        }
        if persistence.get("late_median_before") is not None:
            facts["persistence"].update({
                "late_before_median_min": persistence["late_median_before"],
                "late_after_median_min": persistence["late_median_after"],
                "shorter_percent": None if persistence.get("shorter_rate") is None else round(persistence["shorter_rate"] * 100),
            })
    if patterns:
        facts["time_patterns"] = patterns
    c = _cause_facts(causes)
    if c:
        facts["causes"] = c
    return facts


def _cause_facts(c: dict | None) -> dict | None:
    """Shares of the RECORDED causes among delayed arrivals. Knock-on causes (a train ahead was late,
    ...) say how a delay spread, not where it began, so they are one share of their own and are kept
    out of the top list; the top list names the three most common specific causes."""
    if not c or not c.get("codes") or not c.get("delayed"):
        return None
    coded = sum(n for code, n in c["codes"].items() if cc.cause_category(code))
    if not coded:
        return None
    knock = weather = 0
    by_text: Counter = Counter()
    for code, n in c["codes"].items():
        cat = cc.cause_category(code)
        if cat is None:
            continue
        if cat == cc.KNOCK_ON:
            knock += n
        else:
            by_text[cc.cause_text(code)] += n
            if cat == cc.WEATHER:
                weather += n
    pct = lambda n: round(100 * n / coded)
    return {"days": c["days"], "coverage_percent": round(100 * sum(c["codes"].values()) / c["delayed"]),
            "knock_on_percent": pct(knock), "weather_percent": pct(weather),
            "top_other": [{"cause": t, "percent": pct(n)} for t, n in by_text.most_common(3)]}


def _patterns_sentence(t: dict | None) -> str:
    if not t or not t.get("worst_hours"):
        return ""
    w = t["worst_hours"][0]
    out = (f" Measured by the share of arrivals 6+ min late, the worst hour is {w['window'].replace('-', ' to ')} "
           f"({w['delayed_percent']}%).")
    if t.get("weekday_delayed_percent") is not None and t.get("weekend_delayed_percent") is not None:
        out += (f" {t['weekday_delayed_percent']}% of weekday arrivals and {t['weekend_delayed_percent']}% of weekend "
                f"arrivals are late, {t['rush_delayed_percent']}% during rush hours and {t['other_hours_delayed_percent']}% otherwise.")
    return out


def template_text(facts: dict) -> str:
    """The fixed fallback text: one paragraph per chart plus one for the recorded causes."""
    parts = []
    o = facts.get("overview")
    if o:
        parts.append(
            (f"Over the last {o['period_days']} days, trains were latest at {o['worst_hour_label']} "
             f"(median {o['worst_hour_median_min']:g} min) and most punctual at {o['best_hour_label']} "
             f"(median {o['best_hour_median_min']:g} min). " if not facts.get("time_patterns") else
             f"Over the last {o['period_days']} days, ")
            + f"the highest hourly median of any train type was {o['highest_peak_type']}, "
            f"{o['highest_peak_type_median_min']:g} min at {o['highest_peak_type_hour']:02d}:00."
            + _patterns_sentence(facts.get("time_patterns")))
    p = facts.get("persistence")
    if p:
        late = ""
        if p["late_again_percent"] is not None:
            late = (f" Of the trains that were {p['late_threshold_min']}+ min late in {p['earlier_month']}, "
                    f"{p['late_again_percent']}% were late again in {p['later_month']}.")
        if "late_before_median_min" in p:
            late += (f" Their typical delay went from {p['late_before_median_min']:g} min to "
                     f"{p['late_after_median_min']:g} min, and {p['shorter_percent']}% of them were delayed less.")
        parts.append(
            f"Comparing {p['trains_compared']:,} trains between {p['earlier_month']} and {p['later_month']}, "
            f"delays in the two months are linked (correlation {p['correlation']:g}).{late}")
    c = facts.get("causes")
    if c:
        tops = ", ".join(f"{t['cause'].lower()} ({t['percent']}%)" for t in c["top_other"])
        parts.append(
            f"Deutsche Bahn records a cause for only {c['coverage_percent']}% of delayed arrivals. Of those, "
            f"{c['knock_on_percent']}% are knock-on delays (an earlier delay spreading to later trains)"
            f"{'; the next largest are ' + tops if tops else ''}. Weather accounts for {c['weather_percent']}%.")
    return "\n\n".join(parts) or "Not enough data for a report yet."


def _numbers(text: str) -> set[float]:
    out = set()
    for m in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.add(float(m.replace(",", "").rstrip(".")))
        except ValueError:
            pass
    return out


def numbers_ok(text: str, facts: dict) -> bool:
    """Every number in the text must be a number from the facts (a '17:00' contributes 17 and 0)."""
    allowed = _numbers(json.dumps(facts)) | {0.0}
    return _numbers(text) <= allowed


def _post(body: dict, key: str) -> dict:
    req = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def ask_model(facts: dict, key: str, model: str, post=_post) -> str | None:
    body = {"model": model, "temperature": 0.2, "max_tokens": 1000,
            "reasoning": {"enabled": False},        # reasoning models are slow and can use all tokens thinking
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": json.dumps(facts)}]}
    try:
        choice = post(body, key)["choices"][0]
        text = (choice["message"].get("content") or "").strip()
    except Exception as e:                                   # network, HTTP error, odd answer: fall back
        log.warning("report: model call failed: %s", e)
        return None
    if not text:
        log.warning("report: model returned no text (finish_reason=%s)", choice.get("finish_reason"))
        return None
    text = split_paragraphs(text)
    if not numbers_ok(text, facts):
        log.warning("report: model text rejected, a number is not in the facts: %s", text)
        return None
    return text


def split_paragraphs(text: str) -> str:
    """Models separate paragraphs with '---', a blank line, or (badly) not at all. Return the text with
    blank lines between paragraphs; one long block is cut into three groups of sentences."""
    parts = [p.strip() for p in re.split(r"\n\s*-{3,}\s*\n|\n\s*\n", text.strip()) if p.strip()]
    if len(parts) == 1:
        parts = [p.strip() for p in parts[0].split("\n") if p.strip()]
    if len(parts) == 1:
        sentences = re.split(r"(?<=[.!?])\s+", parts[0])
        if len(sentences) >= 6:
            k = -(-len(sentences) // 3)
            parts = [" ".join(sentences[i:i + k]) for i in range(0, len(sentences), k)]
    return "\n\n".join(parts)


class ReportService:
    """One report per data day, kept in memory and (if cache_path is given) on disk, so a server restart
    does not pay for another AI call. A failed AI call is retried after 15 minutes, not kept all day."""
    def __init__(self, post=_post, clock=time.monotonic, cache_path: str | Path | None = None):
        self._post, self._clock = post, clock
        self._path = Path(cache_path) if cache_path else None
        self._lock = threading.Lock()
        self._day = None
        self._value: dict | None = None
        self._expires: float | None = None

    def _load_disk(self, data_day):
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            if d.get("data_day") == str(data_day) and d.get("source") == "ai" and d.get("text"):
                return d
        except (OSError, ValueError):
            pass
        return None

    def _save_disk(self, value):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(value), encoding="utf-8")
        except OSError as e:
            log.warning("report: could not write the cache file: %s", e)

    def get(self, data_day, make_facts) -> dict:
        with self._lock:
            fresh = self._value is not None and self._day == data_day and (
                self._expires is None or self._clock() < self._expires)
            if fresh:
                return self._value
            if self._path and (hit := self._load_disk(data_day)):
                self._value, self._day, self._expires = hit, data_day, None
                return hit
            facts = make_facts()
            key = os.environ.get("OPENROUTER_API_KEY", "").strip()
            text = ask_model(facts, key, os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL), self._post) if key and facts else None
            self._value = {"text": text or template_text(facts), "source": "ai" if text else "template",
                           "data_day": str(data_day)}
            self._day = data_day
            self._expires = self._clock() + RETRY_AFTER_FAILURE_S if (key and not text) else None
            if text and self._path:
                self._save_disk(self._value)
            return self._value
