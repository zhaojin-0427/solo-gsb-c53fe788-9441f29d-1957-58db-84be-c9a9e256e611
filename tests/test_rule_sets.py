"""Traceable rule sets: drafts, optimistic versions, ppm normalization,
overlap/conflict detection, immutable publishing and digest addressing."""


def _rule(code, allergen="gluten", threshold=0.0, unit="ppm",
          action="declare", applies_to="contains"):
    return {
        "code": code,
        "allergen": allergen,
        "threshold": threshold,
        "unit": unit,
        "action": action,
        "applies_to": applies_to,
    }


def _create(client, code="eu-2026", jurisdiction="EU", name=None):
    return client.post(
        "/rule-sets",
        json={"code": code, "jurisdiction": jurisdiction, "name": name},
    )


def _submit(client, code, rules, base_version):
    return client.put(
        f"/rule-sets/{code}", json={"base_version": base_version, "rules": rules}
    )


def _publish(client, code, base_version):
    return client.post(
        f"/rule-sets/{code}/publish", json={"base_version": base_version}
    )


def test_create_empty_draft_starts_at_base_version_zero(client):
    resp = _create(client, name="EU 2026 pack")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == "eu-2026"
    assert body["jurisdiction"] == "EU"
    assert body["base_version"] == 0
    assert body["published_version"] is None
    assert body["rules"] == []


def test_duplicate_rule_set_code_rejected(client):
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409
    assert client.post(
        "/rule-sets", json={"code": "EU-2026", "jurisdiction": "US"}
    ).status_code == 409


def test_submit_normalizes_thresholds_to_ppm_and_bumps_version(client):
    _create(client)
    resp = _submit(
        client,
        "eu-2026",
        [
            _rule("r-gluten", threshold=20, unit="mg/kg"),       # == 20 ppm
            _rule("r-tree-nuts", threshold=0.0025, unit="%"),    # == 25 ppm
            _rule("r-milk", threshold=10000, unit="ppb"),        # == 10 ppm
        ],
        base_version=0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["base_version"] == 1
    ppm = {r["code"]: r["threshold_ppm"] for r in body["rules"]}
    assert ppm == {"r-gluten": 20.0, "r-tree-nuts": 25.0, "r-milk": 10.0}
    # original submission preserved for traceability, output sorted by code
    assert body["rules"][0]["unit"] == "mg/kg"
    assert [r["code"] for r in body["rules"]] == [
        "r-gluten",
        "r-milk",
        "r-tree-nuts",
    ]


def test_stale_base_version_returns_409_with_current_version(client):
    _create(client)
    assert _submit(client, "eu-2026", [_rule("r1")], 0).status_code == 200
    stale = _submit(client, "eu-2026", [_rule("r2")], 0)
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "VERSION_CONFLICT"
    assert detail["current_version"] == 1
    # rejected write left no trace
    draft = client.get("/rule-sets/eu-2026").json()
    assert draft["base_version"] == 1
    assert [r["code"] for r in draft["rules"]] == ["r1"]


def test_submit_with_wrong_version_negative_rejected(client):
    _create(client)
    resp = client.put(
        "/rule-sets/eu-2026", json={"base_version": -1, "rules": []}
    )
    assert resp.status_code == 422


def test_duplicate_rule_code_in_one_submission_rejected(client):
    _create(client)
    resp = _submit(client, "eu-2026", [_rule("dup"), _rule("DUP", threshold=1)], 0)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "DUPLICATE_RULE_CODE"
    # nothing written; base version unchanged
    assert client.get("/rule-sets/eu-2026").json()["base_version"] == 0


def test_declare_and_warning_may_coexist_at_same_boundary(client):
    _create(client)
    _submit(
        client,
        "eu-2026",
        [
            _rule("g-declare", action="declare"),
            _rule("g-warn", action="warning"),
        ],
        0,
    )
    resp = _publish(client, "eu-2026", 1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["rule_set_code"] == "eu-2026"
    assert len(body["digest"]) == 64
    assert body["summary"] == {
        "rule_count": 2,
        "actions": {"declare": 1, "warning": 1},
        "applies_to": {"contains": 2},
        "allergens": ["gluten"],
    }
    assert len(body["rules"]) == 2


def test_prohibit_conflicts_with_declare_and_warning(client):
    _create(client)
    _submit(
        client,
        "eu-2026",
        [
            _rule("g-declare", threshold=20, action="declare"),
            _rule("g-prohibit", threshold=20, action="prohibit"),
            _rule("g-warn", threshold=20, action="warning"),
            # different allergen / applies_to / threshold: no overlap, no conflict
            _rule("m-declare", allergen="milk", threshold=20, action="declare"),
            _rule("g-cc", threshold=20, action="prohibit",
                  applies_to="cross_contact"),
            _rule("g-other", threshold=21, action="prohibit"),
        ],
        0,
    )
    resp = _publish(client, "eu-2026", 1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "RULE_SET_CONFLICT"
    # exactly the two prohibit-vs-other pairs, stably ordered by pair of codes
    assert [(c["rules"][0]["code"], c["rules"][1]["code"]) for c in detail["conflicts"]] == [
        ("g-declare", "g-prohibit"),
        ("g-prohibit", "g-warn"),
    ]
    first = detail["conflicts"][0]
    assert first["allergen"] == "gluten"
    assert first["applies_to"] == "contains"
    assert first["threshold_ppm"] == 20.0
    # no version created
    assert client.get("/rule-sets/eu-2026/versions").json() == []


def test_unit_normalized_overlap_counts_as_conflict(client):
    # 20 mg/kg == 20 ppm and 0.002% == 20 ppm: same boundary despite units
    _create(client)
    _submit(
        client,
        "eu-2026",
        [
            _rule("r1", threshold=20, unit="mg/kg", action="declare"),
            _rule("r2", threshold=0.002, unit="%", action="prohibit"),
        ],
        0,
    )
    resp = _publish(client, "eu-2026", 1)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RULE_SET_CONFLICT"
    assert len(resp.json()["detail"]["conflicts"]) == 1


def test_identical_action_overlap_is_not_a_conflict(client):
    _create(client)
    _submit(
        client,
        "eu-2026",
        [
            _rule("p1", threshold=20, action="prohibit"),
            _rule("p2", threshold=20, action="prohibit"),
        ],
        0,
    )
    assert _publish(client, "eu-2026", 1).status_code == 200


def test_conflicts_are_sorted_by_allergen_applies_threshold(client):
    _create(client)
    _submit(
        client,
        "eu-2026",
        [
            _rule("z-pro", allergen="wheat", threshold=10, action="prohibit"),
            _rule("a-dec", allergen="wheat", threshold=10, action="declare"),
            _rule("y-pro", allergen="barley", threshold=5, action="prohibit"),
            _rule("b-dec", allergen="barley", threshold=5, action="declare"),
        ],
        0,
    )
    resp = _publish(client, "eu-2026", 1)
    assert resp.status_code == 409
    keys = [(c["allergen"], c["applies_to"], c["threshold_ppm"])
            for c in resp.json()["detail"]["conflicts"]]
    assert keys == [("barley", "contains", 5.0), ("wheat", "contains", 10.0)]


def test_empty_draft_cannot_be_published(client):
    _create(client)
    resp = _publish(client, "eu-2026", 0)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "EMPTY_RULE_SET"


def test_publish_creates_immutable_version_and_summary_endpoints(client):
    _create(client)
    _submit(client, "eu-2026", [_rule("r1", threshold=20, action="prohibit")], 0)
    pub = _publish(client, "eu-2026", 1)
    assert pub.status_code == 200
    v1 = pub.json()

    # draft kept evolving; its base version advanced past the release
    draft = client.get("/rule-sets/eu-2026").json()
    assert draft["base_version"] == 2
    assert draft["published_version"] == 1

    # version listing is summary-only; version GET returns the full snapshot
    listing = client.get("/rule-sets/eu-2026/versions").json()
    assert listing == [
        {
            "rule_set_code": "eu-2026",
            "jurisdiction": "EU",
            "version": 1,
            "digest": v1["digest"],
            "published_at": listing[0]["published_at"],
            "summary": v1["summary"],
        }
    ]
    snapshot = client.get("/rule-sets/eu-2026/versions/1").json()
    assert snapshot["rules"][0]["threshold_ppm"] == 20.0

    # unknown / unpublished versions
    assert client.get("/rule-sets/eu-2026/versions/9").status_code == 404
    assert client.get("/rule-sets/ghost/versions/1").status_code == 404


def test_publish_stale_base_version_conflicts(client):
    _create(client)
    _submit(client, "eu-2026", [_rule("r1", action="prohibit")], 0)
    stale = _publish(client, "eu-2026", base_version=0)
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "VERSION_CONFLICT"
    assert detail["current_version"] == 1
    assert client.get("/rule-sets/eu-2026/versions").json() == []


def test_successive_publishes_get_incrementing_versions_and_digest_changes(client):
    _create(client)
    _submit(client, "eu-2026", [_rule("r1", threshold=20, action="prohibit")], 0)
    v1 = _publish(client, "eu-2026", 1).json()
    assert v1["version"] == 1

    # base_version is now 2; replace draft with new rules
    upd = _submit(
        client, "eu-2026", [_rule("r1", threshold=25, action="prohibit")], 2
    )
    assert upd.status_code == 200
    assert upd.json()["base_version"] == 3
    v2 = _publish(client, "eu-2026", 3).json()
    assert v2["version"] == 2
    assert v2["digest"] != v1["digest"]

    # v1 snapshot stays immutable at 20 ppm
    snap1 = client.get("/rule-sets/eu-2026/versions/1").json()
    assert snap1["rules"][0]["threshold_ppm"] == 20.0


def test_identical_content_republished_has_same_digest(client):
    _create(client)
    _submit(client, "eu-2026", [_rule("r1", threshold=20, action="prohibit")], 0)
    v1 = _publish(client, "eu-2026", 1).json()
    # identical rules resubmitted (draft content unchanged) -> same digest
    assert _submit(client, "eu-2026",
                   [_rule("r1", threshold=20, action="prohibit")], 2).status_code == 200
    v2 = _publish(client, "eu-2026", 3).json()
    assert v2["digest"] == v1["digest"]
    # threshold-unit-independent: 0.002% == 20 ppm
    assert _submit(client, "eu-2026",
                   [_rule("r1", threshold=0.002, unit="%", action="prohibit")],
                   4).status_code == 200
    v3 = _publish(client, "eu-2026", 5).json()
    assert v3["digest"] == v1["digest"]


def test_rule_set_list_filter_by_jurisdiction(client):
    _create(client, "eu-pack", "EU")
    _create(client, "us-pack", "US")
    assert [r["code"] for r in client.get("/rule-sets?jurisdiction=eu").json()] == [
        "eu-pack"
    ]
    assert {r["code"] for r in client.get("/rule-sets").json()} == {
        "eu-pack",
        "us-pack",
    }


def test_concurrent_submits_only_one_wins_no_lost_update(client):
    """Two sessions racing on base_version=0: exactly one CAS lands.

    Exercises the compare-and-swap directly: the loser's UPDATE matches zero
    rows and must not bump the version or overwrite the rules.
    """
    from app.db import get_db
    from app.models import RuleSet
    from app.routers.rule_sets import _cas_advance

    _create(client)
    override = client.app.dependency_overrides[get_db]
    db1 = next(override())
    db2 = next(override())
    try:
        rs1 = db1.query(RuleSet).filter(RuleSet.code == "eu-2026").first()
        rs2 = db2.query(RuleSet).filter(RuleSet.code == "eu-2026").first()

        # both sessions believe they are on base_version 0
        assert _cas_advance(db1, rs1, 0, draft_rules=[{"code": "a"}]) is True
        db1.commit()
        assert _cas_advance(db2, rs2, 0, draft_rules=[{"code": "b"}]) is False
        db2.rollback()
    finally:
        db1.close()
        db2.close()

    draft = client.get("/rule-sets/eu-2026").json()
    assert draft["base_version"] == 1
    assert draft["rules"] == [{"code": "a"}]
