"""
Low-level helpers for reading ruled tables out of scanned GRN/PO images.

The table rules are the useful structure on these documents: they tell us where
each column and row begins. This module only does image geometry and OCR word
boxes; the column semantics live in ``grn_table_reader``.

OpenCV and Tesseract are optional. When either is missing the helpers raise
``GrnReaderUnavailable`` so callers can fall back to their normal extraction path.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - import guard is exercised through available()
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


class GrnReaderUnavailable(RuntimeError):
    """The image table reader cannot run in this environment."""


def _tesseract_executable() -> str:
    executable = os.getenv("TESSERACT_CMD", "tesseract")
    if not shutil.which(executable):
        raise GrnReaderUnavailable("tesseract executable not found")
    return executable


def available() -> bool:
    """True when both OpenCV and the Tesseract executable are usable."""
    if cv2 is None or np is None:
        return False
    try:
        _tesseract_executable()
    except GrnReaderUnavailable:
        return False
    return True


def _require() -> None:
    if cv2 is None or np is None:
        raise GrnReaderUnavailable("opencv-python and numpy are required to read table images")
    _tesseract_executable()


def decode_image(content: bytes) -> "np.ndarray":
    """Decode uploaded bytes into a greyscale image."""
    _require()
    buffer = np.frombuffer(content, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise GrnReaderUnavailable("could not decode image")
    return image


def render_first_page(content: bytes, dpi: int = 200) -> bytes:
    """Rasterise the first PDF page to PNG so the same reader can read it."""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise GrnReaderUnavailable("pymupdf is required to rasterise PDFs") from exc
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise GrnReaderUnavailable(f"could not open PDF: {exc}") from exc
    try:
        if len(document) == 0:
            raise GrnReaderUnavailable("PDF has no pages")
        return document[0].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        document.close()


def load_page_image(content: bytes) -> "np.ndarray":
    """Greyscale page image from image bytes or the first page of a PDF."""
    if b"%PDF" in content[:1024]:
        content = render_first_page(content)
    return decode_image(content)


def rule_masks(gray: "np.ndarray") -> Tuple["np.ndarray", "np.ndarray", "np.ndarray"]:
    """Binary image plus horizontal and vertical rule masks."""
    _require()
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 15
    )
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (70, 1))
    )
    vertical = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    )
    return binary, horizontal, vertical


def deskew(gray: "np.ndarray") -> Tuple["np.ndarray", float]:
    """Rotate the page so the table's horizontal rules are level."""
    _require()
    _, horizontal, _ = rule_masks(gray)
    segments = cv2.HoughLinesP(
        horizontal,
        1,
        math.pi / 720,
        threshold=200,
        minLineLength=gray.shape[1] // 4,
        maxLineGap=10,
    )
    if segments is None:
        return gray, 0.0

    angles: List[float] = []
    weights: List[float] = []
    for x1, y1, x2, y2 in np.asarray(segments).reshape(-1, 4):
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if abs(angle) < 6:
            angles.append(angle)
            weights.append(math.hypot(x2 - x1, y2 - y1))
    if not angles:
        return gray, 0.0

    # Weighted median angle: a stray short rule cannot tilt the whole page.
    order = np.argsort(angles)
    cumulative = np.cumsum(np.array(weights)[order])
    angle = float(np.array(angles)[order][np.searchsorted(cumulative, cumulative[-1] / 2)])
    matrix = cv2.getRotationMatrix2D(
        (gray.shape[1] / 2, gray.shape[0] / 2), angle, 1.0
    )
    rotated = cv2.warpAffine(
        gray,
        matrix,
        (gray.shape[1], gray.shape[0]),
        flags=cv2.INTER_CUBIC,
        borderValue=255,
    )
    return rotated, angle


def group_positions(indices: Sequence[int], tolerance: int) -> List[int]:
    """Collapse runs of adjacent positions into one representative coordinate."""
    groups: List[List[int]] = []
    for index in indices:
        if groups and index - groups[-1][-1] <= tolerance:
            groups[-1].append(index)
        else:
            groups.append([index])
    return [int(round(sum(group) / len(group))) for group in groups]


def detect_rules(gray: "np.ndarray") -> Tuple[List[int], List[int], "np.ndarray", "np.ndarray"]:
    """
    Return the y positions of horizontal rules, the x positions of vertical
    rules, and the two rule masks (so callers can strip them before OCR).
    """
    _require()
    binary, horizontal, vertical = rule_masks(gray)
    height, width = gray.shape

    horizontal_projection = (horizontal > 0).sum(axis=1)
    row_lines = group_positions(
        [y for y, count in enumerate(horizontal_projection) if count > width * 0.35], 6
    )

    vertical_projection = (vertical > 0).sum(axis=0)
    peak = int(vertical_projection.max()) if vertical_projection.size else 0
    column_lines = group_positions(
        [
            x
            for x, count in enumerate(vertical_projection)
            if count > max(peak * 0.45, height * 0.04)
        ],
        15,
    )
    return row_lines, column_lines, horizontal, vertical


def strip_rules(
    binary: "np.ndarray", horizontal: "np.ndarray", vertical: "np.ndarray"
) -> "np.ndarray":
    """Black-on-white image with the table rules erased, so OCR sees text only."""
    _require()
    rules = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8))
    text = cv2.bitwise_and(binary, cv2.bitwise_not(rules))
    return cv2.bitwise_not(text)


def _run_tesseract(image: "np.ndarray", extra_args: Sequence[str]) -> str:
    _require()
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise GrnReaderUnavailable("could not encode cell for OCR")
    completed = subprocess.run(
        [_tesseract_executable(), "stdin", "stdout", *extra_args],
        input=encoded.tobytes(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise GrnReaderUnavailable(
            completed.stderr.decode("utf-8", errors="replace")[:200] or "tesseract failed"
        )
    return completed.stdout.decode("utf-8", errors="replace")


def ocr_words(image: "np.ndarray", psm: str = "6") -> List[Dict[str, Any]]:
    """OCR word boxes as ``{text, x, y, w, h, cx, cy, confidence}``."""
    output = _run_tesseract(image, ["--psm", psm, "tsv"])
    words: List[Dict[str, Any]] = []
    for line in output.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) != 12:
            continue
        text = fields[11].strip()
        try:
            confidence = float(fields[10])
        except ValueError:
            continue
        if not text or confidence <= 0:
            continue
        left, top, width, height = (int(fields[6]), int(fields[7]), int(fields[8]), int(fields[9]))
        words.append(
            {
                "text": text,
                "x": left,
                "y": top,
                "w": width,
                "h": height,
                "cx": left + width / 2.0,
                "cy": top + height / 2.0,
                "confidence": confidence,
            }
        )
    return words


def ocr_text(image: "np.ndarray", psm: str = "7") -> str:
    """Single-value OCR used to re-read one table cell."""
    return _run_tesseract(image, ["--psm", psm]).strip()


def ocr_digits(image: "np.ndarray") -> str:
    """Re-read one cell as digits only, to repair a single misread character."""
    _require()
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise GrnReaderUnavailable("could not encode cell for OCR")
    completed = subprocess.run(
        [
            _tesseract_executable(),
            "stdin",
            "stdout",
            "--psm",
            "7",
            "-c",
            "tessedit_char_whitelist=0123456789,.",
        ],
        input=encoded.tobytes(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", errors="replace").strip()


def crop(image: "np.ndarray", x0: int, y0: int, x1: int, y1: int, inset: int = 3) -> "np.ndarray":
    """Crop with a small inset so anti-aliased rule pixels do not leak in."""
    _require()
    height, width = image.shape
    x0 = max(0, min(width, x0 + inset))
    x1 = max(0, min(width, x1 - inset))
    y0 = max(0, min(height, y0 + inset))
    y1 = max(0, min(height, y1 - inset))
    if x1 <= x0 or y1 <= y0:
        return image[0:0, 0:0]
    return image[y0:y1, x0:x1]


_NUMBER_AT_START = re.compile(r"^(\d[\d,]*(?:\.\d+)?)")


def parse_number(token: str) -> Optional[float]:
    """Read ``2,000``, ``4`` or ``4Drum`` as a number; ignore other text."""
    match = _NUMBER_AT_START.match(token.strip().strip(".,;:|()[]"))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None
