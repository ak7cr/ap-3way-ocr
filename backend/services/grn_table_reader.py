"""
Read a GRN's line-item table from a scanned image.

The invoice-oriented extractors read the first quantity column they find, which
on a goods receipt is usually "PO Qty" or "Challan Qty" rather than what was
actually accepted. This reader uses the table's own geometry instead:

1. deskew the page using its horizontal rules;
2. detect the horizontal and vertical rules;
3. erase the rules, because they cut through the digits during OCR;
4. read the header row and work out which field each column holds;
5. treat every box between two rules as one cell and read the numbers in it.

When vertical rules are missing it falls back to assigning numbers to the
nearest header column, which is less precise but keeps unruled tables readable.

The module is deterministic and has no AI dependency. ``grn_lines`` holds the
image and OCR primitives.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import grn_lines

QUANTITY_FIELDS = ("po_qty", "challan_qty", "received_qty", "accepted_qty", "rejected_qty")
_FIELD_ORDER = (
    "line_no",
    "item_code",
    "description",
    "uom",
    *QUANTITY_FIELDS,
    "remarks",
)

_HEADER_PHRASES: Dict[str, Tuple[str, ...]] = {
    "received_qty": ("received qty", "qty received", "received quantity", "received"),
    "accepted_qty": ("accepted qty", "qty accepted", "accepted quantity", "accepted"),
    "rejected_qty": ("rejected qty", "qty rejected", "rejected quantity", "rejected"),
    "challan_qty": ("challan qty", "qty as per dc", "as per dc", "challan", "dc qty"),
    "po_qty": ("po qty", "ordered qty", "order qty", "po quantity", "po"),
    "item_code": ("item code", "part number", "part no", "code"),
    "description": ("item description", "description", "particulars", "item"),
    "uom": ("uom", "unit", "units"),
    "remarks": ("condition remarks", "remarks", "condition", "remark"),
    "line_no": ("sl no", "s no", "serial no", "line no", "sno", "sl", "sr no", "sr"),
}

_UNITS = {
    "nos", "no", "set", "sets", "kg", "kgs", "roll", "rolls", "drum", "drums",
    "pail", "pails", "pall", "bale", "bales", "ltr", "l", "pcs", "mtr", "box",
    "boxes", "pkt", "bag", "bags", "ton", "tons", "mt",
}

_CODE_RE = re.compile(r"^[A-Za-z]{2,}[A-Za-z0-9]*[-\/]?[A-Za-z0-9]*$")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]+", " ", text or "")).strip().lower()


def _header_field(text: str) -> Optional[str]:
    """Map a header cell to a logical field, tolerating OCR typos."""
    normalised = _normalise(text)
    if not normalised:
        return None
    best_field: Optional[str] = None
    best_score = 0.0
    for name in _FIELD_ORDER:
        for phrase in _HEADER_PHRASES.get(name, ()):
            if phrase in normalised:
                return name
            score = difflib.SequenceMatcher(None, normalised, phrase).ratio()
            if score > best_score:
                best_score, best_field = score, name
    return best_field if best_score >= 0.8 else None


@dataclass
class GrnTable:
    """Result of reading one image; ``lines`` is empty when nothing was found."""

    lines: List[Dict[str, Any]] = field(default_factory=list)
    columns: Dict[str, int] = field(default_factory=dict)
    header: List[str] = field(default_factory=list)
    angle: float = 0.0
    method: str = "none"
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.lines)

    def to_attempt(self) -> Dict[str, Any]:
        return {
            "engine": "grn_table_reader",
            "ok": self.ok,
            "method": self.method,
            "reason": self.reason or None,
            "lines": len(self.lines),
            "columns": sorted(self.columns),
            "angle": round(self.angle, 2),
        }


def _reading_order(words: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Order words the way a human reads a cell: line by line, then left to right.

    Sorting only by x interleaves the wrapped description rows ("Zinc Hex Bolt
    Plated ..." instead of "Hex Bolt ... Zinc Plated").
    """
    ordered = sorted(words, key=lambda w: (w["cy"], w["x"]))
    lines: List[List[Dict[str, Any]]] = []
    for word in ordered:
        if lines and abs(lines[-1][0]["cy"] - word["cy"]) <= max(8.0, word["h"] * 0.5):
            lines[-1].append(word)
        else:
            lines.append([word])
    out: List[Dict[str, Any]] = []
    for line in lines:
        out.extend(sorted(line, key=lambda w: w["x"]))
    return out


def _cells_in_band(
    words: Sequence[Dict[str, Any]], top: int, bottom: int, left: int, right: int
) -> List[Dict[str, Any]]:
    return _reading_order(
        [w for w in words if top <= w["cy"] < bottom and left <= w["cx"] < right]
    )


def _join(words: Sequence[Dict[str, Any]]) -> str:
    return " ".join(word["text"] for word in words).strip()


def _read_cell_text(
    image: Any,
    words: Sequence[Dict[str, Any]],
    box: Tuple[int, int, int, int],
    psm: str,
    reocr: bool = True,
) -> str:
    """Use the word boxes first; re-OCR the crop when a key cell came back empty."""
    text = _join(words)
    if text or not reocr:
        return text
    try:
        crop = grn_lines.crop(image, *box)
        if getattr(crop, "size", 0) == 0:
            return ""
        return grn_lines.ocr_text(crop, psm=psm)
    except grn_lines.GrnReaderUnavailable:
        return ""


def _first_number(text: str) -> Optional[float]:
    for token in text.split():
        value = grn_lines.parse_number(token)
        if value is not None:
            return value
    return None


def _trim_code(token: str) -> str:
    return re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", token)


def _line_code(text: str) -> Optional[str]:
    tokens = [_trim_code(token) for token in text.split()]
    for cleaned in tokens:
        if cleaned and "-" in cleaned and re.search(r"\d", cleaned):
            return cleaned
    for cleaned in tokens:
        if cleaned and _CODE_RE.match(cleaned) and re.search(r"\d", cleaned):
            return cleaned
    return None


def _unit_from_text(text: str) -> Optional[str]:
    for token in re.split(r"\s+", text or ""):
        cleaned = token.strip(".,;:|()[]").lower()
        if cleaned in _UNITS:
            return cleaned
    return None


def _header_band(
    words: Sequence[Dict[str, Any]], bands: Sequence[Tuple[int, int]], lefts: Sequence[int], rights: Sequence[int]
) -> Optional[int]:
    for index, (top, bottom) in enumerate(bands):
        fields = set()
        for x0, x1 in zip(lefts, rights):
            text = _join(_cells_in_band(words, top, bottom, x0, x1))
            name = _header_field(text)
            if name:
                fields.add(name)
        if "received_qty" in fields and "accepted_qty" in fields:
            return index
    return None


def _map_header_columns(
    words: Sequence[Dict[str, Any]], top: int, bottom: int, lefts: Sequence[int], rights: Sequence[int]
) -> Tuple[Dict[str, int], List[str]]:
    columns: Dict[str, int] = {}
    header: List[str] = []
    for x0, x1 in zip(lefts, rights):
        text = _join(_cells_in_band(words, top, bottom, x0, x1))
        header.append(text)
        name = _header_field(text)
        if name and name not in columns:
            columns[name] = (x0 + x1) // 2
    return columns, header


def _read_cell_rows(
    image: Any,
    words: Sequence[Dict[str, Any]],
    bands: Sequence[Tuple[int, int]],
    lefts: Sequence[int],
    rights: Sequence[int],
    header_fields: Sequence[Optional[str]],
) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for index, (top, bottom) in enumerate(bands, start=1):
        record: Dict[str, Any] = {}
        accepted_box: Optional[Tuple[int, int, int, int]] = None
        for x0, x1, name in zip(lefts, rights, header_fields):
            if name is None:
                continue
            if name == "accepted_qty":
                accepted_box = (x0, top, x1, bottom)
            cell_words = _cells_in_band(words, top, bottom, x0, x1)
            psm = "7" if name in ("item_code", "line_no", "uom") else "6"
            text = _read_cell_text(
                image,
                cell_words,
                (x0, top, x1, bottom),
                psm,
                reocr=name in ("item_code", "description") or name in QUANTITY_FIELDS,
            )
            if not text:
                continue
            if name in QUANTITY_FIELDS:
                value = _first_number(text)
                if value is not None:
                    record.setdefault(name, value)
            elif name == "description":
                record["description"] = text
            elif name == "item_code":
                tokens = text.split()
                record["item_code"] = _line_code(text) or (tokens[0] if tokens else None)
            elif name == "uom":
                record["uom"] = text.strip(" ,.;:")
            elif name == "remarks":
                record["remarks"] = text
            elif name == "line_no":
                value = _first_number(text)
                record["line_no"] = int(value) if value is not None else None

        if not record:
            continue

        # GRN-B prints the unit inside the quantity cells ("4 Drum"), not in a
        # separate column.
        record.setdefault("line_no", index)
        if not record.get("uom"):
            for name in ("challan_qty", "received_qty", "accepted_qty"):
                cell_words = []
                for x0, x1, mapped in zip(lefts, rights, header_fields):
                    if mapped == name:
                        cell_words = _cells_in_band(words, top, bottom, x0, x1)
                        break
                unit = _unit_from_text(_join(cell_words))
                if unit:
                    record["uom"] = unit
                    break

        # A single misread digit ("25" read as "26") breaks accepted+rejected ==
        # received. Re-read that one cell as digits and keep the value only if it
        # makes the row consistent.
        received = record.get("received_qty")
        accepted = record.get("accepted_qty")
        if received is not None and accepted is not None and accepted_box is not None:
            rejected = record.get("rejected_qty") or 0.0
            if abs(accepted + rejected - received) > 1e-6:
                try:
                    reread = grn_lines.parse_number(
                        grn_lines.ocr_digits(grn_lines.crop(image, *accepted_box))
                    )
                except grn_lines.GrnReaderUnavailable:
                    reread = None
                if reread is not None and abs(reread + rejected - received) <= 1e-6:
                    record["accepted_qty"] = reread

        if any(record.get(key) is not None for key in QUANTITY_FIELDS) or record.get("description"):
            lines.append(record)
    return lines


def _read_with_rules(
    image: Any,
    words: Sequence[Dict[str, Any]],
    row_lines: Sequence[int],
    column_lines: Sequence[int],
    angle: float,
) -> GrnTable:
    bands = [(top, bottom) for top, bottom in zip(row_lines, row_lines[1:]) if bottom - top > 18]
    lefts, rights = list(column_lines[:-1]), list(column_lines[1:])
    header_index = _header_band(words, bands, lefts, rights)
    if header_index is None:
        return GrnTable(angle=angle, reason="no header row")

    header_top, header_bottom = bands[header_index]
    columns, header = _map_header_columns(words, header_top, header_bottom, lefts, rights)
    if "received_qty" not in columns or "accepted_qty" not in columns:
        return GrnTable(angle=angle, header=header, reason="header cells did not map to columns")

    header_fields = [_header_field(text) for text in header]
    lines = _read_cell_rows(image, words, bands[header_index + 1 :], lefts, rights, header_fields)
    method = "cells" if lines else "none"
    return GrnTable(
        lines=lines,
        columns=columns,
        header=header,
        angle=angle,
        method=method,
        reason="" if lines else "no data rows",
    )


def _header_clusters(words: Sequence[Dict[str, Any]], top: int, bottom: int) -> List[Tuple[str, int]]:
    """Group header words into column labels, so "Qty as per DC" stays one label."""
    band = sorted([w for w in words if top <= w["cy"] < bottom], key=lambda w: w["x"])
    clusters: List[List[Dict[str, Any]]] = []
    for word in band:
        if clusters:
            previous = clusters[-1][-1]
            if word["x"] - (previous["x"] + previous["w"]) < 40:
                clusters[-1].append(word)
                continue
        clusters.append([word])
    out: List[Tuple[str, int]] = []
    for cluster in clusters:
        text = _join(cluster)
        centre = int(sum(w["cx"] for w in cluster) / len(cluster))
        out.append((text, centre))
    return out


def _read_by_header_columns(
    words: Sequence[Dict[str, Any]], row_lines: Sequence[int], angle: float
) -> GrnTable:
    """Fallback for tables whose vertical rules were not detected."""
    bands = [(top, bottom) for top, bottom in zip(row_lines, row_lines[1:]) if bottom - top > 18]
    header_index = None
    header_labels: List[Tuple[str, int]] = []
    for index, (top, bottom) in enumerate(bands):
        labels = _header_clusters(words, top, bottom)
        fields = {_header_field(text) for text, _ in labels}
        if {"received_qty", "accepted_qty"} <= fields:
            header_index, header_labels = index, labels
            break
    if header_index is None:
        return GrnTable(angle=angle, reason="no header row")

    columns: Dict[str, int] = {}
    for text, centre in header_labels:
        name = _header_field(text)
        if name and name not in columns:
            columns[name] = centre
    if "received_qty" not in columns or "accepted_qty" not in columns:
        return GrnTable(angle=angle, reason="header cells did not map to columns")

    qty_columns = {name: centre for name, centre in columns.items() if name in QUANTITY_FIELDS}
    qty_left = min(qty_columns.values())

    data_words = [w for w in words if w["cy"] >= bands[header_index][1]]
    rows: List[List[Dict[str, Any]]] = []
    for word in sorted(data_words, key=lambda w: w["cy"]):
        if rows and abs(rows[-1][0]["cy"] - word["cy"]) <= 14:
            rows[-1].append(word)
        else:
            rows.append([word])

    lines: List[Dict[str, Any]] = []
    for row in rows:
        tokens = _reading_order(row)
        record: Dict[str, Any] = {}
        numbers = [
            word for word in tokens if word["cx"] > qty_left - 90 and grn_lines.parse_number(word["text"]) is not None
        ]
        if not numbers:
            continue
        for word in numbers:
            target = min(qty_columns, key=lambda name: abs(qty_columns[name] - word["cx"]))
            if abs(qty_columns[target] - word["cx"]) < 90:
                record.setdefault(target, grn_lines.parse_number(word["text"]))
        left_text = [w for w in tokens if w["cx"] < qty_left - 60]
        code_words = [w for w in left_text if _line_code(w["text"])]
        if code_words:
            record["item_code"] = _line_code(code_words[0]["text"])
        description = [w for w in left_text if w not in code_words and w["text"].lower().strip(".,") not in _UNITS]
        if description:
            record["description"] = _join(description)
        unit = _unit_from_text(_join(left_text)) or _unit_from_text(_join(numbers))
        if unit:
            record["uom"] = unit
        record["line_no"] = len(lines) + 1
        lines.append(record)

    return GrnTable(
        lines=lines,
        columns=columns,
        header=[text for text, _ in header_labels],
        angle=angle,
        method="header-columns" if lines else "none",
        reason="" if lines else "no data rows",
    )


def read_grn_table(content: bytes) -> GrnTable:
    """Read the GRN table from image bytes; never raises for a bad document."""
    if not grn_lines.available():
        return GrnTable(reason="image reader unavailable")
    try:
        gray = grn_lines.load_page_image(content)
        gray, angle = grn_lines.deskew(gray)
        row_lines, column_lines, horizontal, vertical = grn_lines.detect_rules(gray)
        binary, _, _ = grn_lines.rule_masks(gray)
        clean = grn_lines.strip_rules(binary, horizontal, vertical)
        words = grn_lines.ocr_words(clean)
    except grn_lines.GrnReaderUnavailable as exc:
        return GrnTable(reason=str(exc))
    except Exception as exc:  # malformed image data must not break ingest
        return GrnTable(reason=f"{type(exc).__name__}: {exc}")

    if not words:
        return GrnTable(angle=angle, reason="no text found")
    if len(row_lines) < 2:
        return GrnTable(angle=angle, reason="no table rules found")

    if len(column_lines) >= 4:
        table = _read_with_rules(clean, words, row_lines, column_lines, angle)
        if table.ok:
            return table

    return _read_by_header_columns(words, row_lines, angle)


# ---------------------------------------------------------------------------
# Deterministic checks and PO mapping
# ---------------------------------------------------------------------------
def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _po_line_no(obj: Any) -> Any:
    value = _attr(obj, "line_no")
    return value if value is not None else _attr(obj, "po_line_no")


_CODE_LOOKALIKES = str.maketrans({"I": "1", "O": "0", "S": "5", "B": "8"})


def _code_key(value: Any) -> str:
    # OCR confuses I/1, O/0, S/5 and B/8 in codes (NUT-Mi0 for NUT-M10).
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper().translate(_CODE_LOOKALIKES)


def _code_similarity(left: Any, right: Any) -> float:
    a, b = _code_key(left), _code_key(right)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


_DESCRIPTION_STOPWORDS = {"the", "and", "of", "per", "nos", "pcs"}


def _description_tokens(text: str) -> set:
    return {
        token
        for token in _normalise(text).split()
        if len(token) > 1 and token not in _DESCRIPTION_STOPWORDS
    }


def _description_similarity(left: Any, right: Any) -> float:
    a, b = _normalise(str(left or "")), _normalise(str(right or ""))
    if not a or not b:
        return 0.0
    score = difflib.SequenceMatcher(None, a, b).ratio()
    left_tokens, right_tokens = _description_tokens(a), _description_tokens(b)
    if left_tokens and right_tokens:
        # "Hex Nut M10" is contained in "Hex Nut M10, Zinc Plated"; the longer
        # PO description must not stop the line from mapping.
        score = max(score, len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)))
    return score


def map_lines_to_po(
    lines: Sequence[Dict[str, Any]],
    po_lines: Sequence[Any],
    code_threshold: float = 0.85,
    description_threshold: float = 0.72,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Attach each GRN line to a PO line and return the problems found.

    Item codes are compared after stripping punctuation because OCR turns
    ``ALN-SET9`` into ``ALN-SETS``. A near-tie between two PO lines is reported
    as ambiguous rather than guessed.
    """
    problems: List[str] = []
    mapped: List[Dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        code = line.get("item_code")
        candidates: List[Tuple[float, Any]] = []
        if code:
            candidates = [(_code_similarity(code, _attr(po, "item_code")), po) for po in po_lines]
        best_score = max((score for score, _ in candidates), default=0.0)
        if best_score >= code_threshold:
            contenders = [po for score, po in candidates if score >= code_threshold]
            best = max(candidates, key=lambda item: item[0])[1]
            if len(contenders) > 1:
                second = max(
                    _code_similarity(code, _attr(po, "item_code"))
                    for po in contenders
                    if po is not best
                )
                if best_score - second < 0.05:
                    problems.append(
                        f"line {index}: item code {code} matches more than one PO line"
                    )
                    mapped.append({**line, "po_line_no": None})
                    continue
        else:
            best = None
            best_score = 0.0
            for po in po_lines:
                score = _description_similarity(line.get("description"), _attr(po, "description"))
                if score > best_score:
                    best, best_score = po, score
            if best_score < description_threshold:
                best = None

        if best is None:
            problems.append(f"line {index}: could not map to a PO line")
            mapped.append({**line, "po_line_no": None})
            continue

        mapped.append(
            {
                **line,
                "po_line_no": _po_line_no(best),
                "item_code": _attr(best, "item_code") or line.get("item_code"),
                "description": line.get("description") or _attr(best, "description"),
            }
        )
    return mapped, problems


def sanity_problems(
    lines: Sequence[Dict[str, Any]], po_lines: Optional[Sequence[Any]] = None
) -> List[str]:
    """
    Arithmetic and quantity checks that do not need an AI model.

    ``po_lines`` supplies ``ordered_qty``/``remaining_qty`` for the cumulative
    check; when omitted only the per-line checks run.
    """
    problems: List[str] = []
    received_by_po_line: Dict[Any, float] = {}
    for index, line in enumerate(lines, start=1):
        received = line.get("received_qty")
        accepted = line.get("accepted_qty")
        rejected = line.get("rejected_qty") or 0.0
        if received is None:
            problems.append(f"line {index}: received quantity missing")
        elif received < 0:
            problems.append(f"line {index}: received quantity is negative")
        if received is not None and accepted is not None:
            if abs(accepted + rejected - received) > 1e-6:
                problems.append(
                    f"line {index}: accepted {accepted:g} + rejected {rejected:g} != received {received:g}"
                )
        po_line_no = line.get("po_line_no")
        if po_line_no is not None and received is not None:
            received_by_po_line[po_line_no] = received_by_po_line.get(po_line_no, 0.0) + received

    if po_lines:
        remaining = {
            _po_line_no(po): _attr(po, "remaining_qty") for po in po_lines
        }
        for po_line_no, received in received_by_po_line.items():
            available = remaining.get(po_line_no)
            if available is None:
                continue
            try:
                available = float(available)
            except (TypeError, ValueError):
                continue
            if received > available + 1e-6:
                problems.append(
                    f"PO line {po_line_no}: received {received:g} exceeds the {available:g} still open"
                )
    return problems


def payload_lines(lines: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert reader output to the canonical GRN line payload."""
    payload: List[Dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        payload.append(
            {
                "line_no": line.get("line_no") or index,
                "po_line_no": line.get("po_line_no"),
                "item_code": line.get("item_code"),
                "description": line.get("description"),
                "unit": line.get("uom"),
                "received_quantity": line.get("received_qty"),
            }
        )
    return payload
