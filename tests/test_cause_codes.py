from delay_history import cause_codes as cc


def test_known_and_unknown_codes():
    assert cc.cause_text(43) == "Delay of a train ahead" and cc.cause_category(43) == cc.KNOCK_ON
    assert cc.cause_text(34) == "Signal defect" and cc.cause_category(34) == cc.INFRA
    assert cc.cause_text(None) is None and cc.cause_text(9999) is None     # never invented


def test_every_category_has_a_label_and_top_codes_are_covered():
    assert {c for _, c in cc.CAUSES.values()} <= set(cc.CATEGORY_LABELS)
    for code in (43, 48, 45, 31, 34, 38, 36, 44, 21, 64):                  # top codes in the real data
        assert code in cc.CAUSES
