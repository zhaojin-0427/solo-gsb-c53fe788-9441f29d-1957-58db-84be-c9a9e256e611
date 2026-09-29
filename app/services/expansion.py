"""Recursive recipe expansion.

Expands a published recipe version into leaf-ingredient contributions by
walking the nested recipe graph, converting every quantity to grams and
scaling sub-recipe leaves by ``used quantity / sub-recipe yield``.

- Cycles raise :class:`CycleError` with the offending path.
- Unknown ingredients / recipes / versions are collected into a minimal,
  de-duplicated missing set instead of aborting the whole walk.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Ingredient, Recipe, RecipeVersion
from ..units import mass_to_grams


@dataclass
class Leaf:
    ingredient_code: str
    mass_g: float
    path: list[str]  # e.g. ["cake@v1", "filling@v2", "almond_flour"]


@dataclass(frozen=True)
class MissingRef:
    type: str  # "ingredient" | "recipe" | "recipe_version"
    code: str
    version: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        d: dict = {"type": self.type, "code": self.code}
        if self.version is not None:
            d["version"] = self.version
        if self.reason is not None:
            d["reason"] = self.reason
        return d

    def sort_key(self):
        return (self.type, self.code, self.version or 0, self.reason or "")


class CycleError(Exception):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__("recipe cycle detected: " + " -> ".join(cycle))


@dataclass
class Expansion:
    leaves: list[Leaf]
    yield_g: float
    missing: list[MissingRef]
    resolved_recipes: list[dict]


def _label(code: str, version: int) -> str:
    return f"{code}@v{version}"


class _Expander:
    def __init__(self, db: Session):
        self.db = db
        self.missing: dict[MissingRef, None] = {}  # ordered set, de-duplicated
        self.resolved: dict[tuple[str, int], None] = {}
        self._ingredient_exists_cache: dict[str, bool] = {}
        self._recipe_exists_cache: dict[str, bool] = {}

    # -- lookups ---------------------------------------------------------
    def _ingredient_exists(self, code: str) -> bool:
        if code not in self._ingredient_exists_cache:
            self._ingredient_exists_cache[code] = (
                self.db.query(Ingredient.id).filter(Ingredient.code == code).first()
                is not None
            )
        return self._ingredient_exists_cache[code]

    def _recipe_exists(self, code: str) -> bool:
        if code not in self._recipe_exists_cache:
            self._recipe_exists_cache[code] = (
                self.db.query(Recipe.id).filter(Recipe.code == code).first() is not None
            )
        return self._recipe_exists_cache[code]

    def _resolve_version(self, code: str, version: int | None) -> RecipeVersion | None:
        """Resolve a sub-recipe reference to a published version, or record a miss."""
        q = (
            self.db.query(RecipeVersion)
            .join(Recipe, RecipeVersion.recipe_id == Recipe.id)
            .filter(Recipe.code == code)
        )
        if version is None:
            rv = (
                q.filter(RecipeVersion.status == "published")
                .order_by(RecipeVersion.version.desc())
                .first()
            )
            if rv is None:
                if self._recipe_exists(code):
                    self._miss(MissingRef("recipe_version", code, None, "no_published_version"))
                else:
                    self._miss(MissingRef("recipe", code, None, "unknown_recipe"))
                return None
            return rv
        rv = q.filter(RecipeVersion.version == version).first()
        if rv is None:
            if self._recipe_exists(code):
                self._miss(MissingRef("recipe_version", code, version, "unknown_version"))
            else:
                self._miss(MissingRef("recipe", code, version, "unknown_recipe"))
            return None
        if rv.status != "published":
            self._miss(MissingRef("recipe_version", code, version, "not_published"))
            return None
        return rv

    def _miss(self, ref: MissingRef) -> None:
        self.missing[ref] = None

    # -- expansion -------------------------------------------------------
    def expand_recipe(
        self,
        rv: RecipeVersion,
        code: str,
        path: list[str],
        visiting: list[tuple[str, int]],
    ) -> tuple[list[Leaf], float]:
        """Return (leaf contributions, batch yield in grams) for a resolved version."""
        self.resolved[(code, rv.version)] = None
        leaves: list[Leaf] = []
        inputs_mass = 0.0
        for item in rv.items:
            qty_g = mass_to_grams(item["quantity"], item["unit"])
            inputs_mass += qty_g
            if item["kind"] == "ingredient":
                icode = item["code"]
                if not self._ingredient_exists(icode):
                    self._miss(MissingRef("ingredient", icode, None, "unknown_ingredient"))
                    continue
                leaves.append(Leaf(icode, qty_g, path + [icode]))
            else:
                sub = self._resolve_version(item["code"], item.get("version"))
                if sub is None:
                    continue
                key = (item["code"], sub.version)
                if key in visiting:
                    idx = visiting.index(key)
                    cycle = [_label(c, v) for c, v in visiting[idx:]] + [_label(*key)]
                    raise CycleError(cycle)
                sub_leaves, sub_yield = self.expand_recipe(
                    sub, item["code"], path + [_label(*key)], visiting + [key]
                )
                if sub_yield <= 0:
                    continue
                scale = qty_g / sub_yield
                for leaf in sub_leaves:
                    leaves.append(Leaf(leaf.ingredient_code, leaf.mass_g * scale, leaf.path))
        if rv.yield_quantity is not None:
            yield_g = mass_to_grams(rv.yield_quantity, rv.yield_unit)
        else:
            yield_g = inputs_mass
        return leaves, yield_g


def expand_root(db: Session, root: RecipeVersion, code: str) -> Expansion:
    expander = _Expander(db)
    key = (code, root.version)
    leaves, yield_g = expander.expand_recipe(root, code, [_label(*key)], [key])
    missing = sorted(expander.missing, key=MissingRef.sort_key)
    resolved = [{"code": c, "version": v} for c, v in sorted(expander.resolved)]
    return Expansion(leaves=leaves, yield_g=yield_g, missing=missing, resolved_recipes=resolved)
