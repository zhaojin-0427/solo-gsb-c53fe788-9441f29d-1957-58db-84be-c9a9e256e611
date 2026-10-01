from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import api_error
from ..models import Recipe, RecipeVersion, utcnow
from ..schemas import (
    RecipeCreate,
    RecipeVersionCreate,
    RecipeVersionUpdate,
    normalize_code,
)

router = APIRouter(prefix="/recipes", tags=["recipes"])


def serialize_version(rv: RecipeVersion) -> dict:
    return {
        "version": rv.version,
        "status": rv.status,
        "items": rv.items,
        "yield_quantity": rv.yield_quantity,
        "yield_unit": rv.yield_unit,
        "created_at": rv.created_at.isoformat() if rv.created_at else None,
        "published_at": rv.published_at.isoformat() if rv.published_at else None,
    }


def serialize_recipe(recipe: Recipe) -> dict:
    return {
        "code": recipe.code,
        "name": recipe.name,
        "created_at": recipe.created_at.isoformat() if recipe.created_at else None,
        "versions": [serialize_version(rv) for rv in recipe.versions],
    }


def _get_recipe_or_404(db: Session, code: str) -> Recipe:
    recipe = db.query(Recipe).filter(Recipe.code == normalize_code(code)).first()
    if recipe is None:
        raise api_error(404, "RECIPE_NOT_FOUND", f"unknown recipe: {code!r}")
    return recipe


def _get_version_or_404(db: Session, recipe: Recipe, version: int) -> RecipeVersion:
    rv = (
        db.query(RecipeVersion)
        .filter(RecipeVersion.recipe_id == recipe.id, RecipeVersion.version == version)
        .first()
    )
    if rv is None:
        raise _version_not_found(recipe.code, version)
    return rv


def _version_not_found(code: str, version: int):
    return api_error(
        404, "RECIPE_VERSION_NOT_FOUND", f"recipe {code!r} has no version {version}"
    )


def _version_immutable(code: str, version: int):
    return api_error(
        409,
        "RECIPE_VERSION_IMMUTABLE",
        f"recipe {code!r} version {version} is published and immutable",
    )


def _raise_lost_race(db: Session, recipe: Recipe, version: int, version_id):
    """Explain why a conditional draft write matched no row.

    The row was read as a draft but the guarded UPDATE/DELETE did not land,
    so a concurrent request committed first: either it published the version
    (the row is now immutable) or it deleted it (the version is gone).

    The decisive identity is the primary key captured at the original read.
    A concurrent request may have deleted that row and created a *new* draft
    reusing the same (recipe, version) pair; re-looking up by recipe +
    version would then mistake the new row for the row this request read.
    Querying the original id instead sees exactly what happened to the row we
    are responsible for.
    """
    current = db.get(RecipeVersion, version_id)
    if current is None:
        # The row we read is gone. A fresh draft sharing the version number
        # is an unrelated row and must stay untouched.
        raise _version_not_found(recipe.code, version)
    # Our original row still exists; only a concurrent publish of that same
    # row can explain the missed guard.
    raise _version_immutable(recipe.code, version)


@router.post("", status_code=201)
def create_recipe(body: RecipeCreate, db: Session = Depends(get_db)):
    if db.query(Recipe.id).filter(Recipe.code == body.code).first():
        raise api_error(409, "RECIPE_EXISTS", f"recipe {body.code!r} already exists")
    recipe = Recipe(code=body.code, name=body.name)
    db.add(recipe)
    db.commit()
    db.refresh(recipe)
    return serialize_recipe(recipe)


@router.get("")
def list_recipes(db: Session = Depends(get_db)):
    return [serialize_recipe(r) for r in db.query(Recipe).order_by(Recipe.code).all()]


@router.get("/{code}")
def get_recipe(code: str, db: Session = Depends(get_db)):
    return serialize_recipe(_get_recipe_or_404(db, code))


@router.post("/{code}/versions", status_code=201)
def create_version(code: str, body: RecipeVersionCreate, db: Session = Depends(get_db)):
    """Add a new draft version to a recipe. Version numbers cannot be reused."""
    recipe = _get_recipe_or_404(db, code)
    exists = (
        db.query(RecipeVersion.id)
        .filter(RecipeVersion.recipe_id == recipe.id, RecipeVersion.version == body.version)
        .first()
    )
    if exists:
        raise api_error(
            409,
            "RECIPE_VERSION_EXISTS",
            f"recipe {recipe.code!r} already has a version {body.version}",
        )
    rv = RecipeVersion(
        recipe_id=recipe.id,
        version=body.version,
        status="draft",
        items=[i.model_dump(mode="json") for i in body.items],
        yield_quantity=body.yield_quantity,
        yield_unit=body.yield_unit,
    )
    db.add(rv)
    db.commit()
    db.refresh(rv)
    return serialize_version(rv)


@router.get("/{code}/versions/{version}")
def get_version(code: str, version: int, db: Session = Depends(get_db)):
    recipe = _get_recipe_or_404(db, code)
    return serialize_version(_get_version_or_404(db, recipe, version))


@router.put("/{code}/versions/{version}")
def replace_version(
    code: str, version: int, body: RecipeVersionUpdate, db: Session = Depends(get_db)
):
    """Replace a draft version. Published versions are immutable.

    The draft guard is enforced by the database, not by the read above: the
    UPDATE only lands while the *row we read* (identified by its primary key)
    is still a draft, so a concurrent publish always wins and this request
    then fails with 409 instead of rewriting published content. If that row
    was deleted and a new draft reusing the same version number took its
    place, the miss is judged against the original row id and reported 404;
    the new draft is never rewritten.
    """
    recipe = _get_recipe_or_404(db, code)
    rv = _get_version_or_404(db, recipe, version)
    version_id = rv.id
    updated = (
        db.query(RecipeVersion)
        .filter(RecipeVersion.id == version_id, RecipeVersion.status == "draft")
        .update(
            {
                "items": [i.model_dump(mode="json") for i in body.items],
                "yield_quantity": body.yield_quantity,
                "yield_unit": body.yield_unit,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        # A concurrent publish or delete committed first; nothing was written.
        db.rollback()
        _raise_lost_race(db, recipe, version, version_id)
    db.commit()
    db.refresh(rv)
    return serialize_version(rv)


@router.delete("/{code}/versions/{version}", status_code=204)
def delete_version(code: str, version: int, db: Session = Depends(get_db)):
    """Delete a draft version. Published versions are immutable.

    The DELETE is guarded by ``status = 'draft'`` and scoped to the primary
    key read above, so a version that was published concurrently cannot be
    removed. If the read row was deleted and a new draft reuses the version
    number, the miss is judged against the original row id (404); the new
    draft is not deleted.
    """
    recipe = _get_recipe_or_404(db, code)
    rv = _get_version_or_404(db, recipe, version)
    version_id = rv.id
    deleted = (
        db.query(RecipeVersion)
        .filter(RecipeVersion.id == version_id, RecipeVersion.status == "draft")
        .delete(synchronize_session=False)
    )
    if deleted != 1:
        # A concurrent publish or delete committed first; nothing was written.
        db.rollback()
        _raise_lost_race(db, recipe, version, version_id)
    db.commit()
    return Response(status_code=204)


@router.post("/{code}/versions/{version}/publish")
def publish_version(code: str, version: int, db: Session = Depends(get_db)):
    """Publish a recipe version, freezing it forever. Idempotent.

    The draft -> published transition is a single conditional UPDATE on the
    primary key read above, so it races atomically with draft replacement and
    deletion: it either freezes the newest committed draft content, or fails
    cleanly when that exact row was deleted first (no snapshot is left
    behind). A new draft reusing the same version number is a different row
    and is never mistaken for the one we read — its fate is decided by
    re-fetching the original row id, so a stale publish gets 404 instead of a
    bogus 200 that echoes an unrelated draft.
    """
    recipe = _get_recipe_or_404(db, code)
    rv = _get_version_or_404(db, recipe, version)
    version_id = rv.id
    if rv.status == "published":
        return serialize_version(rv)
    published = (
        db.query(RecipeVersion)
        .filter(RecipeVersion.id == version_id, RecipeVersion.status == "draft")
        .update(
            {"status": "published", "published_at": utcnow()},
            synchronize_session=False,
        )
    )
    if published == 1:
        db.commit()
        db.refresh(rv)
        return serialize_version(rv)
    # Lost the race against a concurrent delete or publish of the row we
    # read. Judge by the original row id, not recipe + version: a different
    # row may now own that (recipe, version) pair.
    db.rollback()
    current = db.get(RecipeVersion, version_id)
    if current is None:
        raise _version_not_found(recipe.code, version)
    # Our original row still exists, so a concurrent publish of that same
    # row won; publishing is idempotent, so replay its frozen state.
    return serialize_version(current)
