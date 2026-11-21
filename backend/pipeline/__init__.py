"""
The document pipeline, as a modular monolith.

One codebase, one deployable, clear internal boundaries. Each stage below names
the module that owns it, so the seams are explicit even though everything runs
in one process. The heavy stage (extraction) can be moved onto dedicated
stateless workers first — see `backend/jobs.py` — because it already takes its
input as bytes and returns a plain result.

    intake        -> storage, document record, job row
    extraction    -> OCR / template / AI fallback (the expensive part)
    validation    -> validation_gate: arithmetic + format checks
    normalization -> normalization: vendor, item codes, units, line mapping
    matching      -> matching_engine (pure) driven by matching_service
    review        -> exception queue, decisions, shadow ledger
    approval      -> approval rules, segregation of duties
    export        -> payment CSV / Tally XML
"""
from typing import Any, Dict

STAGES: Dict[str, str] = {
    "intake": "backend.services.storage + backend.jobs",
    "extraction": "backend.main (OCR pipeline) + backend.services.template_engine",
    "validation": "backend.services.validation_gate",
    "normalization": "backend.services.normalization + backend.services.canonical",
    "matching": "backend.services.matching_engine + backend.services.matching_service",
    "review": "backend.services.matching_service + backend.services.decision + backend.services.shadow",
    "approval": "backend.services.matching_service",
    "export": "backend.services.matching_service",
}


def describe() -> Dict[str, Any]:
    """Stage map for operators and the /api/threeway/config endpoint."""
    return {"architecture": "modular_monolith", "stages": dict(STAGES)}
