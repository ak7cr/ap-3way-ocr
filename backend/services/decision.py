"""
The auto-approval decision: approve, review or reject.

Rules (deliberately asymmetric):
  * APPROVE is strict — validation passed, vendor known, PO open, every match
    check clean, and the vendor has earned auto-approval out of shadow mode.
  * REJECT is narrow — only hard failures that no human should be asked to
    adjudicate: an unverifiable GSTIN, a PO that does not exist.
  * REVIEW is everything else. When in doubt, a human looks at it.

This module is pure: it takes facts and policy and returns a decision. It never
reads or writes the database, which keeps it trivially testable.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from ..config import SHADOW, ShadowPolicy, normalise_template_status

APPROVE = "APPROVE"
REVIEW = "REVIEW"
REJECT = "REJECT"

# Only these justify rejecting an invoice outright.
HARD_REJECT_CODES = {
    "INVALID_SELLER_GSTIN_FORMAT",
    "GSTIN_CHECKSUM_INVALID",
    "PO_NOT_FOUND",
    "PO_NUMBER_MISSING",
}

# Reasons a human must always see, even when the arithmetic passed.
REVIEW_CODES = {
    "LINE_UNMAPPED",
    "QTY_EXCEEDS_GRN",
    "QTY_EXCEEDS_PO",
    "GRN_MISSING",
    "RATE_TOLERANCE_EXCEEDED",
    "TAX_RATE_MISMATCH",
    "TAX_MISMATCH",
    "TOTAL_MISMATCH",
    "VENDOR_MISMATCH",
    "VENDOR_PO_MISMATCH",
    "UNKNOWN_VENDOR",
    "PO_NOT_OPEN",
    "CURRENCY_MISMATCH",
    "MISSING_INVOICE_NUMBER",
    "MISSING_INVOICE_LINES",
}

MATCHED = "MATCHED"


@dataclass
class Decision:
    """The system's recommendation, always recorded — even in shadow mode."""

    decision: str
    reason_codes: List[str] = field(default_factory=list)
    explanation: str = ""
    auto_approve_allowed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason_codes": self.reason_codes,
            "explanation": self.explanation,
            "auto_approve_allowed": self.auto_approve_allowed,
        }


def _vendor_earned_auto_approval(
    vendor_agreement: Optional[Dict[str, Any]],
    policy: ShadowPolicy,
) -> bool:
    """
    A vendor is auto-approvable only in live mode and only with enough observed
    agreement. `vendor_agreement` comes from the shadow ledger.
    """
    if not policy.auto_approve_enabled:
        return False
    if not vendor_agreement:
        return not policy.onboarding_vendors
    decisions = int(vendor_agreement.get("decisions") or 0)
    agreement = float(vendor_agreement.get("agreement") or 0.0)
    return decisions >= policy.min_decisions and agreement >= policy.min_agreement


def decide(
    *,
    validation_passed: bool,
    validation_errors: Optional[Iterable[str]] = None,
    validation_warnings: Optional[Iterable[str]] = None,
    match_status: Optional[str],
    reason_codes: Optional[Iterable[str]] = None,
    vendor_known: bool = False,
    po_exists: bool = False,
    po_open: bool = False,
    template_status: Optional[str] = None,
    vendor_agreement: Optional[Dict[str, Any]] = None,
    policy: Optional[ShadowPolicy] = None,
) -> Decision:
    """Turns the validation verdict, match result and policy into a decision."""
    policy = policy or SHADOW
    reasons = list(reason_codes or [])
    validation_errors = list(validation_errors or [])
    validation_warnings = list(validation_warnings or [])

    # ---- REJECT: hard failures only -------------------------------------
    hard = sorted(set(reasons) & HARD_REJECT_CODES)
    gstin_failures = sorted(
        {code for code in validation_errors if code in {"GSTIN_CHECKSUM_INVALID", "INVALID_SELLER_GSTIN_FORMAT"}}
    )
    if gstin_failures:
        return Decision(
            REJECT,
            sorted(dict.fromkeys(gstin_failures + hard)),
            "The vendor GSTIN cannot be verified, so the invoice cannot be attributed to a payee. "
            "Reject it and ask the vendor to reissue with a valid GSTIN.",
        )
    if hard:
        return Decision(
            REJECT,
            hard,
            "Hard failure: " + ", ".join(code.replace("_", " ").lower() for code in hard) + ".",
        )

    # ---- REVIEW: anything not proven clean ------------------------------
    blockers: List[str] = []
    if not validation_passed:
        blockers.extend(validation_errors or ["VALIDATION_FAILED"])
    if match_status != MATCHED:
        blockers.extend(reasons or [f"MATCH_{match_status or 'NOT_RUN'}"])
    else:
        blockers.extend([code for code in reasons if code in REVIEW_CODES])
    if not vendor_known:
        blockers.append("UNKNOWN_VENDOR")
    if po_exists and not po_open:
        blockers.append("PO_NOT_OPEN")
    if template_status and normalise_template_status(template_status) == "CANDIDATE":
        blockers.append("CANDIDATE_TEMPLATE")

    blockers = sorted(dict.fromkeys(blockers))
    if blockers:
        return Decision(
            REVIEW,
            blockers,
            "Needs a human check: " + ", ".join(code.replace("_", " ").lower() for code in blockers[:6]) + ".",
            auto_approve_allowed=False,
        )

    # ---- APPROVE: everything clean, but shadow mode still holds it -------
    warnings_note = f" {len(validation_warnings)} warning(s) noted." if validation_warnings else ""
    if _vendor_earned_auto_approval(vendor_agreement, policy):
        return Decision(
            APPROVE,
            [],
            f"Validation passed, every match check agrees, and the vendor has earned auto-approval.{warnings_note}",
            auto_approve_allowed=True,
        )

    if policy.auto_approve_enabled:
        explanation = "Clean match, but this vendor has not yet built enough agreement history for automatic approval."
    else:
        explanation = (
            "Clean match. Shadow mode is on, so a human confirms the first weeks of decisions "
            "while the system's recommendation is recorded beside them."
        )
    return Decision(REVIEW, ["SHADOW_MODE" if not policy.auto_approve_enabled else "VENDOR_NOT_EARNED"], explanation)
