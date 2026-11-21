"""
GRN quantity extraction that respects the *Accepted* column.

The generic invoice extractor reads the first quantity column it finds. On a GRN
that is usually "PO Qty", which would let a vendor bill for goods that were never
received/accepted. This helper locates the "Accepted" header, derives its column
bounds from the neighbouring headers, and reads the numbers underneath it.
"""
import re
from typing import List

_NUMERIC = re.compile(r"^-?\d[\d,]*\.?\d*$")
_NOT_LABELS = {"qty", "quantity", "nos", "no", "pcs"}


def _center_x(word) -> float:
    return (word[0] + word[2]) / 2.0


def _center_y(word) -> float:
    return (word[1] + word[3]) / 2.0


def _to_number(text: str):
    cleaned = text.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_accepted_quantities(pdf_bytes: bytes) -> List[float]:
    """
    Returns the accepted quantity per item row, in document order.
    Empty list when the document has no recognisable "Accepted" column.
    """
    try:
        import pymupdf
    except ImportError:
        return []

    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []
    if len(doc) == 0:
        doc.close()
        return []

    page = doc[0]
    words = page.get_text("words")
    doc.close()
    if not words:
        return []

    accepted = next((w for w in words if str(w[4]).strip().lower().startswith("accepted")), None)
    if accepted is None:
        return []

    ax, ay = _center_x(accepted), _center_y(accepted)

    # Header band: other column labels sitting on the same line (±18pt).
    header_centers = []
    for word in words:
        text = str(word[4]).strip().lower()
        if abs(_center_y(word) - ay) > 18:
            continue
        if text in _NOT_LABELS or _NUMERIC.match(text):
            continue
        if len(text) < 3:
            continue
        header_centers.append(_center_x(word))

    left = max([c for c in header_centers if c < ax], default=ax - 45)
    right = min([c for c in header_centers if c > ax], default=ax + 45)
    left_bound = (left + ax) / 2.0
    right_bound = (right + ax) / 2.0

    # Cluster body words into rows by y (words of wrapped descriptions share y).
    body = [w for w in words if _center_y(w) > ay + 10]
    body.sort(key=lambda w: (_center_y(w), w[0]))
    rows = []
    for word in body:
        y = _center_y(word)
        if rows and abs(rows[-1]["y"] - y) <= 6:
            rows[-1]["words"].append(word)
        else:
            rows.append({"y": y, "words": [word]})

    quantities: List[float] = []
    for row in rows:
        tokens = [w for w in row["words"] if left_bound <= _center_x(w) <= right_bound]
        for token in tokens:
            text = str(token[4]).strip()
            if not _NUMERIC.match(text):
                continue
            value = _to_number(text)
            if value is not None:
                quantities.append(value)
                break
    return quantities
