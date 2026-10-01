"""Concurrency regression tests: recipe draft writes racing publish.

A request that has read a draft version must not rewrite or delete the
version once a concurrent publish commits first, and a publish must never
succeed (or leave a snapshot) once a concurrent delete removed the draft.
The win/lose order is decided atomically in the database by the version
status, so exactly one of the racing requests wins:

- publish commits first -> stale PUT/DELETE get 409 RECIPE_VERSION_IMMUTABLE
  and the published row and its content stay untouched;
- PUT commits first     -> publish freezes the updated draft;
- DELETE commits first  -> publish fails with 404 and leaves no snapshot.

A subtler hazard is the delete-and-recreate interleaving: after the stale
request read its draft, another request deletes that *row* and creates a
brand-new draft reusing the same ``(recipe_id, version)`` pair. The stale
request's conditional write (guarded by the row id it read) matches zero
rows, but re-looking up by recipe + version would now hit the *new* row and
misidentify it. The loser must instead track the row id it originally read:
the original row is gone -> 404 RECIPE_VERSION_NOT_FOUND and the new draft
(and its content) stay untouched; only an original row that really was
published yields 409 / idempotent replay.

The interleaving is injected deterministically: a one-shot SQLAlchemy
``load`` listener fires when the endpoint under test reads the
``RecipeVersion`` row, and a second session on the same connection commits
the competing write before the endpoint's own write executes.
"""

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, object_session

from app.models import RecipeVersion, utcnow
from tests.helpers import evaluate, make_ingredient, make_recipe

ORIGINAL_ITEMS = [{"kind": "ingredient", "code": "sugar", "quantity": 10, "unit": "g"}]
UPDATED_ITEMS = [{"kind": "ingredient", "code": "sugar", "quantity": 99, "unit": "g"}]
# Content of the brand-new draft created after the original row was deleted.
RECREATED_ITEMS = [{"kind": "ingredient", "code": "sugar", "quantity": 7, "unit": "g"}]
# Items as stored/returned by the API (normalized by the request schema).
ORIGINAL_ITEMS_JSON = [
    {"kind": "ingredient", "code": "sugar", "version": None, "quantity": 10.0, "unit": "g"}
]
UPDATED_ITEMS_JSON = [
    {"kind": "ingredient", "code": "sugar", "version": None, "quantity": 99.0, "unit": "g"}
]
RECREATED_ITEMS_JSON = [
    {"kind": "ingredient", "code": "sugar", "version": None, "quantity": 7.0, "unit": "g"}
]


def _make_draft(client, code="cake"):
    """A recipe with a single draft version 1 (10 g sugar)."""
    make_ingredient(client, "sugar")
    make_recipe(client, code, [{"version": 1, "items": ORIGINAL_ITEMS}], publish=False)


@pytest.fixture()
def race():
    """Install one-shot competitors firing on the next RecipeVersion load."""
    hooks = []

    def install(action):
        fired = []
        armed = [True]

        def hook(target, context):
            if not armed[0]:
                return
            armed[0] = False
            fired.append(True)
            other = Session(bind=object_session(target).get_bind())
            try:
                action(other, target.id)
                other.commit()
            finally:
                other.close()

        event.listen(RecipeVersion, "load", hook)
        hooks.append(hook)
        return fired

    yield install
    for hook in hooks:
        event.remove(RecipeVersion, "load", hook)


def _publish_now(db, version_id):
    rv = db.get(RecipeVersion, version_id)
    rv.status = "published"
    rv.published_at = utcnow()


def _delete_now(db, version_id):
    db.delete(db.get(RecipeVersion, version_id))


def _delete_and_recreate_now(db, version_id):
    """Delete the read row, then create a NEW draft reusing recipe + version.

    A fresh primary key means it is a different row; the loser still holding
    the old id must not mistake this new draft for the row it read.
    """
    old = db.get(RecipeVersion, version_id)
    recipe_id, version_number = old.recipe_id, old.version
    db.delete(old)
    db.flush()  # release uq_recipe_version before inserting the replacement
    db.add(
        RecipeVersion(
            recipe_id=recipe_id,
            version=version_number,
            status="draft",
            items=RECREATED_ITEMS_JSON,
        )
    )


def _replace_now(db, version_id):
    # What a real concurrent PUT commits (schema-normalized items).
    db.get(RecipeVersion, version_id).items = UPDATED_ITEMS_JSON


def test_put_loses_to_concurrent_publish(client, race):
    _make_draft(client)
    fired = race(_publish_now)

    resp = client.put("/recipes/cake/versions/1", json={"items": UPDATED_ITEMS})

    assert fired, "competing publish did not run inside the PUT read window"
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_IMMUTABLE"
    version = client.get("/recipes/cake/versions/1").json()
    assert version["status"] == "published"
    assert version["items"] == ORIGINAL_ITEMS_JSON  # published content untouched
    assert version["published_at"] is not None


def test_delete_loses_to_concurrent_publish(client, race):
    _make_draft(client)
    fired = race(_publish_now)

    resp = client.delete("/recipes/cake/versions/1")

    assert fired
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_IMMUTABLE"
    version = client.get("/recipes/cake/versions/1").json()
    assert version["status"] == "published"
    assert version["items"] == ORIGINAL_ITEMS_JSON  # published row not deleted


def test_publish_freezes_draft_replaced_by_concurrent_put(client, race):
    _make_draft(client)
    fired = race(_replace_now)

    resp = client.post("/recipes/cake/versions/1/publish")

    assert fired
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"
    version = client.get("/recipes/cake/versions/1").json()
    assert version["status"] == "published"
    assert version["items"] == UPDATED_ITEMS_JSON  # froze the newest committed draft


def test_publish_loses_to_concurrent_delete(client, race):
    _make_draft(client)
    fired = race(_delete_now)

    resp = client.post("/recipes/cake/versions/1/publish")

    assert fired
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"
    # no snapshot left behind
    assert client.get("/recipes/cake/versions/1").status_code == 404


def test_put_loses_to_concurrent_delete(client, race):
    _make_draft(client)
    fired = race(_delete_now)

    resp = client.put("/recipes/cake/versions/1", json={"items": UPDATED_ITEMS})

    assert fired
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"
    assert client.get("/recipes/cake/versions/1").status_code == 404


def test_concurrent_publish_stays_idempotent(client, race):
    _make_draft(client)
    fired = race(_publish_now)

    resp = client.post("/recipes/cake/versions/1/publish")

    assert fired
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"
    assert resp.json()["published_at"] is not None
    version = client.get("/recipes/cake/versions/1").json()
    assert version["status"] == "published"
    assert version["items"] == ORIGINAL_ITEMS_JSON


def test_evaluation_uses_frozen_content_after_publish_wins(client, race):
    _make_draft(client)
    race(_publish_now)

    # The stale PUT must fail; the published version keeps its frozen content.
    resp = client.put("/recipes/cake/versions/1", json={"items": UPDATED_ITEMS})
    assert resp.status_code == 409

    resp = evaluate(client, "cake", 1, key="race-eval")
    assert resp.status_code == 201
    # 10 g sugar from the frozen version, not the 99 g the stale write wanted.
    assert resp.json()["total_mass_g"] == 10.0


# --- delete + recreate with the same recipe code + version number ----------
#
# The originally read row is deleted and replaced by a NEW draft row (new
# primary key, same (recipe_id, version)). The loser must judge fate by the
# row id it originally read, not by recipe + version, so it sees the original
# row gone (404) and leaves the new draft completely untouched.


def test_put_after_delete_and_recreate_says_not_found(client, race):
    _make_draft(client)
    fired = race(_delete_and_recreate_now)

    resp = client.put("/recipes/cake/versions/1", json={"items": UPDATED_ITEMS})

    assert fired, "competing delete+recreate did not run inside the PUT read window"
    # Before the fix this wrongly returned 409 RECIPE_VERSION_IMMUTABLE.
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"
    # The new draft is intact: still a draft, still its own 7 g content.
    version = client.get("/recipes/cake/versions/1").json()
    assert version["status"] == "draft"
    assert version["items"] == RECREATED_ITEMS_JSON


def test_delete_after_delete_and_recreate_says_not_found(client, race):
    _make_draft(client)
    fired = race(_delete_and_recreate_now)

    resp = client.delete("/recipes/cake/versions/1")

    assert fired
    # Before the fix this wrongly returned 409 RECIPE_VERSION_IMMUTABLE.
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"
    # The stale delete must not remove the new draft.
    version = client.get("/recipes/cake/versions/1")
    assert version.status_code == 200
    assert version.json()["status"] == "draft"
    assert version.json()["items"] == RECREATED_ITEMS_JSON


def test_publish_after_delete_and_recreate_says_not_found(client, race):
    _make_draft(client)
    fired = race(_delete_and_recreate_now)

    resp = client.post("/recipes/cake/versions/1/publish")

    assert fired
    # Before the fix this returned 200 while echoing the NEW draft (status
    # draft): a "successful" publish that froze nothing.
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RECIPE_VERSION_NOT_FOUND"
    # The new draft is untouched and still publishable on its own behalf.
    version = client.get("/recipes/cake/versions/1")
    assert version.status_code == 200
    assert version.json()["status"] == "draft"
    assert version.json()["items"] == RECREATED_ITEMS_JSON

    republish = client.post("/recipes/cake/versions/1/publish")
    assert republish.status_code == 200
    assert republish.json()["status"] == "published"
    assert republish.json()["items"] == RECREATED_ITEMS_JSON
