"""
Document type detection for mixed uploads.

A user should be able to drop a PO, a GRN and an invoice into one upload box and
have the system figure out which is which. Classification is keyword/structure
based; when the score is weak, an LLM can break the tie (see llm_assist).
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PURCHASE_ORDER = "PURCHASE_ORDER"
GRN = "GRN"
INVOICE = "INVOICE"
UNKNOWN = "UNKNOWN"

DOC_TYPE_LABELS = {
    PURCHASE_ORDER: "Purchase order",
    GRN: "Goods receipt",
    INVOICE: "Invoice",
    UNKNOWN: "Unrecognised",
}

# phrase -> weight, per document type
_SIGNALS: Dict[str, Dict[str, int]] = {
    PURCHASE_ORDER: {
        "purchase order": 3,
        "po no": 2,
        "po date": 2,
        "delivery date": 1,
        "to (supplier)": 3,
        "supplier": 1,
        "total order value": 2,
        "order qty": 1,
        "delivery schedule": 2,
        "net total": 1,
        "net value": 1,
        "ship to": 1,
        "purchase manager": 1,
    },
    GRN: {
        "goods receipt": 3,
        "goods received": 3,
        "goods received note": 3,
        "grn no": 3,
        "grn date": 2,
        "material receipt": 3,
        "mrr no": 3,
        "accepted qty": 2,
        "qty accepted": 2,
        "received qty": 2,
        "qty received": 2,
        "rejected qty": 1,
        "challan": 1,
    },
    INVOICE: {
        "tax invoice": 3,
        "invoice no": 2,
        "invoice #": 3,
        "invoice date": 2,
        "bill to": 2,
        "bill no": 2,
        "bill / cash": 3,
        "cash memo": 3,
        "your order no": 1,
        "invoice": 1,
        "irn": 2,
        "amount chargeable": 2,
        "cgst": 1,
        "sgst": 1,
        "igst": 1,
        "grand total": 1,
    },
}

GSTIN_RE = re.compile(r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")

_NUMBER_PATTERNS = {
    PURCHASE_ORDER: [
        re.compile(r"P\.?O\.?\s*No\.?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)", re.I),
        re.compile(r"Purchase\s*Order\s*(?:No\.?|Number|#)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)", re.I),
    ],
    GRN: [
        re.compile(r"GRN\s*No\.?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)", re.I),
        re.compile(r"MRR\s*No\.?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)", re.I),
        re.compile(r"(?:Goods\s*Receipt|Material\s*Receipt)\s*(?:Note|Report)?\s*(?:No\.?|Number)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)", re.I),
    ],
    INVOICE: [
        re.compile(r"Invoice\s*(?:No\.?|Number|#|Id)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)", re.I),
        re.compile(r"Bill\s*(?:No\.?|Number|#)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)", re.I),
    ],
}

_PO_REF_RE = re.compile(
    r"(?:Against|Vide|Ref(?:erence)?)\s*"
    r"(?:P\.?O\.?|Purchase\s*Order|Buyer'?s\s*Order|Your\s*Order)\s*"
    r"(?:No\.?|Number|Ref(?:erence)?|#)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)"
    r"|(?:P\.?O\.?\s*Ref(?:erence)?|P\.?O\.?\s*(?:No\.?|Number|#)|Purchase\s*Order\s*(?:No\.?|Number|#)|"
    r"Buyer'?s\s*Order\s*No\.?|Your\s*Order\s*No\.?)\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\.]*)",
    re.I,
)
_PO_REF_STOPWORDS = {"no", "number", "date", "ref", "reference", "value", "qty", "amount"}
# Fallbacks when the label and the value land on different OCR lines.
_PO_TOKEN_RE = re.compile(r"\b[A-Za-z]{2,}/PO/[A-Za-z0-9][A-Za-z0-9\-]*(?:/[A-Za-z0-9\-]+)?\b", re.I)
_INVOICE_TOKEN_RE = re.compile(r"\b[A-Za-z]{2,}/[0-9]{2}-[0-9]{2}/[0-9]{3,}\b", re.I)
_DATE_VALUE = (
    r"(\d{1,2}[\/\.\-][A-Za-z0-9]{2,9}[\/\.\-]\d{2,4}|\d{1,2}[\s\-][A-Za-z]{3,9}[\s\-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4})"
)
_DATE_PATTERNS = [
    re.compile(r"(?:P\.?O\.?|GRN|MRR|Invoice)\s*Date\s*[:\-]?\s*" + _DATE_VALUE, re.I),
    re.compile(r"Dated\s*[:\-]?\s*" + _DATE_VALUE, re.I),
    re.compile(r"Date\s*[:\-]\s*" + _DATE_VALUE, re.I),
]
_VENDOR_RE = re.compile(
    r"(?:To\s*\(Supplier\)|Supplier|Vendor|Party)\s*[:\-]?\s*\n?\s*([A-Z][^\n]{2,80})",
    re.I,
)
_BUYER_RE = re.compile(r"(?:Buyer|Bill\s*To|Consignee)\s*[:\-]?\s*\n?\s*([A-Z][^\n]{2,80})", re.I)
_VENDOR_LABEL_RE = re.compile(r"(?:To\s*\(Supplier\)|Supplier|Vendor|Party)\s*[:\-]", re.I)
_TRAILING_LABEL_RE = re.compile(
    r"\s+(?:PO|GRN|MRR|Invoice|Delivery|Order)\s*(?:No\.?|Date|Number)\b.*$", re.I
)


@dataclass
class Classification:
    doc_type: str
    confidence: float
    signals: List[str] = field(default_factory=list)
    scores: Dict[str, int] = field(default_factory=dict)
    document_number: Optional[str] = None
    document_date: Optional[str] = None
    po_reference: Optional[str] = None
    vendor_name: Optional[str] = None
    buyer_name: Optional[str] = None
    gstins: List[str] = field(default_factory=list)
    needs_llm: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "doc_type_label": DOC_TYPE_LABELS.get(self.doc_type, "Unrecognised"),
            "confidence": round(self.confidence, 3),
            "signals": self.signals,
            "scores": self.scores,
            "document_number": self.document_number,
            "document_date": self.document_date,
            "po_reference": self.po_reference,
            "vendor_name": self.vendor_name,
            "buyer_name": self.buyer_name,
            "gstins": self.gstins,
            "needs_llm": self.needs_llm,
        }


def _score(text: str) -> Dict[str, int]:
    # OCR may wrap one label over multiple lines; classify against normalized
    # whitespace while keeping the original text for field extraction.
    haystack = re.sub(r"\s+", " ", (text or "").upper()).strip()
    # "P.O. No" and "PO No" mean the same thing; compare against a dot-free copy.
    flattened = haystack.replace(".", "")
    scores = {doc_type: 0 for doc_type in (PURCHASE_ORDER, GRN, INVOICE)}
    for doc_type, phrases in _SIGNALS.items():
        for phrase, weight in phrases.items():
            needle = phrase.upper()
            if needle in haystack or needle.replace(".", "") in flattened:
                scores[doc_type] += weight
    return scores


def _first_match(patterns, text: str) -> Optional[str]:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            raw = next((g for g in match.groups() if g), "") if match.groups() else match.group(0)
            value = (raw or "").strip(" .:-")
            if value and value.lower() not in _PO_REF_STOPWORDS:
                return value
    return None


def _plausible_number(value: Optional[str]) -> bool:
    return bool(value) and len(value) >= 5 and bool(re.search(r"\d", value))


def _first_plausible(patterns, text: str) -> Optional[str]:
    for pattern in patterns:
        for match in pattern.finditer(text):
            raw = next((g for g in match.groups() if g), "")
            value = (raw or "").strip(" .:-")
            if _plausible_number(value) and value.lower() not in _PO_REF_STOPWORDS:
                return value
    return None


def _vendor_from_lines(raw_text: str) -> Optional[str]:
    """
    Read the supplier from the line after its label.

    On a PO the label and value often land on different OCR lines
    ("To (Supplier): PO No.: ..." then "Kaveri Fasteners & Tools PO Date: ...").
    """
    label = _VENDOR_LABEL_RE.search(raw_text or "")
    if label is None:
        return None
    lines = [line.strip() for line in (raw_text or "")[label.end():].splitlines() if line.strip()]
    for line in lines[:5]:
        candidate = _TRAILING_LABEL_RE.sub("", line).strip()
        name = _clean_name(candidate)
        if name and len(re.findall(r"[A-Za-z]{2,}", name)) >= 2:
            return name
    return None


def _clean_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    name = value.strip(" .:-")
    if re.search(r"^(?:No|Date|GSTIN|Ph|Email)\b", name, re.I):
        return None
    # A label cell such as "PO No.: SVAC/..." is not a vendor name.
    if re.search(r"\b(?:No|Date|GSTIN|Ph|Email|Ref)\b\s*[:.]", name, re.I):
        return None
    if re.search(r"\d{3,}", name):
        return None
    return name if 2 <= len(name) <= 80 else None


def classify_document(raw_text: str, invoice: Any = None) -> Classification:
    """Infers PO / GRN / Invoice from the OCR text and extracted fields."""
    text_upper = re.sub(r"\s+", " ", (raw_text or "").upper()).strip()
    scores = _score(text_upper)
    total = sum(scores.values())
    best_type = max(scores, key=scores.get)
    best = scores[best_type]
    second = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0

    confidence = (best / total) if total else 0.0
    needs_llm = best < 4 or (best - second) < max(3, 0.4 * best)

    doc_type = best_type if not needs_llm else UNKNOWN
    if doc_type == UNKNOWN:
        # A weak/ambiguous score is not a confident classification.
        confidence = min(confidence, 0.4)
    # Extracted metadata can settle a weak keyword score.
    has_invoice_wording = bool(re.search(r"\b(?:TAX\s+)?INVOICE\b", text_upper))
    if needs_llm and invoice is not None and has_invoice_wording:
        meta = getattr(invoice, "metadata", None)
        if meta and (meta.invoice_number or "").strip():
            doc_type = INVOICE
            confidence = max(confidence, 0.55)
            needs_llm = False
        elif re.search(r"P\.?O\.?\s*No", text_upper) and re.search(r"GRN|GOODS\s*RECEIPT|MATERIAL\s*RECEIPT", text_upper):
            doc_type = GRN
            confidence = max(confidence, 0.55)
            needs_llm = False

    classification = Classification(
        doc_type=doc_type,
        confidence=confidence,
        signals=[
            p
            for p in _SIGNALS.get(best_type, {})
            if re.sub(r"\s+", " ", p.upper()) in text_upper
        ],
        scores=scores,
        needs_llm=needs_llm,
        gstins=GSTIN_RE.findall((raw_text or "").upper()),
    )

    if doc_type != UNKNOWN:
        classification.document_number = _first_plausible(_NUMBER_PATTERNS[doc_type], raw_text)
        if not classification.document_number and doc_type == INVOICE:
            classification.document_number = _first_match([_INVOICE_TOKEN_RE], raw_text)
        classification.document_date = _first_match(_DATE_PATTERNS, raw_text)
        if doc_type != PURCHASE_ORDER:
            classification.po_reference = _first_match([_PO_REF_RE], raw_text) or _first_match(
                [_PO_TOKEN_RE], raw_text
            )
        vendor_match = _VENDOR_RE.search(raw_text or "")
        classification.vendor_name = _clean_name(vendor_match.group(1) if vendor_match else None) or _vendor_from_lines(
            raw_text
        )
        if not classification.vendor_name and doc_type == INVOICE and invoice is not None:
            classification.vendor_name = _clean_name(getattr(getattr(invoice, "seller", None), "name", None))
        buyer_match = _BUYER_RE.search(raw_text or "")
        classification.buyer_name = _clean_name(buyer_match.group(1) if buyer_match else None)

    return classification
