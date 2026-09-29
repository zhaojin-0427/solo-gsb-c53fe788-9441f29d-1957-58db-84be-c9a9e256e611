import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .units import CONCENTRATION_TO_PPM, MASS_TO_GRAMS, concentration_to_ppm

CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
JURISDICTION_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")


def normalize_code(value: str) -> str:
    value = value.strip().lower()
    if not CODE_RE.match(value):
        raise ValueError("must match ^[a-z0-9][a-z0-9._-]{0,63}$")
    return value


def normalize_jurisdiction(value: str) -> str:
    value = value.strip().upper()
    if not JURISDICTION_RE.match(value):
        raise ValueError("must match ^[A-Z0-9][A-Z0-9._-]{0,31}$ (case-insensitive)")
    return value


class AllergenContent(BaseModel):
    allergen: str
    concentration: float = Field(ge=0)
    unit: str

    @field_validator("allergen")
    @classmethod
    def _norm_allergen(cls, v: str) -> str:
        return normalize_code(v)

    @model_validator(mode="after")
    def _check(self):
        if self.unit not in CONCENTRATION_TO_PPM:
            raise ValueError(
                f"unknown concentration unit: {self.unit!r}; "
                f"supported: {sorted(CONCENTRATION_TO_PPM)}"
            )
        if concentration_to_ppm(self.concentration, self.unit) > 1_000_000.0:
            raise ValueError("concentration exceeds 100%")
        return self


class IngredientCreate(BaseModel):
    code: str
    name: str = Field(min_length=1, max_length=255)
    allergens: list[AllergenContent] = []
    cross_contact: list[str] = []

    @field_validator("code")
    @classmethod
    def _norm_code(cls, v: str) -> str:
        return normalize_code(v)

    @field_validator("cross_contact")
    @classmethod
    def _norm_cross_contact(cls, v: list[str]) -> list[str]:
        return sorted({normalize_code(x) for x in v})


class RuleCreate(BaseModel):
    code: str
    jurisdiction: str
    allergen: str
    threshold: float = Field(ge=0)
    unit: str
    action: Literal["declare", "warning", "prohibit"]
    applies_to: Literal["contains", "cross_contact"]

    @field_validator("code", "allergen")
    @classmethod
    def _norm_code(cls, v: str) -> str:
        return normalize_code(v)

    @field_validator("jurisdiction")
    @classmethod
    def _norm_jurisdiction(cls, v: str) -> str:
        return normalize_jurisdiction(v)

    @field_validator("unit")
    @classmethod
    def _check_unit(cls, v: str) -> str:
        v = v.strip()
        if v not in CONCENTRATION_TO_PPM:
            raise ValueError(
                f"unknown concentration unit: {v!r}; supported: {sorted(CONCENTRATION_TO_PPM)}"
            )
        return v


class RecipeCreate(BaseModel):
    code: str
    name: str = Field(min_length=1, max_length=255)

    @field_validator("code")
    @classmethod
    def _norm_code(cls, v: str) -> str:
        return normalize_code(v)


class RecipeItem(BaseModel):
    kind: Literal["ingredient", "recipe"]
    code: str
    # Pinned sub-recipe version; null means "latest published" at evaluation time.
    version: Optional[int] = Field(default=None, ge=1)
    quantity: float = Field(gt=0)
    unit: str

    @field_validator("code")
    @classmethod
    def _norm_code(cls, v: str) -> str:
        return normalize_code(v)

    @model_validator(mode="after")
    def _check(self):
        if self.unit not in MASS_TO_GRAMS:
            raise ValueError(
                f"unknown mass unit: {self.unit!r}; supported: {sorted(MASS_TO_GRAMS)}"
            )
        if self.kind == "ingredient" and self.version is not None:
            raise ValueError("ingredient items must not set version")
        return self


class RecipeVersionCreate(BaseModel):
    version: int = Field(ge=1)
    items: list[RecipeItem] = Field(min_length=1)
    yield_quantity: Optional[float] = Field(default=None, gt=0)
    yield_unit: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        if (self.yield_quantity is None) != (self.yield_unit is None):
            raise ValueError("yield_quantity and yield_unit must be provided together")
        if self.yield_unit is not None and self.yield_unit not in MASS_TO_GRAMS:
            raise ValueError(
                f"unknown mass unit: {self.yield_unit!r}; supported: {sorted(MASS_TO_GRAMS)}"
            )
        return self


class RecipeVersionUpdate(BaseModel):
    items: list[RecipeItem] = Field(min_length=1)
    yield_quantity: Optional[float] = Field(default=None, gt=0)
    yield_unit: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        if (self.yield_quantity is None) != (self.yield_unit is None):
            raise ValueError("yield_quantity and yield_unit must be provided together")
        if self.yield_unit is not None and self.yield_unit not in MASS_TO_GRAMS:
            raise ValueError(
                f"unknown mass unit: {self.yield_unit!r}; supported: {sorted(MASS_TO_GRAMS)}"
            )
        return self


class RuleSetCreate(BaseModel):
    code: str
    jurisdiction: str
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)

    @field_validator("code")
    @classmethod
    def _norm_code(cls, v: str) -> str:
        return normalize_code(v)

    @field_validator("jurisdiction")
    @classmethod
    def _norm_jurisdiction(cls, v: str) -> str:
        return normalize_jurisdiction(v)


class RuleSetRule(BaseModel):
    """One rule inside a rule-set draft submission."""

    code: str
    allergen: str
    threshold: float = Field(ge=0)
    unit: str
    action: Literal["declare", "warning", "prohibit"]
    applies_to: Literal["contains", "cross_contact"]

    @field_validator("code", "allergen")
    @classmethod
    def _norm_code(cls, v: str) -> str:
        return normalize_code(v)

    @field_validator("unit")
    @classmethod
    def _check_unit(cls, v: str) -> str:
        v = v.strip()
        if v not in CONCENTRATION_TO_PPM:
            raise ValueError(
                f"unknown concentration unit: {v!r}; supported: {sorted(CONCENTRATION_TO_PPM)}"
            )
        return v


class RuleSetSubmit(BaseModel):
    """Full replacement of a rule-set draft, guarded by base_version (optimistic lock)."""

    base_version: int = Field(ge=0)
    rules: list[RuleSetRule] = []


class RuleSetPublish(BaseModel):
    """Publishing is also an optimistic-locked write, so it carries base_version."""

    base_version: int = Field(ge=0)


class EvaluationRequest(BaseModel):
    recipe_code: str
    recipe_version: int = Field(ge=1)
    jurisdiction: str
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)
    # Optional explicit binding to an immutable rule-set snapshot. Both fields
    # must be supplied together; the referenced version must be published.
    rule_set_code: Optional[str] = None
    rule_set_version: Optional[int] = Field(default=None, ge=1)

    @field_validator("recipe_code", "rule_set_code")
    @classmethod
    def _norm_code(cls, v: Optional[str]) -> Optional[str]:
        return normalize_code(v) if v is not None else None

    @field_validator("jurisdiction")
    @classmethod
    def _norm_jurisdiction(cls, v: str) -> str:
        return normalize_jurisdiction(v)
