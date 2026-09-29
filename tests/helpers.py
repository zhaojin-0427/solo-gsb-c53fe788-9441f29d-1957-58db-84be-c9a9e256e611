"""Shared helpers to build registration scenarios against the test client."""


def make_ingredient(client, code, allergens=None, cross_contact=None, name=None):
    resp = client.post(
        "/ingredients",
        json={
            "code": code,
            "name": name or code.replace("_", " ").title(),
            "allergens": allergens or [],
            "cross_contact": cross_contact or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_rule(
    client,
    code,
    jurisdiction="EU",
    allergen="gluten",
    threshold=0.0,
    unit="ppm",
    action="declare",
    applies_to="contains",
    publish=True,
):
    resp = client.post(
        "/rules",
        json={
            "code": code,
            "jurisdiction": jurisdiction,
            "allergen": allergen,
            "threshold": threshold,
            "unit": unit,
            "action": action,
            "applies_to": applies_to,
        },
    )
    assert resp.status_code == 201, resp.text
    if publish:
        resp = client.post(f"/rules/{code}/publish")
        assert resp.status_code == 200, resp.text
    return resp.json()


def make_recipe(client, code, versions, name=None, publish=True):
    """versions: list of dicts {version, items, yield_quantity?, yield_unit?}."""
    resp = client.post("/recipes", json={"code": code, "name": name or code.title()})
    assert resp.status_code == 201, resp.text
    for v in versions:
        resp = client.post(f"/recipes/{code}/versions", json=v)
        assert resp.status_code == 201, resp.text
        if publish:
            resp = client.post(f"/recipes/{code}/versions/{v['version']}/publish")
            assert resp.status_code == 200, resp.text
    return client.get(f"/recipes/{code}").json()


def evaluate(client, recipe_code, version, jurisdiction="EU", key="eval-1"):
    return client.post(
        "/evaluations",
        json={
            "recipe_code": recipe_code,
            "recipe_version": version,
            "jurisdiction": jurisdiction,
            "idempotency_key": key,
        },
    )


def build_cake_scenario(client):
    """Nested recipe with known numbers (see tests/test_evaluation.py)."""
    make_ingredient(
        client,
        "almond_flour",
        allergens=[{"allergen": "tree_nuts", "concentration": 100.0, "unit": "%"}],
        cross_contact=["peanut"],
    )
    make_ingredient(
        client,
        "wheat_flour",
        allergens=[{"allergen": "gluten", "concentration": 10.0, "unit": "%"}],
    )
    make_ingredient(client, "sugar")
    make_rule(client, "eu-gluten-declare", allergen="gluten", action="declare")
    make_rule(client, "eu-tree-nuts-declare", allergen="tree_nuts", action="declare")
    make_rule(client, "eu-gluten-prohibit", allergen="gluten", threshold=20.0, action="prohibit")
    make_rule(
        client, "eu-peanut-warning", allergen="peanut", action="warning",
        applies_to="cross_contact",
    )
    make_recipe(
        client,
        "filling",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "almond_flour", "quantity": 100, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar", "quantity": 100, "unit": "g"},
                ],
            }
        ],
    )
    make_recipe(
        client,
        "cake",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "recipe", "code": "filling", "quantity": 400, "unit": "g"},
                    {"kind": "ingredient", "code": "wheat_flour", "quantity": 1, "unit": "kg"},
                ],
            }
        ],
    )
