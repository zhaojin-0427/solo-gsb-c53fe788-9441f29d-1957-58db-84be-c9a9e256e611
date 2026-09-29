from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import api_error
from ..models import Ingredient
from ..schemas import IngredientCreate, normalize_code

router = APIRouter(prefix="/ingredients", tags=["ingredients"])


def serialize(ing: Ingredient) -> dict:
    return {
        "code": ing.code,
        "name": ing.name,
        "allergens": ing.allergens,
        "cross_contact": ing.cross_contact,
        "created_at": ing.created_at.isoformat() if ing.created_at else None,
    }


@router.post("", status_code=201)
def create_ingredient(body: IngredientCreate, db: Session = Depends(get_db)):
    """Register an immutable ingredient. Re-registering the same code is rejected."""
    if db.query(Ingredient.id).filter(Ingredient.code == body.code).first():
        raise api_error(
            409,
            "INGREDIENT_EXISTS",
            f"ingredient {body.code!r} already exists; ingredients are immutable",
        )
    ing = Ingredient(
        code=body.code,
        name=body.name,
        allergens=[a.model_dump() for a in body.allergens],
        cross_contact=body.cross_contact,
    )
    db.add(ing)
    db.commit()
    db.refresh(ing)
    return serialize(ing)


@router.get("")
def list_ingredients(db: Session = Depends(get_db)):
    return [serialize(i) for i in db.query(Ingredient).order_by(Ingredient.code).all()]


@router.get("/{code}")
def get_ingredient(code: str, db: Session = Depends(get_db)):
    ing = db.query(Ingredient).filter(Ingredient.code == normalize_code(code)).first()
    if ing is None:
        raise api_error(404, "INGREDIENT_NOT_FOUND", f"unknown ingredient: {code!r}")
    return serialize(ing)
