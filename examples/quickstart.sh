#!/usr/bin/env bash
# End-to-end example against a locally running API (default: http://localhost:8000).
#
#   bash examples/quickstart.sh
#
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
j() { python3 -m json.tool; }

echo "== 1. health =="
curl -fsS "$BASE/health"; echo

echo "== 2. supported units =="
curl -fsS "$BASE/units" | j; echo

echo "== 3. register immutable ingredients =="
curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' -d '{
  "code": "almond_flour",
  "name": "Almond Flour",
  "allergens": [{"allergen": "tree_nuts", "concentration": 100, "unit": "%"}],
  "cross_contact": ["peanut"]
}' | j

curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' -d '{
  "code": "wheat_flour",
  "name": "Wheat Flour",
  "allergens": [{"allergen": "gluten", "concentration": 10, "unit": "%"}]
}' | j

curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' -d '{
  "code": "sugar", "name": "Sugar"
}' | j

echo "== 4. (legacy) standalone rules still supported =="
curl -fsS -X POST "$BASE/rules" -H 'Content-Type: application/json' \
  -d '{"code":"eu-gluten-prohibit-20ppm","jurisdiction":"EU","allergen":"gluten","threshold":20,"unit":"ppm","action":"prohibit","applies_to":"contains"}' | j
curl -fsS -X POST "$BASE/rules/eu-gluten-prohibit-20ppm/publish" | j

echo "== 5. create a per-jurisdiction rule-set draft (base_version starts at 0) =="
curl -fsS -X POST "$BASE/rule-sets" -H 'Content-Type: application/json' \
  -d '{"code":"eu-pack","jurisdiction":"EU","name":"EU allergen pack"}' | j

echo "== 6. submit rules; thresholds normalized to ppm (20 mg/kg == 20 ppm) =="
curl -fsS -X PUT "$BASE/rule-sets/eu-pack" -H 'Content-Type: application/json' -d '{
  "base_version": 0,
  "rules": [
    {"code":"gluten-declare","allergen":"gluten","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"code":"tree-nuts-declare","allergen":"tree_nuts","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"code":"gluten-prohibit","allergen":"gluten","threshold":20,"unit":"mg/kg","action":"prohibit","applies_to":"contains"},
    {"code":"peanut-may-contain","allergen":"peanut","threshold":0,"unit":"ppm","action":"warning","applies_to":"cross_contact"}
  ]
}' | j

echo "== 7. optimistic concurrency: stale base_version -> 409 + current_version =="
curl -sS -X PUT "$BASE/rule-sets/eu-pack" -H 'Content-Type: application/json' \
  -d '{"base_version":0,"rules":[]}' | j || true

echo "== 8. publish: declare+warning coexist, but prohibit conflicts at the same boundary =="
# Submit a draft that also declares gluten at 20 ppm (same boundary as the prohibit):
curl -fsS -X PUT "$BASE/rule-sets/eu-pack" -H 'Content-Type: application/json' -d '{
  "base_version": 1,
  "rules": [
    {"code":"gluten-declare-20","allergen":"gluten","threshold":20,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"code":"gluten-prohibit","allergen":"gluten","threshold":0.002,"unit":"%","action":"prohibit","applies_to":"contains"}
  ]
}' >/dev/null
curl -sS -X POST "$BASE/rule-sets/eu-pack/publish" -H 'Content-Type: application/json' \
  -d '{"base_version":2}' | j || true

echo "-- fix the draft (declare at 0 ppm coexists with nothing at that boundary) and publish v1 --"
curl -fsS -X PUT "$BASE/rule-sets/eu-pack" -H 'Content-Type: application/json' -d '{
  "base_version": 2,
  "rules": [
    {"code":"gluten-declare","allergen":"gluten","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"code":"tree-nuts-declare","allergen":"tree_nuts","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"code":"gluten-prohibit","allergen":"gluten","threshold":20,"unit":"ppm","action":"prohibit","applies_to":"contains"},
    {"code":"peanut-may-contain","allergen":"peanut","threshold":0,"unit":"ppm","action":"warning","applies_to":"cross_contact"}
  ]
}' >/dev/null
curl -fsS -X POST "$BASE/rule-sets/eu-pack/publish" -H 'Content-Type: application/json' \
  -d '{"base_version":3}' | j

echo "== 9. versioned nested recipe (cake v1 uses 400 g filling v1 + 1 kg wheat flour) =="
curl -fsS -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"code":"filling","name":"Almond Filling"}' | j
curl -fsS -X POST "$BASE/recipes/filling/versions" -H 'Content-Type: application/json' -d '{
  "version": 1,
  "items": [
    {"kind":"ingredient","code":"almond_flour","quantity":100,"unit":"g"},
    {"kind":"ingredient","code":"sugar","quantity":100,"unit":"g"}
  ]
}' | j
curl -fsS -X POST "$BASE/recipes/filling/versions/1/publish" | j

curl -fsS -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"code":"cake","name":"Almond Cake"}' | j
curl -fsS -X POST "$BASE/recipes/cake/versions" -H 'Content-Type: application/json' -d '{
  "version": 1,
  "items": [
    {"kind":"recipe","code":"filling","quantity":400,"unit":"g"},
    {"kind":"ingredient","code":"wheat_flour","quantity":1,"unit":"kg"}
  ]
}' | j
curl -fsS -X POST "$BASE/recipes/cake/versions/1/publish" | j

echo "== 10. evaluate bound to rule-set v1 (first call -> 201) =="
curl -fsS -X POST "$BASE/evaluations" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cake-eu-pack-v1' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU","rule_set_code":"eu-pack","rule_set_version":1}' | j

echo "== 11. replay the same key -> 200, replayed=true, same snapshot reference =="
curl -fsS -o /dev/null -w 'status=%{http_code}\n' -X POST "$BASE/evaluations" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cake-eu-pack-v1' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU","rule_set_code":"eu-pack","rule_set_version":1}'

echo "== 12. missing/partial binding -> stable errors =="
curl -sS -X POST "$BASE/evaluations" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: bad-bind' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU","rule_set_code":"eu-pack"}' | j || true
curl -sS -X POST "$BASE/evaluations" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: bad-bind2' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU","rule_set_code":"eu-pack","rule_set_version":99}' | j || true

echo "== 13. publish v2 (prohibit raised above the cake concentration) and re-evaluate =="
curl -fsS -X PUT "$BASE/rule-sets/eu-pack" -H 'Content-Type: application/json' -d '{
  "base_version": 4,
  "rules": [
    {"code":"gluten-declare","allergen":"gluten","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"code":"tree-nuts-declare","allergen":"tree_nuts","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"code":"gluten-prohibit","allergen":"gluten","threshold":1000000,"unit":"ppm","action":"prohibit","applies_to":"contains"},
    {"code":"peanut-may-contain","allergen":"peanut","threshold":0,"unit":"ppm","action":"warning","applies_to":"cross_contact"}
  ]
}' >/dev/null
curl -fsS -X POST "$BASE/rule-sets/eu-pack/publish" -H 'Content-Type: application/json' \
  -d '{"base_version":5}' | python3 -c 'import sys,json; d=json.load(sys.stdin); print("published v"+str(d["version"]), d["digest"][:16], d["summary"])'
curl -fsS -X POST "$BASE/evaluations" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cake-eu-pack-v2' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU","rule_set_code":"eu-pack","rule_set_version":2}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("violations on v2:", d["violations"], "rule_set:", d["rule_set"]["version"], d["rule_set"]["digest"][:16])'

echo "-- the v1 snapshot stays immutable: replay v1 still shows the violation --"
curl -fsS "$BASE/evaluations?rule_set_code=eu-pack&rule_set_version=1" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("stored v1 evaluations:", len(d), "violations:", [len(e["violations"]) for e in d], "rule_set:", d[0]["rule_set"])'

echo "== 14. inspect immutable versions and the v1 snapshot =="
curl -fsS "$BASE/rule-sets/eu-pack/versions" | j
curl -fsS "$BASE/rule-sets/eu-pack/versions/1" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("v1 digest:", d["digest"][:16], "| gluten-prohibit threshold_ppm:", next(r["threshold_ppm"] for r in d["rules"] if r["code"]=="gluten-prohibit"))'

echo "== 15. incomplete input -> minimal missing set (422) =="
curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' \
  -d '{"code":"sugar2","name":"Sugar 2"}' >/dev/null
curl -fsS -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"code":"broken","name":"Broken"}' >/dev/null
curl -fsS -X POST "$BASE/recipes/broken/versions" -H 'Content-Type: application/json' -d '{
  "version": 1,
  "items": [
    {"kind":"ingredient","code":"mystery","quantity":10,"unit":"g"},
    {"kind":"recipe","code":"nonexistent","version":7,"quantity":10,"unit":"g"}
  ]
}' >/dev/null
curl -fsS -X POST "$BASE/recipes/broken/versions/1/publish" >/dev/null
curl -sS -X POST "$BASE/evaluations" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: broken-1' \
  -d '{"recipe_code":"broken","recipe_version":1,"jurisdiction":"EU"}' | j || true
