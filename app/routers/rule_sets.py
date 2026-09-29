"""Per-jurisdiction, traceable rule sets.

Lifecycle
---------
1. ``POST /rule-sets``            create an empty draft (base_version = 0)
2. ``PUT  /rule-sets/{code}``     replace the draft; requires ``base_version``
3. ``POST /rule-sets/{code}/publish``  freeze an immutable version
4. ``GET  /rule-sets/...``        inspect drafts, versions and snapshots

Every accepted write bumps the draft's ``base_version``; a stale
``base_version`` yields ``409 VERSION_CONFLICT`` carrying ``current_version``
without writing anything.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import api_error
from ..models import RuleSet, RuleSetVersion, utcnow
from ..schemas import RuleSetCreate, RuleSetPublish, RuleSetSubmit, normalize_code
from ..services.rule_sets import (
    detect_conflicts,
    normalize_rules,
    rules_digest,
    rules_summary,
)

router = APIRouter(prefix="/rule-sets", tags=["rule-sets"])


def _conflict(code: str, current_version: int):
    return api_error(
        409,
        "VERSION_CONFLICT",
        f"rule set {code!r} was modified concurrently; refetch and resubmit",
        current_version=current_version,
    )


def _cas_advance(db: Session, rs: RuleSet, base_version: int, **changes) -> bool:
    """Atomically advance the draft version only when it still matches.

    Returns True when the compare-and-swap landed, False when another writer
    won the race (nothing is written by this call).
    """
    changes["draft_base_version"] = base_version + 1
    updated = (
        db.query(RuleSet)
        .filter(RuleSet.id == rs.id, RuleSet.draft_base_version == base_version)
        .update(changes, synchronize_session=False)
    )
    return updated == 1


def _get_rule_set_or_404(db: Session, code: str) -> RuleSet:
    rs = db.query(RuleSet).filter(RuleSet.code == normalize_code(code)).first()
    if rs is None:
        raise api_error(404, "RULE_SET_NOT_FOUND", f"unknown rule set: {code!r}")
    return rs


def _get_version_or_404(rs: RuleSet, version: int) -> RuleSetVersion:
    rsv = next((v for v in rs.versions if v.version == version), None)
    if rsv is None:
        raise api_error(
            404,
            "RULE_SET_VERSION_NOT_FOUND",
            f"rule set {rs.code!r} has no published version {version}",
        )
    return rsv


def serialize_draft(rs: RuleSet) -> dict:
    return {
        "code": rs.code,
        "jurisdiction": rs.jurisdiction,
        "name": rs.name,
        "base_version": rs.draft_base_version,
        "published_version": rs.versions[-1].version if rs.versions else None,
        "rules": rs.draft_rules,
        "created_at": rs.created_at.isoformat() if rs.created_at else None,
        "updated_at": rs.updated_at.isoformat() if rs.updated_at else None,
    }


def serialize_version(rs: RuleSet, rsv: RuleSetVersion, include_rules: bool = True) -> dict:
    data = {
        "rule_set_code": rs.code,
        "jurisdiction": rs.jurisdiction,
        "version": rsv.version,
        "digest": rsv.digest,
        "published_at": rsv.published_at.isoformat() if rsv.published_at else None,
        "summary": rsv.summary,
    }
    if include_rules:
        data["rules"] = rsv.rules
    return data


@router.post("", status_code=201)
def create_rule_set(body: RuleSetCreate, db: Session = Depends(get_db)):
    """Create an empty per-jurisdiction rule-set draft."""
    if db.query(RuleSet.id).filter(RuleSet.code == body.code).first():
        raise api_error(
            409, "RULE_SET_EXISTS", f"rule set {body.code!r} already exists"
        )
    rs = RuleSet(
        code=body.code,
        jurisdiction=body.jurisdiction,
        name=body.name,
        draft_base_version=0,
        draft_rules=[],
    )
    db.add(rs)
    db.commit()
    db.refresh(rs)
    return serialize_draft(rs)


@router.get("")
def list_rule_sets(
    jurisdiction: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(RuleSet).order_by(RuleSet.code)
    if jurisdiction:
        q = q.filter(RuleSet.jurisdiction == jurisdiction.strip().upper())
    return [serialize_draft(rs) for rs in q.all()]


@router.get("/{code}")
def get_rule_set(code: str, db: Session = Depends(get_db)):
    return serialize_draft(_get_rule_set_or_404(db, code))


@router.put("/{code}")
def submit_rule_set(code: str, body: RuleSetSubmit, db: Session = Depends(get_db)):
    """Replace the whole draft with ppm-normalized rules.

    Overlaps are allowed; only within-submission duplicates and a stale
    ``base_version`` are rejected here. Action conflicts surface at publish.
    """
    rs = _get_rule_set_or_404(db, code)
    if body.base_version != rs.draft_base_version:
        raise _conflict(rs.code, rs.draft_base_version)
    try:
        normalized = normalize_rules(body.rules)
    except ValueError as exc:
        raise api_error(
            422,
            "DUPLICATE_RULE_CODE",
            f"rule code {exc.args[0]!r} appears more than once in the submission",
            duplicate=exc.args[0],
        )
    if not _cas_advance(db, rs, body.base_version,
                        draft_rules=normalized, updated_at=utcnow()):
        db.rollback()
        db.refresh(rs)
        raise _conflict(rs.code, rs.draft_base_version)
    db.commit()
    db.refresh(rs)
    return serialize_draft(rs)


@router.post("/{code}/publish")
def publish_rule_set(code: str, body: RuleSetPublish, db: Session = Depends(get_db)):
    """Freeze the draft into an immutable, digest-addressed version.

    Overlapping conflicts block the release: a stable, sorted conflict report is
    returned and nothing is written. A stale ``base_version`` is a 409.
    """
    rs = _get_rule_set_or_404(db, code)
    if body.base_version != rs.draft_base_version:
        raise _conflict(rs.code, rs.draft_base_version)
    if not rs.draft_rules:
        raise api_error(
            422,
            "EMPTY_RULE_SET",
            f"rule set {rs.code!r} has no draft rules; add rules before publishing",
        )
    conflicts = detect_conflicts(rs.draft_rules)
    if conflicts:
        raise api_error(
            409,
            "RULE_SET_CONFLICT",
            "rule set draft contains overlapping rules with conflicting actions",
            conflicts=[c.as_dict() for c in conflicts],
        )

    version = (rs.versions[-1].version + 1) if rs.versions else 1
    digest = rules_digest(rs.draft_rules)
    rsv = RuleSetVersion(
        rule_set_id=rs.id,
        version=version,
        digest=digest,
        rule_count=len(rs.draft_rules),
        summary=rules_summary(rs.draft_rules),
        rules=rs.draft_rules,
    )
    db.add(rsv)
    # Publishing is itself an accepted write: advance the optimistic counter
    # atomically so in-flight submitters must rebase onto the released snapshot.
    if not _cas_advance(db, rs, body.base_version, updated_at=utcnow()):
        db.rollback()
        db.refresh(rs)
        raise _conflict(rs.code, rs.draft_base_version)
    db.commit()
    db.refresh(rsv)
    return serialize_version(rs, rsv)


@router.get("/{code}/versions")
def list_rule_set_versions(code: str, db: Session = Depends(get_db)):
    rs = _get_rule_set_or_404(db, code)
    return [serialize_version(rs, v, include_rules=False) for v in rs.versions]


@router.get("/{code}/versions/{version}")
def get_rule_set_version(code: str, version: int, db: Session = Depends(get_db)):
    rs = _get_rule_set_or_404(db, code)
    return serialize_version(rs, _get_version_or_404(rs, version))
