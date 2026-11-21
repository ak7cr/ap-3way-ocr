"""
Central configuration for the OCR pipeline and the 3-way match engine.

Every threshold and tolerance lives here so it can be tuned with real data
instead of being scattered as magic numbers across request handlers.
Environment variables can override the values.
"""
import os
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Extraction escalation thresholds
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EscalationThresholds:
    """Confidence bands used to decide what happens to an extracted document."""

    auto_accept: float = 0.98        # SUCCESS without a human touching it
    review_floor: float = 0.70       # below this the document is FAILED
    template_match: float = 0.85     # layout similarity needed to reuse a template
    ai_accept: float = 0.85          # AI fallback confidence accepted without review
    low_field_confidence: float = 0.70  # field-level flag threshold
    quarantine_failure_rate: float = 0.25
    quarantine_min_runs: int = 5


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ESCALATION = EscalationThresholds(
    auto_accept=_env_float("ESCALATION_AUTO_ACCEPT", 0.98),
    review_floor=_env_float("ESCALATION_REVIEW_FLOOR", 0.70),
    template_match=_env_float("ESCALATION_TEMPLATE_MATCH", 0.85),
    ai_accept=_env_float("ESCALATION_AI_ACCEPT", 0.85),
    low_field_confidence=_env_float("ESCALATION_LOW_FIELD", 0.70),
    quarantine_failure_rate=_env_float("ESCALATION_QUARANTINE_RATE", 0.25),
    quarantine_min_runs=int(_env_float("ESCALATION_QUARANTINE_MIN_RUNS", 5)),
)


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ValidationPolicy:
    """
    The arithmetic/format checks that decide whether an extraction is usable.

    These are deliberately independent of OCR confidence: a layout can score well
    and still be arithmetically wrong, and that is the failure that reaches a
    payment. Only documents that pass here are allowed into matching.
    """

    line_sum_abs: Decimal = Decimal("2.00")      # lines sum vs taxable total
    tax_recompute_abs: Decimal = Decimal("2.00")  # recomputed tax vs stated tax
    grand_total_abs: Decimal = Decimal("2.00")   # taxable + tax vs grand total
    gstin_checksum: bool = True                  # verify the 15th check digit
    require_invoice_number: bool = True
    require_invoice_date: bool = False           # warn by default (many layouts omit it)
    require_po_reference: bool = False           # the match can be given a PO manually
    max_tax_rate_pct: Decimal = Decimal("28.00")


VALIDATION = ValidationPolicy(
    line_sum_abs=Decimal(str(_env_float("VALIDATION_LINE_SUM_ABS", 2.0))),
    tax_recompute_abs=Decimal(str(_env_float("VALIDATION_TAX_ABS", 2.0))),
    grand_total_abs=Decimal(str(_env_float("VALIDATION_TOTAL_ABS", 2.0))),
    gstin_checksum=_env_bool("VALIDATION_GSTIN_CHECKSUM", True),
    require_invoice_number=_env_bool("VALIDATION_REQUIRE_INVOICE_NUMBER", True),
    require_invoice_date=_env_bool("VALIDATION_REQUIRE_INVOICE_DATE", False),
    require_po_reference=_env_bool("VALIDATION_REQUIRE_PO_REFERENCE", False),
)


# ---------------------------------------------------------------------------
# Template trust
# ---------------------------------------------------------------------------
CANDIDATE = "CANDIDATE"
TRUSTED = "TRUSTED"

# Legacy labels kept so existing rows keep working.
_STATUS_ALIASES = {
    "DRAFT": CANDIDATE,
    "ACTIVE": TRUSTED,
    "": CANDIDATE,
}


def normalise_template_status(status: Optional[str]) -> str:
    """Maps a stored template status onto the candidate/trusted/quarantined model."""
    raw = (status or "").strip().upper()
    if raw == "QUARANTINED":
        return "QUARANTINED"
    return _STATUS_ALIASES.get(raw, raw or CANDIDATE)


@dataclass(frozen=True)
class TemplateTrust:
    """
    A learned layout starts as a candidate and is only trusted after repeated
    clean human confirmations. Anything else risks letting one bad extraction
    poison the library for every later document of the same layout.
    """

    candidate_confirmations: int = 3      # clean confirmations before TRUSTED
    auto_learn_local: bool = True         # local extractions may create candidates
    auto_learn_ai: bool = False           # AI output NEVER saves a template on its own
    require_validation: bool = True       # a template needs a passing validation


TEMPLATE_TRUST = TemplateTrust(
    candidate_confirmations=_env_int("TEMPLATE_CANDIDATE_CONFIRMATIONS", 3),
    auto_learn_local=_env_bool("TEMPLATE_AUTO_LEARN_LOCAL", True),
    auto_learn_ai=_env_bool("TEMPLATE_AUTO_LEARN_AI", False),
    require_validation=_env_bool("TEMPLATE_REQUIRE_VALIDATION", True),
)


# ---------------------------------------------------------------------------
# Shadow mode
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ShadowPolicy:
    """
    Shadow mode sends every document to a human while the engine's recommendation
    is recorded beside the human's decision. Auto-approval is only unlocked per
    vendor once observed agreement is high enough over enough samples.
    """

    mode: str = "shadow"                  # shadow | live
    min_decisions: int = 30               # sample size before a vendor can be promoted
    min_agreement: float = 0.95           # observed agreement needed
    onboarding_vendors: bool = True       # new vendors always start in shadow

    @property
    def auto_approve_enabled(self) -> bool:
        return self.mode == "live"


SHADOW = ShadowPolicy(
    mode=os.getenv("SHADOW_MODE", "shadow").strip().lower(),
    min_decisions=_env_int("SHADOW_MIN_DECISIONS", 30),
    min_agreement=_env_float("SHADOW_MIN_AGREEMENT", 0.95),
    onboarding_vendors=_env_bool("SHADOW_ONBOARDING_VENDORS", True),
)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
DEFAULT_TENANT = os.getenv("TENANT_ID", "default")


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QueueSettings:
    max_attempts: int = 3
    claim_batch: int = 5
    worker_id: str = os.getenv("WORKER_ID", "")


QUEUE = QueueSettings(
    max_attempts=_env_int("QUEUE_MAX_ATTEMPTS", 3),
    claim_batch=_env_int("QUEUE_CLAIM_BATCH", 5),
)



def classify_confidence(confidence: Optional[float]) -> str:
    """Maps a confidence value onto the band that decided its fate."""
    if confidence is None:
        return "UNKNOWN"
    if confidence >= ESCALATION.auto_accept:
        return "AUTO_ACCEPT"
    if confidence >= ESCALATION.ai_accept:
        return "REVIEW_AI_BAND"
    if confidence >= ESCALATION.review_floor:
        return "REVIEW"
    return "REJECT_FLOOR"


def threshold_decision(
    confidence: Optional[float],
    threshold_name: str = "auto_accept",
    matched_template_id: Optional[int] = None,
    layout_score: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Builds the record persisted with every document so thresholds can be tuned
    from real data later. Also prints a single grep-able log line.
    """
    band = classify_confidence(confidence)
    decision = {
        "threshold_name": threshold_name,
        "threshold_value": getattr(ESCALATION, threshold_name, None),
        "confidence": confidence,
        "band": band,
        "auto_accept": ESCALATION.auto_accept,
        "review_floor": ESCALATION.review_floor,
        "template_match": ESCALATION.template_match,
        "matched_template_id": matched_template_id,
        "layout_score": layout_score,
    }
    print(
        "🎚️  [Escalation] "
        f"band={band} threshold={threshold_name}@{decision['threshold_value']} "
        f"confidence={confidence} template={matched_template_id} layout_score={layout_score}"
    )
    return decision


# ---------------------------------------------------------------------------
# 3-way match tolerances
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MatchTolerances:
    """Numeric slack allowed before a comparison raises an exception."""

    price_pct: Decimal = Decimal("0.02")     # ±2% on unit rate
    quantity: Decimal = Decimal("0.00")      # exact quantities by default
    line_abs: Decimal = Decimal("1.00")      # ₹1 rounding slack per line
    tax_abs: Decimal = Decimal("1.00")       # ₹1 tax recompute slack
    total_abs: Decimal = Decimal("1.00")     # ₹1 grand total slack

    def merged(self, **overrides: Any) -> "MatchTolerances":
        clean = {k: v for k, v in overrides.items() if v is not None}
        if not clean:
            return self
        decimals = {
            k: (v if isinstance(v, Decimal) else Decimal(str(v)))
            for k, v in clean.items()
        }
        return replace(self, **decimals)


DEFAULT_TOLERANCES = MatchTolerances()

# Per-category overrides, keyed by lower-case category label.
TOLERANCE_OVERRIDES: Dict[str, Dict[str, Any]] = {
    # bulk commodities are billed on weight, allow a little more price drift
    "commodity": {"price_pct": Decimal("0.03")},
    "services": {"price_pct": Decimal("0.00")},
}


def resolve_tolerances(
    vendor_name: Optional[str] = None,
    category: Optional[str] = None,
    vendor_override: Optional[Dict[str, Any]] = None,
) -> MatchTolerances:
    """Precedence: per-vendor config > per-category config > global default."""
    tolerances = DEFAULT_TOLERANCES
    if category and category.lower() in TOLERANCE_OVERRIDES:
        tolerances = tolerances.merged(**TOLERANCE_OVERRIDES[category.lower()])
    if vendor_name and vendor_override:
        tolerances = tolerances.merged(**vendor_override)
    return tolerances


# ---------------------------------------------------------------------------
# Duplicate detection / match behaviour
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MatchSettings:
    duplicate_window_days: int = 180       # how far back to look for the same invoice
    require_grn: bool = True               # 3-way by default
    allow_over_delivery: bool = False


MATCH = MatchSettings(
    duplicate_window_days=int(_env_float("MATCH_DUPLICATE_WINDOW_DAYS", 180)),
    require_grn=os.getenv("MATCH_REQUIRE_GRN", "true").lower() != "false",
)


# ---------------------------------------------------------------------------
# Approval policy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApprovalRule:
    """Amount-band rule: how many approvals a match needs and who may approve."""

    name: str
    min_amount: Decimal
    max_amount: Optional[Decimal]
    approvals_required: int
    roles: List[str] = field(default_factory=list)

    def covers(self, amount: Decimal) -> bool:
        if amount < self.min_amount:
            return False
        return self.max_amount is None or amount <= self.max_amount


APPROVAL_RULES: List[ApprovalRule] = [
    ApprovalRule("auto_band", Decimal("0"), Decimal("25000"), 1, ["finance"]),
    ApprovalRule("manager_band", Decimal("25000.01"), Decimal("250000"), 2, ["finance", "manager"]),
    ApprovalRule("director_band", Decimal("250000.01"), None, 2, ["finance", "director"]),
]

# Exception reason codes that always need a manager, regardless of amount.
APPROVAL_ESCALATED_REASON_CODES = {
    "VENDOR_MISMATCH",
    "PO_NOT_FOUND",
    "DUPLICATE_INVOICE",
    "QTY_EXCEEDS_PO",
}

SEGREGATION_OF_DUTIES = True

# Vendor risk tiers -> required extra role
VENDOR_RISK_APPROVERS: Dict[str, str] = {
    "HIGH": "director",
    "MEDIUM": "manager",
    "LOW": "finance",
}


# ---------------------------------------------------------------------------
# Object storage for document previews
# ---------------------------------------------------------------------------
STORAGE = {
    "backend": os.getenv("STORAGE_BACKEND", "local"),          # local | s3 | minio
    "root": os.getenv("STORAGE_ROOT", os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")),
    "bucket": os.getenv("STORAGE_BUCKET", "invoice-previews"),
    "public_prefix": "/api/files",
}

# LLM is advisory only: it never decides a match or an approval.
LLM_ASSIST_MODEL = os.getenv("LLM_ASSIST_MODEL", "gemini-2.0-flash")
