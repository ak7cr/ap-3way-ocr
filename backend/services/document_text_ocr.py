"""Document-agnostic text used to identify scanned or digital documents."""
import os
import re
import shutil
import subprocess
from typing import Any, Dict, Optional, Tuple

_PDF_EXTENSIONS = {"pdf"}
_IMAGE_EXTENSIONS = {"bmp", "jpeg", "jpg", "png", "tif", "tiff", "webp"}


def _usable(text: str) -> bool:
    return len(re.findall(r"[A-Za-z0-9]{2,}", text or "")) >= 3


def _run_tesseract(content: bytes, engine: str = "tesseract") -> Tuple[str, Dict[str, Any]]:
    executable = os.getenv("TESSERACT_CMD", "tesseract")
    if not shutil.which(executable):
        return "", {"engine": engine, "ok": False, "reason": "not_installed"}

    try:
        completed = subprocess.run(
            [executable, "stdin", "stdout", "--psm", "6"],
            input=content,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", {"engine": engine, "ok": False, "reason": "timeout"}
    except OSError as exc:
        return "", {"engine": engine, "ok": False, "reason": "execution_error", "detail": str(exc)}

    text = completed.stdout.decode("utf-8", errors="replace").strip()
    usable = _usable(text)
    attempt: Dict[str, Any] = {
        "engine": engine,
        "ok": completed.returncode == 0 and usable,
        "return_code": completed.returncode,
        "text_characters": len(text),
    }
    if completed.returncode != 0:
        attempt["reason"] = "nonzero_exit"
        attempt["detail"] = completed.stderr.decode("utf-8", errors="replace")[:300]
    elif not usable:
        attempt["reason"] = "near_empty" if text else "no_text"
    return text, attempt


def _pdf_text(content: bytes) -> str:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - optional dependency
        return ""
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        return ""
    try:
        return "\n".join(page.get_text("text") for page in document)
    finally:
        document.close()


def _pdf_first_page_png(content: bytes, dpi: int = 200) -> Optional[bytes]:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - optional dependency
        return None
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        return None
    try:
        if len(document) == 0:
            return None
        return document[0].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        document.close()


def _looks_like_pdf(content: bytes, extension: str, content_type: Optional[str]) -> bool:
    return (
        extension in _PDF_EXTENSIONS
        or (content_type or "").lower().startswith("application/pdf")
        or b"%PDF" in content[:1024]
    )


def extract_document_text(
    content: bytes,
    filename: str,
    content_type: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Read enough text to classify the document.

    Images are OCR'd. PDFs use their text layer when present; a PDF with no
    usable text layer is rasterised and OCR'd instead of being treated as empty.
    """
    extension = (filename or "").rsplit(".", 1)[-1].lower()

    if _looks_like_pdf(content, extension, content_type):
        text = _pdf_text(content)
        if _usable(text):
            return text, {"engine": "pymupdf", "ok": True, "text_characters": len(text)}
        page = _pdf_first_page_png(content)
        if page is None:
            return text, {"engine": "pymupdf", "ok": False, "reason": "no_text_layer"}
        return _run_tesseract(page, engine="tesseract-pdf")

    if extension not in _IMAGE_EXTENSIONS and not (content_type or "").lower().startswith("image/"):
        return "", {"engine": "tesseract", "ok": False, "reason": "not_an_image"}

    return _run_tesseract(content)
