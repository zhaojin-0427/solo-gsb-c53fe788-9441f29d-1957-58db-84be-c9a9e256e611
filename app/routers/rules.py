from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import api_error
from ..models import Rule, utcnow
from ..schemas import RuleCreate, normalize_code

router = APIRouter(prefix="/rules", tags=["rules"])


def serialize(rule: Rule) -> dict:
    return {
        "code": rule.code,
        "jurisdiction": rule.jurisdiction,
        "allergen": rule.allergen,
        "threshold": rule.threshold,
        "unit": rule.unit,
        "action": rule.action,
        "applies_to": rule.applies_to,
        "status": rule.status,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "published_at": rule.published_at.isoformat() if rule.published_at else None,
    }


def _get_rule_or_404(db: Session, code: str) -> Rule:
    rule = db.query(Rule).filter(Rule.code == normalize_code(code)).first()
    if rule is None:
        raise api_error(404, "RULE_NOT_FOUND", f"unknown rule: {code!r}")
    return rule


@router.post("", status_code=201)
def create_rule(body: RuleCreate, db: Session = Depends(get_db)):
    """Register a threshold rule as a draft."""
    if db.query(Rule.id).filter(Rule.code == body.code).first():
        raise api_error(409, "RULE_EXISTS", f"rule {body.code!r} already exists")
    rule = Rule(
        code=body.code,
        jurisdiction=body.jurisdiction,
        allergen=body.allergen,
        threshold=body.threshold,
        unit=body.unit,
        action=body.action,
        applies_to=body.applies_to,
        status="draft",
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return serialize(rule)


@router.get("")
def list_rules(
    jurisdiction: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(Rule).order_by(Rule.code)
    if jurisdiction:
        q = q.filter(Rule.jurisdiction == jurisdiction.strip().upper())
    if status:
        q = q.filter(Rule.status == status)
    return [serialize(r) for r in q.all()]


@router.get("/{code}")
def get_rule(code: str, db: Session = Depends(get_db)):
    return serialize(_get_rule_or_404(db, code))


@router.put("/{code}")
def replace_rule(code: str, body: RuleCreate, db: Session = Depends(get_db)):
    """Replace a draft rule. Published rules are immutable."""
    rule = _get_rule_or_404(db, code)
    if rule.status == "published":
        raise api_error(409, "RULE_IMMUTABLE", f"rule {rule.code!r} is published and immutable")
    if body.code != rule.code:
        raise api_error(400, "CODE_MISMATCH", "body code must match the path code")
    rule.jurisdiction = body.jurisdiction
    rule.allergen = body.allergen
    rule.threshold = body.threshold
    rule.unit = body.unit
    rule.action = body.action
    rule.applies_to = body.applies_to
    db.commit()
    db.refresh(rule)
    return serialize(rule)


@router.delete("/{code}", status_code=204)
def delete_rule(code: str, db: Session = Depends(get_db)):
    """Delete a draft rule. Published rules are immutable."""
    rule = _get_rule_or_404(db, code)
    if rule.status == "published":
        raise api_error(409, "RULE_IMMUTABLE", f"rule {rule.code!r} is published and immutable")
    db.delete(rule)
    db.commit()
    return Response(status_code=204)


@router.post("/{code}/publish")
def publish_rule(code: str, db: Session = Depends(get_db)):
    """Publish a rule, freezing it forever. Idempotent."""
    rule = _get_rule_or_404(db, code)
    if rule.status != "published":
        rule.status = "published"
        rule.published_at = utcnow()
        db.commit()
        db.refresh(rule)
    return serialize(rule)
