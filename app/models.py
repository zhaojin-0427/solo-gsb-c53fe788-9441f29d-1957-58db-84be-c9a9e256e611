import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite in tests).
JSON_TYPE = JSONB().with_variant(sa.JSON(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Ingredient(Base):
    """An immutable raw material with its allergen markers."""

    __tablename__ = "ingredients"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    code = sa.Column(sa.String(64), unique=True, nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    # [{"allergen": "gluten", "concentration": 10.0, "unit": "%"}, ...]
    allergens = sa.Column(JSON_TYPE, nullable=False, default=list)
    # ["peanut", "sesame", ...] — "may contain" traces
    cross_contact = sa.Column(JSON_TYPE, nullable=False, default=list)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)


class Rule(Base):
    """A jurisdiction threshold rule. Immutable once published."""

    __tablename__ = "rules"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    code = sa.Column(sa.String(64), unique=True, nullable=False, index=True)
    jurisdiction = sa.Column(sa.String(32), nullable=False, index=True)
    allergen = sa.Column(sa.String(64), nullable=False)
    threshold = sa.Column(sa.Float, nullable=False)
    unit = sa.Column(sa.String(16), nullable=False)
    action = sa.Column(sa.String(16), nullable=False)  # declare | warning | prohibit
    applies_to = sa.Column(sa.String(16), nullable=False)  # contains | cross_contact
    status = sa.Column(sa.String(16), nullable=False, default="draft")  # draft | published
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    published_at = sa.Column(sa.DateTime(timezone=True), nullable=True)


class Recipe(Base):
    __tablename__ = "recipes"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    code = sa.Column(sa.String(64), unique=True, nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)

    versions = sa.orm.relationship(
        "RecipeVersion", back_populates="recipe", order_by="RecipeVersion.version"
    )


class RecipeVersion(Base):
    """A versioned, optionally nested recipe. Immutable once published."""

    __tablename__ = "recipe_versions"
    __table_args__ = (sa.UniqueConstraint("recipe_id", "version", name="uq_recipe_version"),)

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    recipe_id = sa.Column(sa.Uuid, sa.ForeignKey("recipes.id"), nullable=False, index=True)
    version = sa.Column(sa.Integer, nullable=False)
    status = sa.Column(sa.String(16), nullable=False, default="draft")  # draft | published
    # [{"kind": "ingredient"|"recipe", "code": ..., "version": int|null,
    #   "quantity": float, "unit": str}, ...]
    items = sa.Column(JSON_TYPE, nullable=False, default=list)
    yield_quantity = sa.Column(sa.Float, nullable=True)
    yield_unit = sa.Column(sa.String(16), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    published_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    recipe = sa.orm.relationship("Recipe", back_populates="versions")


class RuleSet(Base):
    """A per-jurisdiction, version-controlled collection of threshold rules.

    Holds a single working draft guarded by an optimistic-concurrency counter
    (``draft_base_version``). Publishing freezes the draft into an immutable
    :class:`RuleSetVersion`; the draft then keeps evolving for the next release.
    """

    __tablename__ = "rule_sets"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    code = sa.Column(sa.String(64), unique=True, nullable=False, index=True)
    jurisdiction = sa.Column(sa.String(32), nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=True)
    # Optimistic-concurrency counter; starts at 0, bumped on every accepted write.
    draft_base_version = sa.Column(sa.Integer, nullable=False, default=0)
    # Normalized draft rules: [{"code", "allergen", "action", "applies_to",
    # "threshold", "unit", "threshold_ppm"}, ...]
    draft_rules = sa.Column(JSON_TYPE, nullable=False, default=list)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)

    versions = sa.orm.relationship(
        "RuleSetVersion", back_populates="rule_set", order_by="RuleSetVersion.version"
    )


class RuleSetVersion(Base):
    """An immutable, published rule-set snapshot with a content digest."""

    __tablename__ = "rule_set_versions"
    __table_args__ = (
        sa.UniqueConstraint("rule_set_id", "version", name="uq_rule_set_version"),
    )

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    rule_set_id = sa.Column(
        sa.Uuid, sa.ForeignKey("rule_sets.id"), nullable=False, index=True
    )
    version = sa.Column(sa.Integer, nullable=False)
    digest = sa.Column(sa.String(128), nullable=False)
    rule_count = sa.Column(sa.Integer, nullable=False, default=0)
    # {"rule_count": n, "actions": {...}, "allergens": [...]}
    summary = sa.Column(JSON_TYPE, nullable=False, default=dict)
    # Immutable normalized rule snapshot (same shape as RuleSet.draft_rules).
    rules = sa.Column(JSON_TYPE, nullable=False, default=list)
    published_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)

    rule_set = sa.orm.relationship("RuleSet", back_populates="versions")


class Evaluation(Base):
    """A stored, replayable evaluation result keyed by an idempotency key."""

    __tablename__ = "evaluations"

    id = sa.Column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    idempotency_key = sa.Column(sa.String(128), unique=True, nullable=False, index=True)
    request_hash = sa.Column(sa.String(64), nullable=False)
    recipe_code = sa.Column(sa.String(64), nullable=False)
    recipe_version = sa.Column(sa.Integer, nullable=False)
    jurisdiction = sa.Column(sa.String(32), nullable=False)
    # Optional binding to an immutable rule-set snapshot.
    rule_set_code = sa.Column(sa.String(64), nullable=True, index=True)
    rule_set_version = sa.Column(sa.Integer, nullable=True)
    rule_set_digest = sa.Column(sa.String(128), nullable=True)
    status = sa.Column(sa.String(16), nullable=False, default="completed")
    result = sa.Column(JSON_TYPE, nullable=False, default=dict)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, default=utcnow)
