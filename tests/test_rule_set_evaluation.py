"""Evaluation binding to immutable rule-set snapshots and replay semantics."""

from tests.helpers import make_ingredient, make_recipe


RULES_V1 = [
    {"code": "gluten-declare", "allergen": "gluten", "threshold": 0,
     "unit": "ppm", "action": "declare", "applies_to": "contains"},
    {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 20,
     "unit": "ppm", "action": "prohibit", "applies_to": "contains"},
]
RULES_V2 = [
    {"code": "gluten-declare", "allergen": "gluten", "threshold": 0,
     "unit": "ppm", "action": "declare", "applies_to": "contains"},
    # raised threshold above the 100,000 ppm loaf: no longer violates
    {"code": "gluten-prohibit", "allergen": "gluten", "threshold": 100001,
     "unit": "ppm", "action": "prohibit", "applies_to": "contains"},
]


def _publish_set(client, code, rules, jurisdiction="EU"):
    assert client.post(
        "/rule-sets", json={"code": code, "jurisdiction": jurisdiction}
    ).status_code == 201
    assert client.put(
        f"/rule-sets/{code}", json={"base_version": 0, "rules": rules}
    ).status_code == 200
    resp = client.post(
        f"/rule-sets/{code}/publish", json={"base_version": 1}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _loaf(client):
    # 1 kg wheat flour at 10% gluten => 100,000 ppm, total 1000 g
    make_ingredient(
        client,
        "wheat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "%"}],
    )
    make_recipe(
        client,
        "loaf",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "wheat", "quantity": 1, "unit": "kg"}
                ],
            }
        ],
    )


def _eval(client, key, code=None, version=None, jurisdiction="EU"):
    body = {
        "recipe_code": "loaf",
        "recipe_version": 1,
        "jurisdiction": jurisdiction,
        "idempotency_key": key,
    }
    if code is not None:
        body["rule_set_code"] = code
    if version is not None:
        body["rule_set_version"] = version
    return client.post("/evaluations", json=body)


def test_evaluation_uses_bound_rule_set_snapshot(client):
    _loaf(client)
    pub = _publish_set(client, "eu-pack", RULES_V1)
    resp = _eval(client, "e1", "eu-pack", 1)
    assert resp.status_code == 201, resp.text
    result = resp.json()
    assert result["rule_set"] == {
        "code": "eu-pack",
        "version": 1,
        "digest": pub["digest"],
    }
    assert len(result["obligations"]) == 1
    assert len(result["violations"]) == 1
    assert result["violations"][0]["rule_code"] == "gluten-prohibit"
    assert result["violations"][0]["threshold_ppm"] == 20.0


def test_rebound_to_v2_replays_against_new_snapshot(client):
    _loaf(client)
    _publish_set(client, "eu-pack", RULES_V1)
    _eval(client, "e1", "eu-pack", 1)

    # publish v2 with a raised prohibit threshold
    assert client.put(
        "/rule-sets/eu-pack", json={"base_version": 2, "rules": RULES_V2}
    ).status_code == 200
    pub2 = client.post(
        "/rule-sets/eu-pack/publish", json={"base_version": 3}
    ).json()
    assert pub2["version"] == 2

    resp = _eval(client, "e2", "eu-pack", 2)
    result = resp.json()
    assert result["rule_set"]["version"] == 2
    assert len(result["violations"]) == 0
    assert len(result["obligations"]) == 1


def test_snapshot_is_immutable_after_new_rules_published(client):
    _loaf(client)
    v1 = _publish_set(client, "eu-pack", RULES_V1)
    _eval(client, "e1", "eu-pack", 1)
    # publish a different v2; the v1-bound stored evaluation must be unchanged
    assert client.put(
        "/rule-sets/eu-pack", json={"base_version": 2, "rules": RULES_V2}
    ).status_code == 200
    assert client.post(
        "/rule-sets/eu-pack/publish", json={"base_version": 3}
    ).status_code == 200

    replayed = _eval(client, "e1", "eu-pack", 1)
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert replayed.json()["rule_set"]["digest"] == v1["digest"]
    assert len(replayed.json()["violations"]) == 1


def test_missing_or_partial_binding_returns_stable_error(client):
    _loaf(client)
    _publish_set(client, "eu-pack", RULES_V1)

    # code without version
    resp = _eval(client, "e1", code="eu-pack")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "RULE_SET_BINDING_INCOMPLETE"

    # version without code
    resp = _eval(client, "e2", version=1)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "RULE_SET_BINDING_INCOMPLETE"

    # unknown code
    resp = _eval(client, "e3", "ghost", 1)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RULE_SET_NOT_FOUND"

    # known code, unpublished version
    resp = _eval(client, "e4", "eu-pack", 7)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RULE_SET_VERSION_NOT_FOUND"


def test_jurisdiction_mismatch_rejected(client):
    _loaf(client)
    _publish_set(client, "us-pack", RULES_V1, jurisdiction="US")
    resp = _eval(client, "e1", "us-pack", 1, jurisdiction="EU")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "RULE_SET_JURISDICTION_MISMATCH"
    assert detail["rule_set_jurisdiction"] == "US"
    assert detail["requested_jurisdiction"] == "EU"


def test_get_by_evaluation_id_returns_snapshot_reference(client):
    _loaf(client)
    pub = _publish_set(client, "eu-pack", RULES_V1)
    created = _eval(client, "e1", "eu-pack", 1).json()
    got = client.get(f"/evaluations/{created['evaluation_id']}").json()
    assert got["rule_set"]["digest"] == pub["digest"]
    assert got["recipe"] == {"code": "loaf", "version": 1}


def test_filter_evaluations_by_rule_set_version(client):
    _loaf(client)
    _publish_set(client, "eu-pack", RULES_V1)
    assert client.put(
        "/rule-sets/eu-pack", json={"base_version": 2, "rules": RULES_V2}
    ).status_code == 200
    client.post("/rule-sets/eu-pack/publish", json={"base_version": 3})
    _eval(client, "e1", "eu-pack", 1)
    _eval(client, "e2", "eu-pack", 2)

    v1_only = client.get(
        "/evaluations?rule_set_code=eu-pack&rule_set_version=1"
    ).json()
    assert len(v1_only) == 1
    assert v1_only[0]["rule_set"]["version"] == 1

    code_only = client.get("/evaluations?rule_set_code=eu-pack").json()
    assert {e["rule_set"]["version"] for e in code_only} == {1, 2}

    # version filter without code -> stable 400
    bad = client.get("/evaluations?rule_set_version=1")
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "RULE_SET_BINDING_INCOMPLETE"


def test_legacy_evaluation_without_binding_has_null_rule_set(client):
    from tests.helpers import make_rule

    _loaf(client)
    make_rule(client, "legacy-gluten", allergen="gluten", threshold=20,
              action="prohibit")
    resp = _eval(client, "e1")
    assert resp.status_code == 201, resp.text
    assert resp.json()["rule_set"] is None
    assert len(resp.json()["violations"]) == 1
