from datetime import date

from api import report as rp

OV = {"start": "2026-09-02", "end": "2026-10-01",
      "series": {"All": [{"median": 1.0, "n": 50} if h != 17 else {"median": 4.0, "n": 50} for h in range(24)],
                 "ICE": [None] * 17 + [{"median": 9.0, "n": 50}] + [None] * 6,
                 "IC": [None] * 24, "RE": [None] * 24, "RB": [None] * 24, "S": [None] * 24}}
PE = {"x_month": "2026-08", "y_month": "2026-09", "n_trains": 236630, "pearson_r": 0.746,
      "slope": 0.741, "repeat_rate": 0.6}


def facts():
    return rp.build_facts(OV, PE)


def test_facts_contain_only_chart_numbers():
    f = facts()
    assert f["overview"]["worst_hour"] == 17 and f["overview"]["period_days"] == 30
    assert f["overview"]["highest_peak_type"] == "ICE"
    assert f["persistence"]["late_again_percent"] == 60 and f["persistence"]["later_month"] == "September"


def test_template_passes_its_own_number_check():
    f = facts()
    t = rp.template_text(f)
    assert "236,630" in t and rp.numbers_ok(t, f)


def test_numbers_ok_rejects_foreign_numbers():
    f = facts()
    assert rp.numbers_ok("Trains were latest at 17:00, median 4 min.", f)
    assert not rp.numbers_ok("Trains were 12 min late.", f)


def reply(text):
    return lambda body, key: {"choices": [{"message": {"content": text}}]}


def test_ask_model_accepts_good_text_and_rejects_bad():
    f = facts()
    assert rp.ask_model(f, "k", "m", reply("Latest at 17:00 with a median of 4 min.")) == "Latest at 17:00 with a median of 4 min."
    assert rp.ask_model(f, "k", "m", reply("About 99 min late.")) is None


def test_ask_model_survives_errors():
    def boom(body, key):
        raise OSError("offline")
    assert rp.ask_model(facts(), "k", "m", boom) is None


def test_service_without_key_uses_template_and_caches(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = []
    svc = rp.ReportService()
    def mk():
        calls.append(1); return facts()
    a = svc.get(date(2026, 10, 1), mk)
    b = svc.get(date(2026, 10, 1), mk)
    assert a["source"] == "template" and a is b and len(calls) == 1
    svc.get(date(2026, 10, 2), mk)
    assert len(calls) == 2                       # a new data day builds a new report


def test_service_ai_then_retry_after_failure(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    now = [0.0]
    answers = [OSError("down"), "Latest at 17:00, median 4 min."]
    def post(body, key):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return {"choices": [{"message": {"content": a}}]}
    svc = rp.ReportService(post=post, clock=lambda: now[0])
    d = date(2026, 10, 1)
    assert svc.get(d, facts)["source"] == "template"
    assert svc.get(d, facts)["source"] == "template" and len(answers) == 1   # not retried yet
    now[0] = 16 * 60
    assert svc.get(d, facts)["source"] == "ai"
    now[0] = 10 ** 6
    assert svc.get(d, facts)["source"] == "ai" and not answers               # AI result is kept all day


def test_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text('# c\nOPENROUTER_API_KEY="abc"\n')
    rp.load_dotenv(tmp_path / ".env")
    import os
    assert os.environ["OPENROUTER_API_KEY"] == "abc"


CA = {"days": 30, "delayed": 1000, "codes": {43: 300, 48: 200, 34: 80, 36: 50, 22: 30, 31: 40}}


def test_cause_facts_split_knock_on_and_root_causes():
    f = rp.build_facts(OV, PE, CA)
    c = f["causes"]
    assert c["coverage_percent"] == 70
    assert c["knock_on_percent"] == 71          # 500 of 700
    assert c["weather_percent"] == 4
    assert [t["cause"] for t in c["top_other"]] == ["Signal defect", "Technical fault on the train", "Construction work"]
    t = rp.template_text(f)
    assert "\n\n" in t and "signal defect" not in t.split("\n\n")[0] and rp.numbers_ok(t, f)


def test_no_causes_no_paragraph():
    assert "causes" not in rp.build_facts(OV, PE, {"days": 30, "delayed": 5, "codes": {}})
    assert "causes" not in rp.build_facts(OV, PE)


CAUSES = {"days": 30, "delayed": 1000, "codes": {43: 300, 48: 100, 34: 80, 36: 40, 11: 20, 31: 10}}


def test_cause_facts_split_knock_on_from_root_causes():
    c = rp.build_facts(OV, PE, CAUSES)["causes"]
    assert c["coverage_percent"] == 55
    assert c["knock_on_percent"] == 73          # (300 + 100) / 550
    assert c["weather_percent"] == 4
    assert [t["cause"] for t in c["top_other"]][0] == "Signal defect"


def test_three_paragraphs_and_numbers_check():
    pe = dict(PE, late_median_before=9.0, late_median_after=7.5, shorter_rate=0.62)
    f = rp.build_facts(OV, pe, CAUSES)
    t = rp.template_text(f)
    assert t.count("\n\n") == 2 and "62%" in t and "7.5 min" in t
    assert rp.numbers_ok(t, f)


def test_no_causes_no_third_paragraph():
    assert rp.template_text(rp.build_facts(OV, PE)).count("\n\n") == 1


PAT = {"days": 30, "weekday_delayed_percent": 9, "weekend_delayed_percent": 7, "weekday_median_min": 1.0,
       "weekend_median_min": 0.0, "rush_hours": "06:00-08:59 and 16:00-18:59", "rush_delayed_percent": 11,
       "other_hours_delayed_percent": 8, "worst_weekday": {"name": "Friday", "delayed_percent": 12},
       "best_weekday": {"name": "Saturday", "delayed_percent": 6},
       "worst_hours": [{"window": "17:00-17:59", "delayed_percent": 14}], "best_hour": {"window": "04:00-04:59", "delayed_percent": 3}}


def test_time_patterns_in_facts_and_template():
    f = rp.build_facts(OV, PE, None, PAT)
    assert f["time_patterns"]["worst_weekday"]["name"] == "Friday"
    t = rp.template_text(f)
    assert "17:00 to 17:59" in t and "weekend" in t and rp.numbers_ok(t, f)


def test_split_paragraphs():
    assert rp.split_paragraphs("A one.\n---\nB two.\n---\nC three.") == "A one.\n\nB two.\n\nC three."
    assert rp.split_paragraphs("A.\n\nB.") == "A.\n\nB."
    assert rp.split_paragraphs("A.\nB.\nC.") == "A.\n\nB.\n\nC."
    six = " ".join(f"S{i} here." for i in range(6))
    assert rp.split_paragraphs(six).count("\n\n") == 2


def test_disk_cache_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    path = tmp_path / "c.json"
    calls = []
    def post(body, key):
        calls.append(1)
        return {"choices": [{"message": {"content": "Latest at 17:00, median 4 min."}}]}
    d = date(2026, 10, 1)
    assert rp.ReportService(post=post, cache_path=path).get(d, facts)["source"] == "ai"
    again = rp.ReportService(post=post, cache_path=path).get(d, facts)       # a "restarted" server
    assert again["source"] == "ai" and len(calls) == 1
    assert rp.ReportService(post=post, cache_path=path).get(date(2026, 10, 2), facts)["source"] == "ai" and len(calls) == 2
