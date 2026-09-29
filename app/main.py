from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

from .db import Base, engine
from .routers import evaluations, ingredients, recipes, rule_sets, rules
from .units import CONCENTRATION_TO_PPM, MASS_TO_GRAMS

# Lightweight additive migration for databases created before rule sets existed:
# create_all builds new tables, and we add the new evaluation columns in place.
_EVALUATION_ADDED_COLUMNS = {
    "rule_set_code": "VARCHAR(64)",
    "rule_set_version": "INTEGER",
    "rule_set_digest": "VARCHAR(128)",
}


def _add_missing_columns() -> None:
    inspector = inspect(engine)
    if "evaluations" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("evaluations")}
    with engine.begin() as conn:
        for name, ddl in _EVALUATION_ADDED_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE evaluations ADD COLUMN {name} {ddl}"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    yield


app = FastAPI(
    title="Food Allergen Recipe Compliance API",
    version="1.1.0",
    description=(
        "Register immutable ingredients, jurisdiction threshold rules and versioned "
        "nested recipes; publish traceable, versioned per-jurisdiction rule sets; "
        "evaluate recipes for allergen labeling obligations, warnings and threshold "
        "violations with idempotent, replayable requests bound to immutable snapshots."
    ),
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    try:
        errors = jsonable_encoder(exc.errors())
    except Exception:  # pragma: no cover - defensive
        errors = [{"msg": str(exc)}]
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "VALIDATION_ERROR",
                "message": "request validation failed",
                "errors": errors,
            }
        },
    )


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "food-allergen-compliance",
        "version": "1.1.0",
        "docs": "/docs",
        "endpoints": [
            "/ingredients",
            "/rules",
            "/rule-sets",
            "/recipes",
            "/evaluations",
            "/units",
            "/health",
        ],
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/units", tags=["meta"])
def supported_units():
    return {
        "mass_units": sorted(MASS_TO_GRAMS),
        "concentration_units": sorted(CONCENTRATION_TO_PPM),
    }


app.include_router(ingredients.router)
app.include_router(rules.router)
app.include_router(rule_sets.router)
app.include_router(recipes.router)
app.include_router(evaluations.router)
