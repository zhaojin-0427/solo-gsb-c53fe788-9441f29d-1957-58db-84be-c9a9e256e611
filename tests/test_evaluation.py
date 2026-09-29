from tests.helpers import (
    build_cake_scenario,
    evaluate,
    make_ingredient,
    make_recipe,
    make_rule,
)


def test_nested_expansion_with_unit_conversion(client):
    build_cake_scenario(client)
    resp = evaluate(client, "cake", 1, key="e1")
    assert resp.status_code == 201, resp.text
    result = resp.json()

    # 400g filling (almond:sugar = 50:50) + 1000g wheat flour
    assert result["total_mass_g"] == 1400.0

    # tree nuts: 200g / 1400g = 14.285714%
    tree_nuts = next(a for a in result["allergens"] if a["allergen"] == "tree_nuts")
    assert abs(tree_nuts["concentration_percent"] - 14.285714) < 1e-5

    # gluten: 100g / 1400g = 7.142857%
    gluten = next(a for a in result["allergens"] if a["allergen"] == "gluten")
    assert abs(gluten["concentration_percent"] - 7.142857) < 1e-5

    # tree_nuts and gluten obligations (threshold 0); gluten also prohibits at 20 ppm
    obligated = {o["allergen"] for o in result["obligations"]}
    assert obligated == {"gluten", "tree_nuts"}
    violations = {v["allergen"] for v in result["violations"]}
    assert violations == {"gluten"}

    # cross-contact peanut appears as warning even though the ingredient has 0 peanut
    warnings = result["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["allergen"] == "peanut"
    assert warnings[0]["source"] == "cross_contact"
    # trigger paths are stably ordered and show the nested path
    paths = ["/".join(p["path"]) for p in warnings[0]["trigger_paths"]]
    assert paths == ["cake@v1/filling@v1/almond_flour"]


def test_trigger_paths_are_stable_and_recursive(client):
    build_cake_scenario(client)
    r1 = evaluate(client, "cake", 1, key="a").json()
    r2 = evaluate(client, "cake", 1, key="b").json()
    assert r1["obligations"] == r2["obligations"]
    assert r1["warnings"] == r2["warnings"]

    # obligation trigger paths carry nested chain through filling
    gluten_paths = [
        "/".join(p["path"])
        for o in r1["obligations"]
        if o["allergen"] == "gluten"
        for p in o["trigger_paths"]
    ]
    assert gluten_paths == ["cake@v1/wheat_flour"]

    tree_nut_paths = [
        "/".join(p["path"])
        for o in r1["obligations"]
        if o["allergen"] == "tree_nuts"
        for p in o["trigger_paths"]
    ]
    assert tree_nut_paths == ["cake@v1/filling@v1/almond_flour"]


def test_below_threshold_no_obligation(client):
    make_ingredient(
        client,
        "wheat",
        allergens=[{"allergen": "gluten", "concentration": 10, "unit": "ppm"}],
    )
    make_rule(client, "r", allergen="gluten", threshold=20, action="declare", unit="ppm")
    make_recipe(
        client,
        "bread",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "wheat", "quantity": 1, "unit": "kg"}
                ],
            }
        ],
    )
    resp = evaluate(client, "bread", 1)
    assert resp.status_code == 201
    result = resp.json()
    assert result["obligations"] == []
    assert result["violations"] == []
    # present but below threshold -> not regulated
    assert result["allergens"][0]["concentration_ppm"] == 10.0


def test_units_kg_mg_oz(client):
    # ingredient: 1000 ppm gluten; 1 lb used (=453.59237 g) inside 1 kg batch
    make_ingredient(
        client,
        "mild_flour",
        allergens=[{"allergen": "gluten", "concentration": 1000, "unit": "ppm"}],
    )
    make_rule(client, "r", allergen="gluten", threshold=400, action="declare", unit="ppm")
    make_recipe(
        client,
        "loaf",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "mild_flour",
                     "quantity": 1, "unit": "lb"},
                    {"kind": "ingredient", "code": "filler",
                     "quantity": 546.40763, "unit": "g"},
                ],
            }
        ],
    )
    make_ingredient(client, "filler")
    # filler registered after recipe definition: references only resolved at eval
    resp = evaluate(client, "loaf", 1)
    assert resp.status_code == 201, resp.text
    result = resp.json()
    expected_ppm = 453.59237 / 1000.0 * 1000
    assert abs(result["allergens"][0]["concentration_ppm"] - expected_ppm) < 1e-3
    assert len(result["obligations"]) == 1


def test_unknown_ingredient_returns_minimal_missing_set(client):
    make_rule(client, "r", allergen="gluten")
    make_recipe(
        client,
        "cake",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "ingredient", "code": "mystery_a", "quantity": 10, "unit": "g"},
                    {"kind": "ingredient", "code": "mystery_b", "quantity": 10, "unit": "g"},
                    {"kind": "ingredient", "code": "mystery_a", "quantity": 20, "unit": "g"},
                ],
            }
        ],
    )
    resp = evaluate(client, "cake", 1)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "INCOMPLETE_INPUT"
    # de-duplicated minimal set, stable order
    assert detail["missing"] == [
        {"type": "ingredient", "code": "mystery_a", "reason": "unknown_ingredient"},
        {"type": "ingredient", "code": "mystery_b", "reason": "unknown_ingredient"},
    ]


def test_unknown_subrecipe_and_version(client):
    make_ingredient(client, "sugar")
    make_recipe(
        client,
        "top",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "recipe", "code": "ghost", "quantity": 10, "unit": "g"},
                    {"kind": "recipe", "code": "ghost", "version": 3,
                     "quantity": 10, "unit": "g"},
                    {"kind": "recipe", "code": "missing_v", "version": 9,
                     "quantity": 10, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar", "quantity": 10, "unit": "g"},
                ],
            }
        ],
    )
    resp = evaluate(client, "top", 1)
    assert resp.status_code == 422
    missing = resp.json()["detail"]["missing"]
    assert missing == [
        {"type": "recipe", "code": "ghost", "reason": "unknown_recipe"},
        {"type": "recipe", "code": "ghost", "version": 3, "reason": "unknown_recipe"},
        {"type": "recipe", "code": "missing_v", "version": 9, "reason": "unknown_recipe"},
    ]


def test_unpinned_subrecipe_resolves_latest_published(client):
    make_ingredient(client, "sugar")
    make_recipe(
        client,
        "base",
        [
            {"version": 1,
             "items": [{"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "kg"}]},
            {"version": 2,
             "items": [{"kind": "ingredient", "code": "sugar", "quantity": 2, "unit": "kg"}]},
        ],
    )
    make_recipe(
        client,
        "product",
        [
            {
                "version": 1,
                "items": [{"kind": "recipe", "code": "base", "quantity": 2, "unit": "kg"}],
            }
        ],
    )
    resp = evaluate(client, "product", 1)
    assert resp.status_code == 201
    # resolved to v2
    assert {"code": "base", "version": 2} in resp.json()["resolved_recipes"]


def test_draft_subrecipe_not_resolved(client):
    make_ingredient(client, "sugar")
    make_recipe(
        client,
        "base",
        [
            {"version": 1,
             "items": [{"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "kg"}]},
        ],
        publish=False,
    )
    make_recipe(
        client,
        "product",
        [
            {
                "version": 1,
                "items": [{"kind": "recipe", "code": "base", "quantity": 1, "unit": "kg"}],
            }
        ],
    )
    resp = evaluate(client, "product", 1)
    assert resp.status_code == 422
    assert resp.json()["detail"]["missing"] == [
        {"type": "recipe_version", "code": "base", "reason": "no_published_version"}
    ]


def test_cycle_detected(client):
    make_ingredient(client, "sugar")
    make_recipe(
        client,
        "a",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "recipe", "code": "b", "quantity": 10, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar", "quantity": 10, "unit": "g"},
                ],
            }
        ],
    )
    make_recipe(
        client,
        "b",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "recipe", "code": "a", "quantity": 10, "unit": "g"},
                    {"kind": "ingredient", "code": "sugar", "quantity": 10, "unit": "g"},
                ],
            }
        ],
    )
    resp = evaluate(client, "a", 1)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "RECIPE_CYCLE"
    assert detail["cycle"][0] == detail["cycle"][-1]
    assert set(detail["cycle"]) == {"a@v1", "b@v1"}


def test_self_cycle(client):
    make_recipe(
        client,
        "loop",
        [
            {
                "version": 1,
                "items": [
                    {"kind": "recipe", "code": "loop", "version": 1,
                     "quantity": 10, "unit": "g"}
                ],
            }
        ],
    )
    resp = evaluate(client, "loop", 1)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "RECIPE_CYCLE"


def test_cannot_evaluate_draft(client):
    make_recipe(
        client,
        "r",
        [
            {"version": 1,
             "items": [{"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "g"}]},
        ],
        publish=False,
    )
    resp = evaluate(client, "r", 1)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "VERSION_NOT_PUBLISHED"


def test_missing_recipe_and_version(client):
    resp = evaluate(client, "nope", 1)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_NOT_FOUND"

    make_recipe(
        client,
        "r",
        [
            {"version": 1,
             "items": [{"kind": "ingredient", "code": "sugar", "quantity": 1, "unit": "g"}]},
        ],
        publish=False,
    )
    make_ingredient(client, "sugar")
    resp = evaluate(client, "r", 2)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"
