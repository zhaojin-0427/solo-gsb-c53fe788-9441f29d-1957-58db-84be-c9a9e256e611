from tests.helpers import make_ingredient, make_rule


def test_register_and_list_ingredients(client):
    make_ingredient(
        client,
        "wheat-flour",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "%"}],
        cross_contact=["sesame", "SESAME"],  # de-duplicated + normalized
        name="Wheat Flour",
    )
    resp = client.get("/ingredients")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["code"] == "wheat-flour"
    assert data[0]["allergens"] == [
        {"allergen": "gluten", "concentration": 10.0, "unit": "%"}
    ]
    assert data[0]["cross_contact"] == ["sesame"]


def test_ingredient_is_immutable(client):
    make_ingredient(client, "sugar")
    resp = client.post(
        "/ingredients", json={"code": "SUGAR", "name": "Other"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "INGREDIENT_EXISTS"


def test_unknown_and_invalid_units(client):
    resp = client.post(
        "/ingredients",
        json={
            "code": "x",
            "name": "X",
            "allergens": [{"allergen": "gluten", "concentration": 1, "unit": "bananas"}],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "VALIDATION_ERROR"

    resp = client.post(
        "/ingredients",
        json={
            "code": "y",
            "name": "Y",
            "allergens": [{"allergen": "gluten", "concentration": 200, "unit": "%"}],
        },
    )
    assert resp.status_code == 422  # >100%
    assert "100%" in resp.text


def test_concentration_unit_variants_accepted(client):
    for idx, (unit, value) in enumerate(
        [("ppm", 10), ("mg/kg", 10), ("ppb", 10000), ("%", 0.001)]
    ):
        resp = client.post(
            "/ingredients",
            json={
                "code": f"i_{idx}",
                "name": unit,
                "allergens": [{"allergen": "gluten", "concentration": value, "unit": unit}],
            },
        )
        assert resp.status_code == 201, resp.text


def test_rule_lifecycle_and_immutability(client):
    make_rule(client, "r1", publish=False)
    resp = client.put(
        "/rules/r1",
        json={
            "code": "r1",
            "jurisdiction": "EU",
            "allergen": "gluten",
            "threshold": 5,
            "unit": "ppm",
            "action": "warning",
            "applies_to": "contains",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["threshold"] == 5

    client.post("/rules/r1/publish")
    resp = client.put(
        "/rules/r1",
        json={
            "code": "r1",
            "jurisdiction": "EU",
            "allergen": "gluten",
            "threshold": 9,
            "unit": "ppm",
            "action": "warning",
            "applies_to": "contains",
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RULE_IMMUTABLE"

    # publish is idempotent
    again = client.post("/rules/r1/publish")
    assert again.status_code == 200
    assert again.json()["status"] == "published"


def test_rule_filters(client):
    make_rule(client, "eu-1", jurisdiction="EU")
    make_rule(client, "us-1", jurisdiction="US", allergen="milk")
    assert len(client.get("/rules?jurisdiction=eu").json()) == 1
    assert len(client.get("/rules?status=published").json()) == 2
    assert len(client.get("/rules?status=draft").json()) == 0


def test_mass_units_endpoint(client):
    units = client.get("/units").json()
    assert {"g", "kg", "mg", "oz", "lb"} <= set(units["mass_units"])
    assert {"ppm", "ppb", "mg/kg", "g/kg", "%"} <= set(units["concentration_units"])
