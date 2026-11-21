"""Tests for the scanned-GRN table reader and its deterministic checks.

The OCR tests read the three sample scans in ``tests/ap_samples`` and score the
quantities against ``truth/``. They are skipped when OpenCV or Tesseract is not
installed, because the reader is an optional capability.
"""
import json
from pathlib import Path

import pytest

from backend.services import grn_lines
from backend.services.grn_table_reader import (
    _header_field,
    map_lines_to_po,
    payload_lines,
    read_grn_table,
    sanity_problems,
)

SAMPLES = Path(__file__).resolve().parent / "ap_samples"

QUANTITY_KEYS = ("po_qty", "challan_qty", "received_qty", "accepted_qty")

requires_ocr = pytest.mark.skipif(
    not grn_lines.available(), reason="OpenCV and Tesseract are required"
)


def _truth(name: str):
    return json.loads((SAMPLES / "truth" / f"{name}.json").read_text())


def _read(scenario: str):
    content = (SAMPLES / "scanned" / f"grn_{scenario}_scan.jpg").read_bytes()
    return read_grn_table(content)


def _scored_fields(scenario: str):
    """GRN-B has no PO-quantity column, so only score what is printed."""
    keys = QUANTITY_KEYS
    if scenario == "S3":
        keys = tuple(key for key in QUANTITY_KEYS if key != "po_qty")
    return keys


@requires_ocr
@pytest.mark.parametrize("scenario", ["S1", "S2", "S3"])
def test_reads_scans_against_truth(scenario):
    truth = _truth(f"grn_{scenario}")
    table = _read(scenario)

    assert table.ok, table.reason
    assert table.method == "cells"
    assert len(table.lines) == len(truth["lines"])

    keys = _scored_fields(scenario)
    expected = [{key: line[key] for key in keys} for line in truth["lines"]]
    actual = [{key: line.get(key) for key in keys} for line in table.lines]

    matched = sum(
        1
        for expected_line, actual_line in zip(expected, actual)
        for key in keys
        if actual_line[key] == expected_line[key]
    )
    total = len(expected) * len(keys)
    assert matched / total >= 0.9, f"{scenario}: {matched}/{total}\n{actual}"


@requires_ocr
def test_all_scans_score_against_truth():
    matched = total = 0
    for scenario in ("S1", "S2", "S3"):
        truth = _truth(f"grn_{scenario}")
        table = _read(scenario)
        keys = _scored_fields(scenario)
        for expected, actual in zip(truth["lines"], table.lines):
            for key in keys:
                total += 1
                if actual.get(key) == expected[key]:
                    matched += 1

    assert matched / total >= 0.95, f"{matched}/{total}"


@requires_ocr
def test_material_receipt_report_quantities():
    """GRN-B's header cells do not line up with the numbers; the cells do."""
    table = _read("S3")

    assert [(line["challan_qty"], line["received_qty"], line["accepted_qty"]) for line in table.lines] == [
        (4.0, 4.0, 4.0),
        (6.0, 6.0, 6.0),
        (10.0, 8.0, 8.0),
    ]


@requires_ocr
def test_grn_a_reads_item_codes_and_columns():
    table = _read("S1")
    truth = _truth("grn_S1")

    assert table.columns["received_qty"] < table.columns["accepted_qty"] < table.columns["rejected_qty"]
    assert len(table.lines) == len(truth["lines"])
    assert [line["accepted_qty"] for line in table.lines][:3] == [2000.0, 2000.0, 2000.0]
    assert table.lines[3]["item_code"] == "ALN-SET9"


@requires_ocr
def test_undecodable_image_returns_a_reason():
    table = read_grn_table(b"not an image")

    assert not table.ok
    assert table.reason


@requires_ocr
def test_falls_back_to_header_columns_without_vertical_rules(monkeypatch):
    real_detect = grn_lines.detect_rules

    def without_verticals(gray):
        rows, _columns, horizontal, vertical = real_detect(gray)
        return rows, [], horizontal, vertical

    monkeypatch.setattr(grn_lines, "detect_rules", without_verticals)

    table = _read("S1")

    assert table.method == "header-columns"
    assert len(table.lines) == 4
    assert [line["received_qty"] for line in table.lines] == [2000.0, 2000.0, 2000.0, 25.0]


def test_header_fields_survive_ocr_typos():
    assert _header_field("PO aty") == "po_qty"
    assert _header_field("Chailan aty") == "challan_qty"
    assert _header_field("ttem Code") == "item_code"
    assert _header_field("Qty as per DC") == "challan_qty"
    assert _header_field("Received Qty") == "received_qty"
    assert _header_field("Qty Accepted") == "accepted_qty"
    assert _header_field("Condition / Remarks") == "remarks"
    assert _header_field("Batch") is None


def test_sanity_checks_accepted_plus_rejected():
    lines = [
        {"received_qty": 25, "accepted_qty": 26, "rejected_qty": 0},
        {"received_qty": 10, "accepted_qty": 9, "rejected_qty": 1},
    ]

    assert sanity_problems(lines) == [
        "line 1: accepted 26 + rejected 0 != received 25"
    ]


def test_sanity_checks_missing_received_quantity():
    problems = sanity_problems([{"accepted_qty": 5}])

    assert problems == ["line 1: received quantity missing"]


def test_sanity_checks_cumulative_receipt_against_open_po_quantity():
    lines = [{"po_line_no": 3, "received_qty": 8, "accepted_qty": 8, "rejected_qty": 0}]
    po_lines = [{"po_line_no": 3, "remaining_qty": "5"}]

    problems = sanity_problems(lines, po_lines)

    assert problems == ["PO line 3: received 8 exceeds the 5 still open"]


def test_maps_ocr_mangled_item_code_fuzzily():
    po_lines = [
        {"line_no": 4, "item_code": "ALN-SET9", "description": "Allen Key Set"},
        {"line_no": 5, "item_code": "BLT-M10-60", "description": "Hex Bolt"},
    ]

    mapped, problems = map_lines_to_po([{"item_code": "ALN-SETS"}], po_lines)

    assert problems == []
    assert mapped[0]["po_line_no"] == 4


def test_ambiguous_item_code_is_left_for_review():
    po_lines = [
        {"line_no": 1, "item_code": "ALN-SET9"},
        {"line_no": 2, "item_code": "ALN-SET8"},
    ]

    mapped, problems = map_lines_to_po([{"item_code": "ALN-SET7"}], po_lines)

    assert mapped[0]["po_line_no"] is None
    assert problems == ["line 1: item code ALN-SET7 matches more than one PO line"]


def test_falls_back_to_description_when_the_document_has_no_codes():
    po_lines = [
        {"line_no": 1, "item_code": "LUB-SRV68", "description": "Servo Hydraulic Oil 68, 210 L Drum"},
        {"line_no": 2, "item_code": "GRS-EP2", "description": "Lithium Grease EP2, 18 kg Pail"},
    ]

    mapped, problems = map_lines_to_po(
        [{"description": "Servo Hydraulic Oil 68, 210 L Drum"}], po_lines
    )

    assert problems == []
    assert mapped[0]["po_line_no"] == 1
    assert mapped[0]["item_code"] == "LUB-SRV68"


def test_unmapped_line_is_reported():
    mapped, problems = map_lines_to_po(
        [{"item_code": "ZZZ-999", "description": "Widget coupling"}],
        [{"line_no": 1, "item_code": "ABC-100", "description": "Known part"}],
    )

    assert mapped[0]["po_line_no"] is None
    assert problems == ["line 1: could not map to a PO line"]


def test_payload_lines_carries_the_mapped_po_line_number():
    payload = payload_lines(
        [
            {
                "po_line_no": 3,
                "line_no": 1,
                "item_code": "CW-50",
                "description": "Cotton Waste",
                "uom": "Bale",
                "received_qty": 8,
            }
        ]
    )

    assert payload == [
        {
            "line_no": 1,
            "po_line_no": 3,
            "item_code": "CW-50",
            "description": "Cotton Waste",
            "unit": "Bale",
            "received_quantity": 8,
        }
    ]


def test_lookalike_code_maps_to_the_po_line():
    """OCR reads NUT-M10 as NUT-Mi0; the code comparison must tolerate it."""
    po_lines = [{"line_no": 2, "item_code": "NUT-M10", "description": "Hex Nut M10"}]

    mapped, problems = map_lines_to_po(
        [{"item_code": "NUT-Mi0", "description": "Hex Nut M10, Zinc Plated"}], po_lines
    )

    assert problems == []
    assert mapped[0]["po_line_no"] == 2


def test_truncated_po_description_still_maps():
    po_lines = [{"line_no": 1, "item_code": None, "description": "Hex Bolt M10 x 60 mm"}]

    mapped, problems = map_lines_to_po(
        [{"description": "Hex Bolt M10 x 60 mm, HT 8.8, Zinc Plated"}], po_lines
    )

    assert problems == []
    assert mapped[0]["po_line_no"] == 1


@requires_ocr
@pytest.mark.parametrize("scenario", ["S1", "S2", "S3"])
def test_reads_digital_pdf_grns(scenario):
    """A digital PDF is rasterised and read by the same table reader."""
    truth = _truth(f"grn_{scenario}")
    content = (SAMPLES / "pdf" / f"grn_{scenario}.pdf").read_bytes()

    table = read_grn_table(content)

    assert table.ok, table.reason
    assert table.method == "cells"
    assert len(table.lines) == len(truth["lines"])
    keys = tuple(key for key in _scored_fields(scenario) if not (scenario == "S3" and key == "po_qty"))
    for expected, actual in zip(truth["lines"], table.lines):
        for key in keys:
            assert actual.get(key) == expected[key], (scenario, key, actual)
