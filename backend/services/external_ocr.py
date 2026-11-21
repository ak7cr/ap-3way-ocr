"""
External OCR engines for scanned documents.

Measured on the AP sample kit, the two imported engines beat this repo's own
extractor on *scanned images* (`invoice-ocr-c++` 2/40 vs 0/40, and
`invoice-ocr-line-system` 2/40 vs 0/40), while this repo's extractor is far
ahead on digital PDFs (25/40 vs 9/40). So the routing is deliberate:

    images  -> the external engines (best-of, scored)
    PDFs    -> this repo's extractor, untouched

The external engines run as HTTP sidecars (see `npm run dev:ocr`). They are the
upstream projects, imported as-is and left unmodified; we only call their public
`POST /api/extract-invoice` endpoint and map the response onto our schema.
"""
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..schemas import InvoiceData
from .validator import validate_invoice

DEFAULT_SERVICES = "c++=http://127.0.0.1:8021,line-system=http://127.0.0.1:8022"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ExternalOcrSettings:
    enabled: bool = True
    services: List[Tuple[str, str]] = field(default_factory=list)
    timeout: float = 180.0
    health_timeout: float = 2.0

    @property
    def urls(self) -> List[str]:
        return [url for _, url in self.services]


def load_settings() -> ExternalOcrSettings:
    raw = os.getenv("EXTERNAL_OCR_SERVICES", DEFAULT_SERVICES)
    services: List[Tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, url = chunk.split("=", 1)
        else:
            name, url = f"service{len(services) + 1}", chunk
        if url.strip():
            services.append((name.strip(), url.strip().rstrip("/")))
    return ExternalOcrSettings(
        enabled=_env_bool("EXTERNAL_OCR_ENABLED", True),
        services=services,
        timeout=float(os.getenv("EXTERNAL_OCR_TIMEOUT", "180")),
        health_timeout=float(os.getenv("EXTERNAL_OCR_HEALTH_TIMEOUT", "2")),
    )


def is_image(filename: str, content_type: Optional[str] = None) -> bool:
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    if ext in {"png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp"}:
        return True
    return (content_type or "").lower().startswith("image/")


def health(settings: Optional[ExternalOcrSettings] = None) -> List[Dict[str, Any]]:
    """Availability of each sidecar, for the config endpoint and diagnostics."""
    settings = settings or load_settings()
    statuses: List[Dict[str, Any]] = []
    for name, url in settings.services:
        entry: Dict[str, Any] = {"name": name, "url": url, "available": False}
        try:
            response = httpx.get(url + "/", timeout=settings.health_timeout)
            entry["available"] = response.status_code < 500
            entry["status_code"] = response.status_code
        except Exception as exc:  # a dead sidecar is not an error, just unavailable
            entry["error"] = f"{type(exc).__name__}"
        statuses.append(entry)
    return statuses


def completeness_score(invoice: Optional[InvoiceData]) -> Tuple[int, int, float]:
    """
    Ranks a candidate extraction by how much usable data it actually contains.

    The eval scores fields, so the ranking rewards populated fields and clean
    arithmetic rather than raw OCR token count.
    """
    if invoice is None:
        return (0, 0, 0.0)

    meta = invoice.metadata
    summary = invoice.summary
    populated = 0
    for value in (
        getattr(meta, "invoice_number", None),
        getattr(meta, "invoice_date", None),
        getattr(meta, "po_number", None),
        getattr(invoice.seller, "name", None) if invoice.seller else None,
        getattr(invoice.seller, "gstin", None) if invoice.seller else None,
        getattr(summary, "grand_total", None),
        getattr(summary, "subtotal", None),
        getattr(summary, "total_gst", None),
    ):
        if value not in (None, "", 0, 0.0):
            populated += 1

    lines = invoice.line_items or []
    usable_lines = 0
    for line in lines:
        if (line.quantity or 0) > 0 and (line.unit_price or 0) > 0 and (line.total_amount or 0) > 0:
            usable_lines += 1
        if line.item_name or line.description:
            populated += 1
    populated += usable_lines

    try:
        validation = validate_invoice(invoice)
        confidence = validation.overall_confidence
        arithmetic_ok = 1 if validation.passed else 0
    except Exception:
        confidence = 0.0
        arithmetic_ok = 0

    return (arithmetic_ok, populated, confidence)


def extract_with_service(
    name: str,
    url: str,
    content: bytes,
    filename: str,
    content_type: Optional[str],
    settings: ExternalOcrSettings,
) -> Optional[InvoiceData]:
    """Calls one sidecar and maps its response onto our schema."""
    try:
        files = {"file": (filename, content, content_type or "application/octet-stream")}
        data = {"pipeline_mode": os.getenv("EXTERNAL_OCR_PIPELINE_MODE", "local")}
        response = httpx.post(url + "/api/extract-invoice", files=files, data=data, timeout=settings.timeout)
        if response.status_code >= 400:
            print(f"   • {name}: HTTP {response.status_code} — {response.text[:160]}")
            return None
        payload = response.json()
        invoice = InvoiceData(**payload)
        invoice.engine_used = f"External OCR ({name})"
        print(f"   • {name}: {len(invoice.line_items or [])} line(s), score {completeness_score(invoice)}")
        return invoice
    except Exception as exc:
        print(f"   • {name}: {type(exc).__name__}: {exc}")
        return None


def best_external_extraction(
    content: bytes,
    filename: str,
    content_type: Optional[str] = None,
    settings: Optional[ExternalOcrSettings] = None,
) -> Tuple[Optional[InvoiceData], List[Dict[str, Any]]]:
    """
    Tries every configured engine and keeps the best-scoring result.

    Returns (invoice or None, per-service scores) so callers can log why a
    particular engine won.
    """
    settings = settings or load_settings()
    if not settings.enabled or not settings.services:
        return None, []

    best: Optional[InvoiceData] = None
    best_score: Tuple[int, int, float] = (0, 0, 0.0)
    attempts: List[Dict[str, Any]] = []

    for name, url in settings.services:
        invoice = extract_with_service(name, url, content, filename, content_type, settings)
        if invoice is None:
            attempts.append({"name": name, "url": url, "ok": False})
            continue
        score = completeness_score(invoice)
        attempts.append({"name": name, "url": url, "ok": True, "score": list(score), "lines": len(invoice.line_items or [])})
        if score > best_score:
            best, best_score = invoice, score

    return best, attempts
