"""Unit tests for rule-set ppm normalization, overlap and conflict logic."""

import pytest

from app.schemas import RuleSetRule
from app.services.rule_sets import detect_conflicts, normalize_rules, rules_digest


def _r(code, allergen="gluten", threshold=0.0, unit="ppm",
       action="declare", applies_to="contains"):
    return RuleSetRule(
        code=code, allergen=allergen, threshold=threshold, unit=unit,
        action=action, applies_to=applies_to,
    )


def test_thresholds_normalized_and_rounded_to_ppm():
    rules = normalize_rules(
        [_r("a", threshold=20, unit="mg/kg"),
         _r("b", threshold=0.002, unit="%"),
         _r("c", threshold=10_000, unit="ppb"),
         _r("d", threshold=1, unit="g/kg")]
    )
    ppm = {r["code"]: r["threshold_ppm"] for r in rules}
    assert ppm == {"a": 20.0, "b": 20.0, "c": 10.0, "d": 1000.0}


def test_duplicate_code_raises():
    with pytest.raises(ValueError):
        normalize_rules([_r("x"), _r("X", threshold=1)])


@pytest.mark.parametrize(
    "actions,expected_conflicts",
    [
        (["declare", "warning"], 0),
        (["warning", "declare"], 0),
        (["prohibit", "declare"], 1),
        (["declare", "prohibit"], 1),
        (["prohibit", "warning"], 1),
        (["warning", "prohibit"], 1),
        (["prohibit", "prohibit"], 0),
        (["declare", "declare"], 0),
    ],
)
def test_conflict_matrix(actions, expected_conflicts):
    rules = normalize_rules(
        [_r(f"r{i}", threshold=20, action=a) for i, a in enumerate(actions)]
    )
    assert len(detect_conflicts(rules)) == expected_conflicts


def test_boundary_equal_thresholds_in_different_units_conflict():
    # 20 ppm == 20 mg/kg == 0.002%: one overlapping group, three pairs
    rules = normalize_rules(
        [
            _r("p", threshold=20, unit="ppm", action="prohibit"),
            _r("m", threshold=20, unit="mg/kg", action="declare"),
            _r("pct", threshold=0.002, unit="%", action="warning"),
        ]
    )
    conflicts = detect_conflicts(rules)
    assert len(conflicts) == 2  # prohibit-declare, prohibit-warning; declare-warning ok
    pairs = [c.rule_codes for c in conflicts]
    assert pairs == [("m", "p"), ("p", "pct")]
    assert all(c.threshold_ppm == 20.0 for c in conflicts)


def test_different_applies_to_do_not_overlap():
    rules = normalize_rules(
        [
            _r("a", threshold=20, action="prohibit", applies_to="contains"),
            _r("b", threshold=20, action="declare", applies_to="cross_contact"),
        ]
    )
    assert detect_conflicts(rules) == []


def test_different_thresholds_do_not_overlap():
    rules = normalize_rules(
        [
            _r("a", threshold=20, action="prohibit"),
            _r("b", threshold=20.000001, action="declare"),
        ]
    )
    assert detect_conflicts(rules) == []


def test_digest_stable_and_unit_independent():
    a = normalize_rules([_r("r", threshold=20, unit="ppm", action="prohibit")])
    b = normalize_rules([_r("r", threshold=20, unit="mg/kg", action="prohibit")])
    assert rules_digest(a) == rules_digest(b)
    c = normalize_rules([_r("r", threshold=21, unit="ppm", action="prohibit")])
    assert rules_digest(a) != rules_digest(c)
