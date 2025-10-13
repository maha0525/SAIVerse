import base64
import logging
import mimetypes
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

try:
    from PIL import Image  # type: ignore
except ImportError:  # pragma: no cover
    Image = None

LOGGER = logging.getLogger(__name__)
IMAGE_URI_PREFIX = "saiverse://image/"
SUPPORTED_LLM_IMAGE_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp"}


def _ensure_image_dir() -> Path:
    dest_dir = Path.home() / ".saiverse" / "image"
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir


def resolve_media_uri(uri: str) -> Optional[Path]:
    """Resolve a SAIVerse media URI to a local filesystem path."""
    if not isinstance(uri, str):
        return None
    if not uri.startswith(IMAGE_URI_PREFIX):
        return None
    filename = uri[len(IMAGE_URI_PREFIX):].strip()
    if not filename:
        return None
    return _ensure_image_dir() / filename


def iter_image_media(metadata: Any) -> List[Dict[str, Any]]:
    """Extract validated image descriptors from a metadata payload."""
    results: List[Dict[str, Any]] = []
    if not isinstance(metadata, dict):
        return results

    media = metadata.get("media")
    if not isinstance(media, list):
        media = metadata.get("images")
        if not isinstance(media, list):
            return results

    for item in media:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        if not uri:
            continue
        path = resolve_media_uri(uri)
        if path is None or not path.exists():
            LOGGER.warning("Image URI %s could not be resolved or file missing", uri)
            continue
        mime_type = item.get("mime_type") or mimetypes.guess_type(path)[0] or "image/png"
        results.append(
            {
                "uri": uri,
                "path": path,
                "mime_type": mime_type,
            }
        )
    return results


def store_image_bytes(data: bytes, mime_type: str, *, source: str = "generated") -> Tuple[Dict[str, str], Path]:
    dest_dir = _ensure_image_dir()
    mime_type = (mime_type or "image/png").lower()
    ext = mimetypes.guess_extension(mime_type) or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}{ext}"
    dest_path = dest_dir / filename
    dest_path.write_bytes(data)
    metadata = {
        "type": "image",
        "uri": f"{IMAGE_URI_PREFIX}{filename}",
        "mime_type": mime_type,
        "source": source,
    }
    return metadata, dest_path


@lru_cache(maxsize=256)
def _cached_path_to_data_url(path: str, mime_type: str, mtime: float) -> Optional[str]:
    """Internal helper to memoize base64 conversions keyed by path + mtime."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        LOGGER.exception("Failed to read image: %s", path)
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def path_to_data_url(path: Path, mime_type: str) -> Optional[str]:
    """Convert an image file to a data URL with caching."""
    try:
        stat = path.stat()
    except OSError:
        LOGGER.warning("Cannot read file metadata for %s", path)
        return None
    return _cached_path_to_data_url(str(path), mime_type, stat.st_mtime)


def load_image_bytes_for_llm(path: Path, mime_type: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Return (bytes, effective_mime) for LLM consumption.
    Converts unsupported formats to PNG when Pillow is available.
    """
    target_mime = mime_type.lower()
    if target_mime not in SUPPORTED_LLM_IMAGE_MIME and Image is not None:
        try:
            with Image.open(path) as img:
                buf = BytesIO()
                img.save(buf, format="PNG")
                LOGGER.debug("Converted image %s to PNG for LLM input", path)
                return buf.getvalue(), "image/png"
        except Exception:
            LOGGER.exception("Failed to convert image %s to PNG; falling back to raw bytes", path)
    try:
        data = path.read_bytes()
    except OSError:
        LOGGER.exception("Failed to read image for LLM: %s", path)
        return None, None
    if target_mime not in SUPPORTED_LLM_IMAGE_MIME:
        LOGGER.warning("Using raw bytes for potentially unsupported mime '%s'", mime_type)
    return data, target_mime
