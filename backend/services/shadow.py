"""
Shadow mode ledger.

Every document gets a recorded recommendation from the engine. While the system
is in shadow mode a human also decides, and both are stored. Agreement per
vendor is then measured from real data before auto-approval is switched on for
that vendor — which is what makes "we can auto-approve this vendor" a claim
backed by evidence rather than optimism.
"""
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import SHADOW, ShadowPolicy
from ..database import DecisionObservation

AGREED = "AGREED"
DISAGREED = "DISAGREED"
PENDING = "PENDING"


def record_recommendation(
    db: Session,
    *,
    tenant_id: str,
    document_id: Optional[int],
    vendor_id: Optional[int],
    recommendation: str,
    reason_codes: Optional[List[str]] = None,
    confidence: Optional[float] = None,
    match_result_id: Optional[int] = None,
    extraction_log_id: Optional[int] = None,
) -> DecisionObservation:
    """Stores the engine's recommendation. The human decision follows later."""
    observation = DecisionObservation(
        tenant_id=tenant_id,
        document_id=document_id,
        vendor_id=vendor_id,
        match_result_id=match_result_id,
        extraction_log_id=extraction_log_id,
        recommendation=recommendation,
        reason_codes=list(reason_codes or []),
        confidence=confidence,
        agreement=PENDING,
    )
    db.add(observation)
    db.flush()
    return observation


def record_human_decision(
    db: Session,
    *,
    observation_id: Optional[int] = None,
    document_id: Optional[int] = None,
    human_decision: str,
    actor: Optional[str] = None,
    note: Optional[str] = None,
) -> Optional[DecisionObservation]:
    """
    Attaches the human's decision. Agreement is measured at the level that
    matters operationally: did the human let it through when we said approve,
    or hold it back when we said review/reject?
    """
    observation = None
    if observation_id is not None:
        observation = db.query(DecisionObservation).filter(DecisionObservation.id == observation_id).first()
    elif document_id is not None:
        observation = (
            db.query(DecisionObservation)
            .filter(DecisionObservation.document_id == document_id)
            .filter(DecisionObservation.agreement == PENDING)
            .order_by(DecisionObservation.id.desc())
            .first()
        )
    if observation is None:
        return None

    observation.human_decision = human_decision
    observation.actor = actor
    observation.note = note
    observation.agreement = AGREED if _same_intent(observation.recommendation, human_decision) else DISAGREED
    db.flush()
    return observation


def _same_intent(recommendation: Optional[str], human_decision: Optional[str]) -> bool:
    """
    Compares what the human did with what we suggested. Approving a document we
    recommended for review is agreement (we were appropriately cautious);
    approving something we rejected outright is not.
    """
    recommendation = (recommendation or "").upper()
    human_decision = (human_decision or "").upper()
    if not recommendation or not human_decision:
        return False
    if human_decision == "APPROVE":
        return recommendation in {"APPROVE", "REVIEW"}
    if human_decision in {"REVIEW", "HOLD"}:
        return recommendation in {"REVIEW", "REJECT"}
    if human_decision == "REJECT":
        return recommendation == "REJECT"
    return False


def vendor_agreement(db: Session, tenant_id: str, vendor_id: Optional[int]) -> Dict[str, Any]:
    """Observed agreement for one vendor, used to gate auto-approval."""
    if vendor_id is None:
        return {"vendor_id": None, "decisions": 0, "agreements": 0, "agreement": 0.0, "earned": False}

    rows = (
        db.query(DecisionObservation.agreement, func.count(DecisionObservation.id))
        .filter(DecisionObservation.tenant_id == tenant_id)
        .filter(DecisionObservation.vendor_id == vendor_id)
        .filter(DecisionObservation.agreement.in_([AGREED, DISAGREED]))
        .group_by(DecisionObservation.agreement)
        .all()
    )
    counts = {agreement: count for agreement, count in rows}
    agreements = int(counts.get(AGREED, 0))
    disagreements = int(counts.get(DISAGREED, 0))
    decisions = agreements + disagreements
    rate = round(agreements / decisions, 4) if decisions else 0.0
    return {
        "vendor_id": vendor_id,
        "decisions": decisions,
        "agreements": agreements,
        "disagreements": disagreements,
        "agreement": rate,
        "earned": decisions >= SHADOW.min_decisions and rate >= SHADOW.min_agreement,
        "policy": {
            "min_decisions": SHADOW.min_decisions,
            "min_agreement": SHADOW.min_agreement,
            "mode": SHADOW.mode,
        },
    }


def agreement_report(db: Session, tenant_id: str, policy: Optional[ShadowPolicy] = None) -> Dict[str, Any]:
    """Per-vendor agreement table plus the totals, for the metrics/review screens."""
    policy = policy or SHADOW
    from ..database import Vendor

    rows = (
        db.query(
            DecisionObservation.vendor_id,
            DecisionObservation.agreement,
            func.count(DecisionObservation.id),
        )
        .filter(DecisionObservation.tenant_id == tenant_id)
        .group_by(DecisionObservation.vendor_id, DecisionObservation.agreement)
        .all()
    )

    buckets: Dict[Optional[int], Dict[str, int]] = {}
    for vendor_id, agreement, count in rows:
        bucket = buckets.setdefault(vendor_id, {})
        bucket[agreement or PENDING] = bucket.get(agreement or PENDING, 0) + int(count)

    names = {}
    vendor_ids = [vid for vid in buckets if vid is not None]
    if vendor_ids:
        for vendor in db.query(Vendor).filter(Vendor.id.in_(vendor_ids)).all():
            names[vendor.id] = vendor.name

    vendors: List[Dict[str, Any]] = []
    total_decisions = total_agreements = 0
    for vendor_id, bucket in buckets.items():
        agreements = bucket.get(AGREED, 0)
        disagreements = bucket.get(DISAGREED, 0)
        decisions = agreements + disagreements
        total_decisions += decisions
        total_agreements += agreements
        vendors.append(
            {
                "vendor_id": vendor_id,
                "vendor": names.get(vendor_id, "Unresolved vendor" if vendor_id is None else f"Vendor #{vendor_id}"),
                "observations": sum(bucket.values()),
                "decisions": decisions,
                "agreements": agreements,
                "disagreements": disagreements,
                "pending": bucket.get(PENDING, 0),
                "agreement": round(agreements / decisions, 4) if decisions else 0.0,
                "earned": decisions >= policy.min_decisions
                and (agreements / decisions if decisions else 0.0) >= policy.min_agreement,
            }
        )
    vendors.sort(key=lambda v: (-v["agreement"], -v["decisions"]))

    return {
        "mode": policy.mode,
        "auto_approve_enabled": policy.auto_approve_enabled,
        "thresholds": {"min_decisions": policy.min_decisions, "min_agreement": policy.min_agreement},
        "total_decisions": total_decisions,
        "overall_agreement": round(total_agreements / total_decisions, 4) if total_decisions else 0.0,
        "vendors": vendors,
    }
