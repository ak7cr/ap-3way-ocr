"""
Object storage for document previews.

Previously previews were stored as base64 data URLs inside PostgreSQL rows, which
bloats the database and every API response. Now the bytes live in object storage
(local disk by default, S3/MinIO when configured) and the DB only keeps the key.
"""
import base64
import binascii
import hashlib
import os
import re
from datetime import datetime
from typing import Optional

from ..config import STORAGE

_DATA_URL_RE = re.compile(r"^data:(?P<mime>[-\w.+/]+);base64,(?P<payload>.*)$", re.DOTALL)

_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "application/pdf": "pdf",
}


def _ext_for_mime(mime: str, default: str = "jpg") -> str:
    return _MIME_TO_EXT.get((mime or "").lower(), default)


def _object_key(ext: str, prefix: str) -> str:
    now = datetime.utcnow()
    return f"{prefix}/{now:%Y/%m/%d}/{now:%H%M%S}-{os.urandom(6).hex()}.{ext}"


def _local_root() -> str:
    root = STORAGE["root"]
    os.makedirs(root, exist_ok=True)
    return root


def _s3_client():
    """Lazily creates an S3/MinIO client. Requires boto3 when enabled."""
    import boto3  # imported lazily so local-only installs need no boto3

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("STORAGE_ENDPOINT_URL") or None,
        aws_access_key_id=os.getenv("STORAGE_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("STORAGE_SECRET_KEY"),
        region_name=os.getenv("STORAGE_REGION", "us-east-1"),
    )


def save_bytes(data: bytes, ext: str, prefix: str = "previews") -> str:
    """Persists raw bytes and returns the storage key (never a data URL)."""
    key = _object_key(ext, prefix)
    backend = (STORAGE.get("backend") or "local").lower()

    if backend in ("s3", "minio"):
        _s3_client().put_object(Bucket=STORAGE["bucket"], Key=key, Body=data)
        return key

    path = os.path.join(_local_root(), key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return key


def save_data_url(data_url: str, prefix: str = "previews") -> Optional[str]:
    """
    Stores a `data:<mime>;base64,...` URL in object storage and returns the key.
    Returns None when the payload is not a data URL (e.g. an already-stored key).
    """
    if not data_url:
        return None
    match = _DATA_URL_RE.match(data_url.strip())
    if not match:
        return None
    try:
        payload = base64.b64decode(match.group("payload"), validate=False)
    except (binascii.Error, ValueError):
        return None
    return save_bytes(payload, _ext_for_mime(match.group("mime")), prefix=prefix)


def store_preview(preview: str, prefix: str = "previews") -> Optional[str]:
    """Accepts a data URL or an existing storage key and returns a storage key."""
    if not preview:
        return None
    key = save_data_url(preview, prefix=prefix)
    return key if key else preview


def public_url(key: Optional[str]) -> Optional[str]:
    """Maps a storage key to a URL the browser can fetch."""
    if not key:
        return None
    if key.startswith("http://") or key.startswith("https://") or key.startswith("/"):
        return key
    prefix = STORAGE.get("public_prefix", "/api/files").rstrip("/")
    return f"{prefix}/{key.lstrip('/')}"


def content_type_for(key: str) -> str:
    ext = (key.rsplit(".", 1)[-1] if "." in key else "jpg").lower()
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "tiff": "image/tiff",
        "pdf": "application/pdf",
    }.get(ext, "application/octet-stream")


def read_bytes(key: str) -> Optional[bytes]:
    """Reads a stored object back (used by the file-serving endpoint)."""
    if not key:
        return None
    backend = (STORAGE.get("backend") or "local").lower()
    if backend in ("s3", "minio"):
        try:
            return _s3_client().get_object(Bucket=STORAGE["bucket"], Key=key)["Body"].read()
        except Exception as exc:  # pragma: no cover - network path
            print(f"[Storage] S3 read failed for {key}: {exc}")
            return None

    safe_key = _safe_local_key(key)
    if not safe_key:
        return None
    path = os.path.join(_local_root(), safe_key)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return fh.read()


def _safe_local_key(key: str) -> Optional[str]:
    """Rejects path traversal attempts outside the storage root."""
    normalized = os.path.normpath(key.lstrip("/"))
    if normalized.startswith("..") or os.path.isabs(normalized):
        return None
    return normalized


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
