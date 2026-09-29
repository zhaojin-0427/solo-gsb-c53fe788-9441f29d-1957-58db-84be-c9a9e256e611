from tests.helpers import build_cake_scenario, evaluate


def test_idempotent_replay_returns_same_result(client):
    build_cake_scenario(client)
    first = evaluate(client, "cake", 1, key="fixed-key")
    assert first.status_code == 201
    assert first.json()["replayed"] is False
    evaluation_id = first.json()["evaluation_id"]

    second = evaluate(client, "cake", 1, key="fixed-key")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["evaluation_id"] == evaluation_id

    # header-based key works the same way
    third = client.post(
        "/evaluations",
        json={"recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU"},
        headers={"Idempotency-Key": "fixed-key"},
    )
    assert third.status_code == 200
    assert third.json()["evaluation_id"] == evaluation_id


def test_same_key_different_request_conflicts(client):
    build_cake_scenario(client)
    assert evaluate(client, "cake", 1, key="k").status_code == 201
    resp = evaluate(client, "cake", 1, jurisdiction="US", key="k")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_missing_key_rejected(client):
    build_cake_scenario(client)
    resp = client.post(
        "/evaluations",
        json={"recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_replay_listing_and_get(client):
    build_cake_scenario(client)
    evaluate(client, "cake", 1, key="k1")
    listing = client.get("/evaluations").json()
    assert len(listing) == 1
    eid = listing[0]["evaluation_id"]
    got = client.get(f"/evaluations/{eid}")
    assert got.status_code == 200
    assert got.json()["recipe"]["code"] == "cake"
    assert client.get("/evaluations/00000000-0000-0000-0000-000000000000").status_code == 404


def test_replay_after_incomplete_evaluation_does_not_store(client):
    build_cake_scenario(client)
    # a different recipe referencing an unknown ingredient
    client.post("/recipes", json={"code": "broken", "name": "Broken"})
    client.post(
        "/recipes/broken/versions",
        json={
            "version": 1,
            "items": [
                {"kind": "ingredient", "code": "ghost", "quantity": 1, "unit": "g"}
            ],
        },
    )
    client.post("/recipes/broken/versions/1/publish")

    resp = evaluate(client, "broken", 1, key="kb")
    assert resp.status_code == 422
    # nothing stored for the failed attempt; a fresh request behaves like the first
    assert client.get("/evaluations").json() == []
