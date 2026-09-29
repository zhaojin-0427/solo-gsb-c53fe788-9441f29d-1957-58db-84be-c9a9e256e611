import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import api_error
from ..models import Evaluation
from ..schemas import EvaluationRequest, normalize_code
from ..services.evaluation import evaluate_recipe, resolve_rule_set_binding

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def _request_hash(body: EvaluationRequest) -> str:
    payload = body.model_dump(mode="json")
    payload.pop("idempotency_key", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _serialize(ev: Evaluation, replayed: bool) -> dict:
    result = dict(ev.result)
    result["evaluation_id"] = str(ev.id)
    result["replayed"] = replayed
    return result


@router.post("", status_code=201)
def create_evaluation(
    body: EvaluationRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Evaluate a published recipe version against a jurisdiction.

    Optionally bind the request to an immutable rule-set version
    (``rule_set_code`` + ``rule_set_version``); every replay then reuses that
    exact rule snapshot. Idempotent: the same idempotency key + same request
    body replays the stored result; the same key with a different body is a
    conflict.
    """
    key = body.idempotency_key or idempotency_key
    if not key:
        raise api_error(
            400,
            "MISSING_IDEMPOTENCY_KEY",
            "provide an Idempotency-Key header or idempotency_key in the request body",
        )
    request_hash = _request_hash(body)

    existing = db.query(Evaluation).filter(Evaluation.idempotency_key == key).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise api_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "idempotency key was already used with a different request",
            )
        return JSONResponse(content=_serialize(existing, replayed=True), status_code=200)

    binding = resolve_rule_set_binding(
        db, body.rule_set_code, body.rule_set_version, body.jurisdiction
    )
    result = evaluate_recipe(
        db, body.recipe_code, body.recipe_version, body.jurisdiction, binding=binding
    )

    ev = Evaluation(
        id=uuid.uuid4(),
        idempotency_key=key,
        request_hash=request_hash,
        recipe_code=body.recipe_code,
        recipe_version=body.recipe_version,
        jurisdiction=body.jurisdiction,
        rule_set_code=binding.code if binding else None,
        rule_set_version=binding.version if binding else None,
        rule_set_digest=binding.digest if binding else None,
        status="completed",
        result=result,
    )
    db.add(ev)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent request using the same key.
        db.rollback()
        existing = db.query(Evaluation).filter(Evaluation.idempotency_key == key).first()
        if existing is not None and existing.request_hash == request_hash:
            return JSONResponse(content=_serialize(existing, replayed=True), status_code=200)
        raise api_error(
            409,
            "IDEMPOTENCY_CONFLICT",
            "idempotency key was already used with a different request",
        )
    db.refresh(ev)
    return _serialize(ev, replayed=False)


@router.get("")
def list_evaluations(
    recipe_code: str | None = None,
    idempotency_key: str | None = None,
    rule_set_code: str | None = None,
    rule_set_version: int | None = None,
    db: Session = Depends(get_db),
):
    """List evaluations; filter by recipe, idempotency key or rule-set version.

    Filtering by rule-set version requires ``rule_set_code`` as well.
    """
    q = db.query(Evaluation).order_by(Evaluation.created_at, Evaluation.id)
    if recipe_code:
        q = q.filter(Evaluation.recipe_code == recipe_code.strip().lower())
    if idempotency_key:
        q = q.filter(Evaluation.idempotency_key == idempotency_key)
    if rule_set_version is not None:
        if not rule_set_code:
            raise api_error(
                400,
                "RULE_SET_BINDING_INCOMPLETE",
                "rule_set_code is required when filtering by rule_set_version",
            )
        q = q.filter(
            Evaluation.rule_set_code == normalize_code(rule_set_code),
            Evaluation.rule_set_version == rule_set_version,
        )
    elif rule_set_code:
        q = q.filter(Evaluation.rule_set_code == normalize_code(rule_set_code))
    return [_serialize(ev, replayed=False) for ev in q.all()]


@router.get("/{evaluation_id}")
def get_evaluation(evaluation_id: str, db: Session = Depends(get_db)):
    try:
        ev_uuid = uuid.UUID(evaluation_id)
    except ValueError:
        raise api_error(404, "EVALUATION_NOT_FOUND", f"unknown evaluation: {evaluation_id!r}")
    ev = db.query(Evaluation).filter(Evaluation.id == ev_uuid).first()
    if ev is None:
        raise api_error(404, "EVALUATION_NOT_FOUND", f"unknown evaluation: {evaluation_id!r}")
    return _serialize(ev, replayed=False)
