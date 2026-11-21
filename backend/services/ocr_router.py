"""
Image extraction routing.

Measured on `tests/ap_samples` (scanned JPEGs, scored against ground truth):

    this repo's local engine      0/40
    invoice-ocr-c++               2/40
    invoice-ocr-line-system       2/40

and on digital PDFs the ranking is the other way round (25/40 ours vs 9/40
theirs). So the routing is per input type, and it is deliberate rather than a
guess:

    scanned image  -> the external engines, best-of, then AI/our engine fallback
    digital PDF    -> this repo's extractor, untouched

The external engines are the upstream projects run as HTTP sidecars; they are
never modified, only called. If they are not running the router degrades
silently to the local engine, so a missing sidecar can never break intake.
"""
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..schemas import InvoiceData
from . import external_ocr
from .validator import validate_invoice

# A sidecar result is only accepted if it is at least this good; otherwise the
# local engine gets its turn (and then the AI fallback).
MIN_LINE_ITEMS = 1


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class RoutedExtraction:
    invoice: Optional[InvoiceData]
    engine: str
    attempts: list
    used_external: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "used_external": self.used_external,
            "lines": len(self.invoice.line_items or []) if self.invoice else 0,
            "attempts": self.attempts,
        }


def is_usable(invoice: Optional[InvoiceData]) -> bool:
    """
    Is this extraction worth keeping?

    Cheap on purpose: the validation gate downstream does the real arithmetic
    check. Here we only reject an empty or absurd result so we can decide
    whether to bother with the next engine.
    """
    if invoice is None:
        return False
    lines = invoice.line_items or []
    if len(lines) < MIN_LINE_ITEMS:
        return False
    usable = sum(
        1 for line in lines if (line.quantity or 0) > 0 or (line.unit_price or 0) > 0 or (line.total_amount or 0) > 0
    )
    return usable >= MIN_LINE_ITEMS


def provider_order(filename: str, content_type: Optional[str] = None) -> str:
    """Cheap, testable statement of which engine family serves this input."""
    if not _env_bool("EXTERNAL_OCR_ENABLED", True):
        return "local"
    if external_ocr.is_image(filename, content_type):
        return "external-then-local"
    return "local"


def extract_image_document(
    content: bytes,
    filename: str,
    content_type: Optional[str] = None,
    settings: Optional[external_ocr.ExternalOcrSettings] = None,
) -> RoutedExtraction:
    """
    Extracts a scanned image using the external engines, falling back to the
    local engine in this repo.

    Called by the image branch of the API pipeline and by the eval harness, so
    the measured number is the deployed behaviour.
    """
    attempts: list = []

    if _env_bool("EXTERNAL_OCR_ENABLED", True) and external_ocr.is_image(filename, content_type):
        best, external_attempts = external_ocr.best_external_extraction(
            content, filename, content_type, settings=settings
        )
        attempts.extend(external_attempts)
        if is_usable(best):
            engine = best.engine_used if "External OCR" in (best.engine_used or "") else f"External OCR ({best.engine_used})"
            return RoutedExtraction(best, engine, attempts, True)
        attempts.append({"name": "external", "ok": False, "reason": "no usable external extraction"})

    local = _local_image_extraction(content, filename)
    attempts.append(
        {
            "name": "local",
            "ok": is_usable(local),
            "lines": len(local.line_items or []) if local else 0,
        }
    )
    engine = local.engine_used if local is not None else "none"
    return RoutedExtraction(local, engine, attempts, False)


def _local_image_extraction(content: bytes, filename: str) -> Optional[InvoiceData]:
    try:
        from .invoice_extractor import extract_invoice_from_image

        return extract_invoice_from_image(content, preview_url="")
    except Exception as exc:
        print(f"   • local image engine failed: {type(exc).__name__}: {exc}")
        return None


def score(invoice: Optional[InvoiceData]) -> Tuple[int, int, float]:
    """Exposed for callers that want the same ranking the router uses."""
    return external_ocr.completeness_score(invoice)


def validate_candidate(invoice: Optional[InvoiceData]) -> Dict[str, Any]:
    """Arithmetic verdict for a routed result (used in logs and the report)."""
    if invoice is None:
        return {"passed": False, "errors": ["NO_EXTRACTION"]}
    validation = validate_invoice(invoice)
    return {
        "passed": validation.passed,
        "confidence": validation.overall_confidence,
        "errors": validation.errors,
        "warnings": validation.warnings,
    }
