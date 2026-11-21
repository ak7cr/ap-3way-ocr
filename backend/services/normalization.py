"""
Normalization layer.

Turns messy OCR / ERP text into stable keys the deterministic match engine can
compare: vendor identity, item codes, units of measure and numbers.

The invoice -> PO line mapping lives here too because it is the hardest part of
3-way matching and needs its own deterministic ladder:
item code -> fuzzy description -> rate + quantity.
"""
import re
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .canonical import CanonicalInvoice, CanonicalLine
from .validator import to_decimal

# ---------------------------------------------------------------------------
# Units of measure
# ---------------------------------------------------------------------------
_UOM_CANON = {
    "KG": ["KG", "KGS", "K.G.", "K.G", "KILOGRAM", "KILOGRAMS", "KILO", "KILOS"],
    "GM": ["GM", "GMS", "G", "GRAM", "GRAMS"],
    "MG": ["MG", "MILLIGRAM", "MILLIGRAMS"],
    "L": ["L", "LTR", "LTRS", "LITRE", "LITER", "LITRES", "LITERS"],
    "ML": ["ML", "MILLILITRE", "MILLILITER"],
    "M": ["M", "MTR", "MTRS", "METER", "METERS", "METRE", "METRES"],
    "CM": ["CM", "CENTIMETER", "CENTIMETRE"],
    "FT": ["FT", "FEET", "FOOT"],
    "PCS": ["PCS", "PC", "PIECE", "PIECES", "NOS", "NO", "NO.", "NUMBER", "UNIT", "UNITS", "EA", "EACH"],
    "BOX": ["BOX", "BOXES", "CTN", "CARTON", "CARTONS", "PKT", "PACKET"],
    "SET": ["SET", "SETS"],
    "ROLL": ["ROLL", "ROLLS"],
    "BAG": ["BAG", "BAGS", "SACK", "SACKS"],
    "HR": ["HR", "HRS", "HOUR", "HOURS"],
    "DAY": ["DAY", "DAYS"],
    "SQM": ["SQM", "SQMT", "SQUARE METER", "SQUARE METRE"],
    "DOZEN": ["DOZEN", "DOZ", "DZ"],
    "T": ["T", "TON", "TONS", "TONNE", "TONNES", "MT"],
}

_UOM_LOOKUP: Dict[str, str] = {}
for _canon, _aliases in _UOM_CANON.items():
    for _alias in _aliases:
        _UOM_LOOKUP[re.sub(r"[^A-Z]", "", _alias)] = _canon

_UNIT_NOISE = re.compile(r"[^A-Z]")


def normalize_uom(unit: Optional[str], default: str = "PCS") -> str:
    """Maps KG/kgs/Kilogram/Gms onto a single canonical unit code."""
    if not unit:
        return default
    key = _UNIT_NOISE.sub("", str(unit).upper())
    return _UOM_LOOKUP.get(key, key or default)


# ---------------------------------------------------------------------------
# Item codes and text
# ---------------------------------------------------------------------------
_ITEM_NOISE = re.compile(r"[^A-Z0-9]")


def normalize_item_code(code: Optional[str]) -> Optional[str]:
    """Uppercase and strip separators so 'abc-100 ' == 'ABC 100' == 'ABC100'."""
    if code is None:
        return None
    text = str(code).strip().upper()
    if not text:
        return None
    cleaned = _ITEM_NOISE.sub("", text)
    return cleaned or None


def normalize_hsn(hsn: Optional[str]) -> Optional[str]:
    if not hsn:
        return None
    digits = re.sub(r"\D", "", str(hsn))
    return digits or None


_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^A-Z0-9 ]")


def normalize_text(value: Optional[str]) -> str:
    """Uppercase, collapse whitespace and drop punctuation for fuzzy comparison."""
    if not value:
        return ""
    text = str(value).upper().replace("&", " AND ")
    text = _NON_WORD.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalize_gstin(value: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_currency(value: Optional[str]) -> str:
    if not value:
        return "INR"
    text = str(value).strip().upper()
    return {
        "RS": "INR",
        "RS.": "INR",
        "RUPEE": "INR",
        "RUPEES": "INR",
        "$": "USD",
        "US$": "USD",
        "EURO": "EUR",
        "€": "EUR",
        "£": "GBP",
    }.get(text, text)


def normalize_quantity(value, unit: Optional[str] = None) -> Decimal:
    """Normalizes numbers (including 1.234,56 style) into a Decimal quantity."""
    qty = to_decimal(value)
    if normalize_uom(unit) == "T":
        return qty
    return qty


def similarity(a: Optional[str], b: Optional[str]) -> float:
    """Fuzzy similarity between two free-text descriptions (0..1)."""
    left, right = normalize_text(a), normalize_text(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    base = SequenceMatcher(None, left, right).ratio()
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if left_tokens and right_tokens:
        jaccard = len(left_tokens & right_tokens) / float(len(left_tokens | right_tokens))
        base = max(base, jaccard)
    return round(base, 4)


# ---------------------------------------------------------------------------
# Vendor resolution
# ---------------------------------------------------------------------------
@dataclass
class VendorRef:
    id: Optional[int]
    code: str
    name: str
    gstin: Optional[str] = None
    risk_tier: str = "LOW"
    category: Optional[str] = None
    tolerance_override: Optional[Dict[str, str]] = None


@dataclass
class VendorResolution:
    vendor: Optional[VendorRef]
    method: str = "NONE"          # GSTIN | NAME_EXACT | NAME_FUZZY | NONE
    score: float = 0.0

    @property
    def resolved(self) -> bool:
        return self.vendor is not None


FUZZY_VENDOR_THRESHOLD = 0.86


def resolve_vendor(
    vendors: Sequence[VendorRef],
    name: Optional[str],
    gstin: Optional[str],
) -> VendorResolution:
    """
    Vendor master lookup: GSTIN first (authoritative), then exact name, then fuzzy name.
    Never returns a fuzzy match below the threshold — an unresolved vendor is an
    exception, not a guess.
    """
    wanted_gstin = normalize_gstin(gstin)
    if wanted_gstin:
        for vendor in vendors:
            if normalize_gstin(vendor.gstin) == wanted_gstin:
                return VendorResolution(vendor, "GSTIN", 1.0)

    wanted_name = normalize_text(name)
    if wanted_name:
        for vendor in vendors:
            if normalize_text(vendor.name) == wanted_name:
                return VendorResolution(vendor, "NAME_EXACT", 1.0)

        best: Optional[VendorRef] = None
        best_score = 0.0
        for vendor in vendors:
            score = similarity(vendor.name, name)
            vendor_norm = normalize_text(vendor.name)
            if wanted_name in vendor_norm or vendor_norm in wanted_name:
                score = max(score, 0.93)
            if score > best_score:
                best, best_score = vendor, score
        if best is not None and best_score >= FUZZY_VENDOR_THRESHOLD:
            return VendorResolution(best, "NAME_FUZZY", round(best_score, 4))
        return VendorResolution(None, "NONE", round(best_score, 4))

    return VendorResolution(None, "NONE", 0.0)


# ---------------------------------------------------------------------------
# Invoice -> PO line mapping
# ---------------------------------------------------------------------------
@dataclass
class LineMapping:
    invoice_line: CanonicalLine
    po_line: Optional[CanonicalLine]
    method: str = "NONE"   # ITEM_CODE | DESCRIPTION_FUZZY | RATE_QTY | NONE
    score: float = 0.0

    @property
    def mapped(self) -> bool:
        return self.po_line is not None


FUZZY_LINE_THRESHOLD = 0.72
RATE_QTY_PRICE_TOLERANCE = Decimal("0.02")


def _price_close(a: Decimal, b: Decimal) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / b <= RATE_QTY_PRICE_TOLERANCE


def map_invoice_lines_to_po(
    invoice_lines: Sequence[CanonicalLine],
    po_lines: Sequence[CanonicalLine],
) -> List[LineMapping]:
    """
    Deterministic ladder: item code, then fuzzy description, then rate + quantity.
    Each PO line can be consumed only once, and every invoice line gets a result
    (mapped or not) so the exception queue can show exactly what failed.
    """
    remaining = list(po_lines)
    mappings: List[Optional[LineMapping]] = [None] * len(invoice_lines)

    # Pass 1: item code
    for i, inv_line in enumerate(invoice_lines):
        code = normalize_item_code(inv_line.item_code)
        if not code:
            continue
        for po_line in remaining:
            if normalize_item_code(po_line.item_code) == code:
                mappings[i] = LineMapping(inv_line, po_line, "ITEM_CODE", 1.0)
                remaining.remove(po_line)
                break

    # Pass 2: fuzzy description
    for i, inv_line in enumerate(invoice_lines):
        if mappings[i] or not remaining:
            continue
        best, best_score = None, 0.0
        for po_line in remaining:
            score = similarity(inv_line.description, po_line.description)
            if score > best_score:
                best, best_score = po_line, score
        if best is not None and best_score >= FUZZY_LINE_THRESHOLD:
            mappings[i] = LineMapping(inv_line, best, "DESCRIPTION_FUZZY", best_score)
            remaining.remove(best)

    # Pass 3: rate + quantity (deterministic tie-break for poorly described lines)
    for i, inv_line in enumerate(invoice_lines):
        if mappings[i] or not remaining:
            continue
        inv_qty = to_decimal(inv_line.quantity)
        inv_rate = to_decimal(inv_line.rate)
        best, best_score = None, -1.0
        for po_line in remaining:
            po_qty = to_decimal(po_line.quantity)
            po_rate = to_decimal(po_line.rate)
            if _price_close(inv_rate, po_rate) and inv_qty > 0 and po_qty == inv_qty:
                score = 0.9
            elif _price_close(inv_rate, po_rate):
                score = 0.75
            elif inv_qty > 0 and po_qty == inv_qty:
                score = 0.6
            else:
                continue
            if score > best_score:
                best, best_score = po_line, score
        if best is not None:
            mappings[i] = LineMapping(inv_line, best, "RATE_QTY", best_score)
            remaining.remove(best)

    for i, inv_line in enumerate(invoice_lines):
        if mappings[i] is None:
            mappings[i] = LineMapping(inv_line, None, "NONE", 0.0)

    return [m for m in mappings if m is not None]


# ---------------------------------------------------------------------------
# Whole-invoice normalization
# ---------------------------------------------------------------------------
def normalize_invoice(
    invoice: CanonicalInvoice,
    vendors: Optional[Sequence[VendorRef]] = None,
) -> Tuple[CanonicalInvoice, VendorResolution]:
    """Normalizes codes/units and resolves the vendor (may stay unresolved)."""
    normalized_lines: List[CanonicalLine] = []
    for line in invoice.lines:
        normalized_lines.append(
            line.model_copy(
                update={
                    "item_code": normalize_item_code(line.item_code) or normalize_hsn(line.hsn_sac),
                    "unit": normalize_uom(line.unit),
                    "quantity": float(normalize_quantity(line.quantity, line.unit)),
                    "rate": float(to_decimal(line.rate)),
                    "tax_rate": float(to_decimal(line.tax_rate)),
                    "tax_amount": float(to_decimal(line.tax_amount)),
                    "amount": float(to_decimal(line.amount)),
                    "hsn_sac": normalize_hsn(line.hsn_sac),
                }
            )
        )

    resolution = resolve_vendor(vendors or [], invoice.vendor_name, invoice.vendor_gstin)
    normalized = invoice.model_copy(
        update={
            "lines": normalized_lines,
            "currency": normalize_currency(invoice.currency),
            "vendor_gstin": normalize_gstin(invoice.vendor_gstin) or None,
            "po_number": (invoice.po_number or "").strip() or None,
            "subtotal": float(to_decimal(invoice.subtotal)),
            "total_tax": float(to_decimal(invoice.total_tax)),
            "grand_total": float(to_decimal(invoice.grand_total)),
        }
    )
    return normalized, resolution


def normalize_po_lines(lines: Iterable[CanonicalLine]) -> List[CanonicalLine]:
    result: List[CanonicalLine] = []
    for line in lines:
        result.append(
            line.model_copy(
                update={
                    "item_code": normalize_item_code(line.item_code) or normalize_hsn(line.hsn_sac),
                    "unit": normalize_uom(line.unit),
                    "hsn_sac": normalize_hsn(line.hsn_sac),
                }
            )
        )
    return result
