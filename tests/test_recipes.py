from tests.helpers import make_ingredient, make_recipe


def test_recipe_versioning_and_immutability(client):
    make_recipe(
        client,
        "bread",
        [
            {
                "version": 1,
                "items": [{"kind": "ingredient", "code": "sugar", "quantity": 10, "unit": "g"}],
            }
        ],
        publish=False,
    )
    make_ingredient(client, "sugar")

    # Draft version is editable
    resp = client.put(
        "/recipes/bread/versions/1",
        json={
            "items": [
                {"kind": "ingredient", "code": "sugar", "quantity": 20, "unit": "g"},
                {"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "oz"},
            ]
        },
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2

    client.post("/recipes/bread/versions/1/publish")
    resp = client.put(
        "/recipes/bread/versions/1",
        json={
            "items": [
                {"kind": "ingredient", "code": "sugar", "quantity": 5, "unit": "g"}
            ]
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_IMMUTABLE"

    # next version possible
    resp = client.post(
        "/recipes/bread/versions",
        json={
            "version": 2,
            "items": [{"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "kg"}],
        },
    )
    assert resp.status_code == 201

    # version reuse rejected
    resp = client.post(
        "/recipes/bread/versions",
        json={
            "version": 1,
            "items": [{"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "kg"}],
        },
    )
    assert resp.status_code == 409


def test_ingredient_item_cannot_have_version(client):
    client.post("/recipes", json={"code": "r", "name": "R"})
    client.post(
        "/recipes/r/versions",
        json={
            "version": 1,
            "items": [
                {"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "g"}
            ],
        },
    )
    resp = client.post(
        "/recipes/r/versions",
        json={
            "version": 2,
            "items": [
                {"kind": "ingredient", "code": "sugar", "version": 1,
                 "quantity": 1, "unit": "g"}
            ],
        },
    )
    assert resp.status_code == 422
    assert "version" in resp.text


def test_publish_is_idempotent(client):
    make_recipe(
        client,
        "bread",
        [
            {
                "version": 1,
                "items": [{"kind": "ingredient", "code": "sugar", "quantity": 10, "unit": "g"}],
            }
        ],
    )
    first = client.post("/recipes/bread/versions/1/publish")
    assert first.status_code == 200
    again = client.post("/recipes/bread/versions/1/publish")
    assert again.status_code == 200
    assert again.json() == first.json()
    assert again.json()["published_at"] == first.json()["published_at"]


def test_empty_items_rejected(client):
    client.post("/recipes", json={"code": "r", "name": "R"})
    resp = client.post("/recipes/r/versions", json={"version": 1, "items": []})
    assert resp.status_code == 422


def test_yield_fields_must_come_together(client):
    client.post("/recipes", json={"code": "r", "name": "R"})
    resp = client.post(
        "/recipes/r/versions",
        json={
            "version": 1,
            "items": [{"kind": "ingredient", "code": "x", "quantity": 1, "unit": "g"}],
            "yield_quantity": 100,
        },
    )
    assert resp.status_code == 422
