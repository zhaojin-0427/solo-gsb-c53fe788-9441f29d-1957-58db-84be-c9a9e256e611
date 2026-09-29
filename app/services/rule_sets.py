"""Traceable rule-set service.

Rules inside a rule set are normalized to ppm on every draft submission, so
overlap detection and published digests are independent of the input unit.

Overlap semantics
-----------------
Two rules overlap when they share ``jurisdiction`` (implicit, the set itself),
``allergen``, ``applies_to`` and a ppm-equal threshold (boundaries count:
``>=``/``<=`` triggers make equal thresholds the same boundary).

- ``declare`` + ``warning`` may coexist.
- ``prohibit`` conflicts with *any* different action.
- Identical-action duplicates are not conflicts (same legal effect).

Conflicts are reported in a stable order and block publishing.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass

from ..schemas import RuleSetRule
from ..units import concentration_to_ppm

# Actions that can coexist on the same boundary.
COMPATIBLE_ACTIONS = {"declare", "warning"}


@dataclass(frozen=True)
class RuleConflict:
    allergen: str
    applies_to: str
    threshold_ppm: float
    rule_codes: tuple[str, str]
    actions: tuple[str, str]
    reason: str

    def as_dict(self) -> dict:
        return {
            "allergen": self.allergen,
            "applies_to": self.applies_to,
            "threshold_ppm": _r6(self.threshold_ppm),
            "rules": [
                {"code": self.rule_codes[0], "action": self.actions[0]},
                {"code": self.rule_codes[1], "action": self.actions[1]},
            ],
            "reason": self.reason,
        }

    def sort_key(self):
        return (
            self.allergen,
            self.applies_to,
            self.threshold_ppm,
            self.rule_codes[0],
            self.rule_codes[1],
        )


def _r6(x: float) -> float:
    return round(float(x), 6)


def normalize_rules(rules: list[RuleSetRule]) -> list[dict]:
    """Validate and convert a submitted rule list to its canonical ppm form.

    Duplicate codes within one submission are rejected. Thresholds are converted
    to ppm and rounded to 6 decimals; the original value/unit are retained for
    traceability. The result is canonically ordered by code.
    """
    seen: set[str] = set()
    normalized: list[dict] = []
    for rule in rules:
        if rule.code in seen:
            raise ValueError(rule.code)
        seen.add(rule.code)
        normalized.append(
            {
                "code": rule.code,
                "allergen": rule.allergen,
                "action": rule.action,
                "applies_to": rule.applies_to,
                "threshold": rule.threshold,
                "unit": rule.unit,
                "threshold_ppm": _r6(concentration_to_ppm(rule.threshold, rule.unit)),
            }
        )
    normalized.sort(key=lambda r: r["code"])
    return normalized


def _actions_conflict(a: str, b: str) -> bool:
    if a == b:
        return False  # same action on the same boundary: redundant, not a conflict
    # At least one side is prohibit; declare/warning pair is the only allowed mix.
    return not ({a, b} <= COMPATIBLE_ACTIONS)


def detect_conflicts(rules: list[dict]) -> list[RuleConflict]:
    """Return all conflicting overlapping pairs in a stable order."""
    groups: dict[tuple[str, str, float], list[dict]] = {}
    for rule in rules:
        key = (rule["allergen"], rule["applies_to"], _r6(rule["threshold_ppm"]))
        groups.setdefault(key, []).append(rule)

    conflicts: list[RuleConflict] = []
    for (allergen, applies_to, threshold_ppm), members in groups.items():
        members = sorted(members, key=lambda r: r["code"])
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                left, right = members[i], members[j]
                if _actions_conflict(left["action"], right["action"]):
                    conflicts.append(
                        RuleConflict(
                            allergen=allergen,
                            applies_to=applies_to,
                            threshold_ppm=threshold_ppm,
                            rule_codes=(left["code"], right["code"]),
                            actions=(left["action"], right["action"]),
                            reason=(
                                "overlapping rules at the same ppm threshold: "
                                "'prohibit' conflicts with any other action"
                            ),
                        )
                    )
    conflicts.sort(key=RuleConflict.sort_key)
    return conflicts


def canonical_rules(rules: list[dict]) -> list[dict]:
    """Project stored rules to the fields that define a published snapshot."""
    return [
        {
            "code": r["code"],
            "allergen": r["allergen"],
            "action": r["action"],
            "applies_to": r["applies_to"],
            "threshold_ppm": _r6(r["threshold_ppm"]),
        }
        for r in sorted(rules, key=lambda r: r["code"])
    ]


def rules_digest(rules: list[dict]) -> str:
    canonical = json.dumps(canonical_rules(rules), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rules_summary(rules: list[dict]) -> dict:
    actions = Counter(r["action"] for r in rules)
    applies = Counter(r["applies_to"] for r in rules)
    return {
        "rule_count": len(rules),
        "actions": {a: actions[a] for a in sorted(actions)},
        "applies_to": {k: applies[k] for k in sorted(applies)},
        "allergens": sorted({r["allergen"] for r in rules}),
    }
