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
DOCUMENT_URI_PREFIX = "saiverse://document/"
ITEM_IMAGE_URI_PREFIX = "saiverse://item/"
PERSONA_URI_PREFIX = "saiverse://persona/"
BUILDING_URI_PREFIX = "saiverse://building/"
SUPPORTED_LLM_IMAGE_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
SUMMARY_SUFFIX = ".summary.txt"


def _ensure_image_dir() -> Path:
    from .data_paths import get_saiverse_home
    dest_dir = get_saiverse_home() / "image"
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir


def _ensure_document_dir() -> Path:
    from .data_paths import get_saiverse_home
    dest_dir = get_saiverse_home() / "documents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir


def resolve_media_uri(uri: str) -> Optional[Path]:
    """Resolve a SAIVerse media URI to a local filesystem path."""
    if not isinstance(uri, str):
        return None
    if uri.startswith(IMAGE_URI_PREFIX):
        filename = uri[len(IMAGE_URI_PREFIX):].strip()
        if not filename:
            return None
        return _ensure_image_dir() / filename
    elif uri.startswith(DOCUMENT_URI_PREFIX):
        filename = uri[len(DOCUMENT_URI_PREFIX):].strip()
        if not filename:
            return None
        return _ensure_document_dir() / filename
    return None


def resolve_extended_media_uri(
    uri: str,
    persona_id: Optional[str] = None,
    building_id: Optional[str] = None,
) -> Optional[Path]:
    """Resolve extended SAIVerse media URIs including context-dependent ones.

    Supported URI formats:
    - saiverse://image/<filename> - Generated image file
    - saiverse://item/<item_id>/image - Picture item's image
    - saiverse://persona/<persona_id>/image - Persona's avatar
    - saiverse://persona/self/image - Current persona's avatar (requires persona_id)
    - saiverse://building/<building_id>/image - Building's interior image
    - saiverse://building/current/image - Current building's interior (requires building_id)

    Args:
        uri: The SAIVerse URI to resolve
        persona_id: Current persona ID (for resolving 'self')
        building_id: Current building ID (for resolving 'current')

    Returns:
        Path to the image file, or None if not found/invalid
    """
    if not isinstance(uri, str):
        return None

    # Try basic resolution first
    basic_result = resolve_media_uri(uri)
    if basic_result is not None:
        return basic_result

    from .data_paths import get_saiverse_home
    saiverse_home = get_saiverse_home()

    # saiverse://item/<item_id>/image
    if uri.startswith(ITEM_IMAGE_URI_PREFIX):
        remainder = uri[len(ITEM_IMAGE_URI_PREFIX):]
        if remainder.endswith("/image"):
            item_id = remainder[:-6]  # Remove "/image"
            return _resolve_item_image(item_id, saiverse_home)

    # saiverse://persona/<persona_id>/image or saiverse://persona/self/image
    if uri.startswith(PERSONA_URI_PREFIX):
        remainder = uri[len(PERSONA_URI_PREFIX):]
        if remainder.endswith("/image"):
            target_id = remainder[:-6]  # Remove "/image"
            if target_id == "self":
                if not persona_id:
                    LOGGER.warning("Cannot resolve 'self' without persona_id context")
                    return None
                target_id = persona_id
            return _resolve_persona_image(target_id, saiverse_home)

    # saiverse://building/<building_id>/image or saiverse://building/current/image
    if uri.startswith(BUILDING_URI_PREFIX):
        remainder = uri[len(BUILDING_URI_PREFIX):]
        if remainder.endswith("/image"):
            target_id = remainder[:-6]  # Remove "/image"
            if target_id == "current":
                if not building_id:
                    LOGGER.warning("Cannot resolve 'current' without building_id context")
                    return None
                target_id = building_id
            return _resolve_building_image(target_id, saiverse_home)

    return None


def _resolve_item_image(item_id: str, saiverse_home: Path) -> Optional[Path]:
    """Resolve an item's image path from the database."""
    try:
        from database.session import SessionLocal
        from database.models import Item

        session = SessionLocal()
        try:
            item = session.query(Item).filter(Item.ITEM_ID == item_id).first()
            if not item or not item.FILE_PATH:
                LOGGER.debug("Item %s not found or has no file_path", item_id)
                return None
            if item.TYPE and item.TYPE.lower() != "picture":
                LOGGER.debug("Item %s is not a picture type (type=%s)", item_id, item.TYPE)
                return None

            file_path = Path(item.FILE_PATH)
            # Handle relative paths
            if not file_path.is_absolute():
                file_path = saiverse_home / item.FILE_PATH

            if file_path.exists():
                return file_path

            # Try recovery for legacy paths
            if "image" in file_path.parts:
                idx = file_path.parts.index("image")
                rel = Path(*file_path.parts[idx:])
                candidate = saiverse_home / rel
                if candidate.exists():
                    return candidate

            # Fallback: just filename
            candidate = saiverse_home / "image" / file_path.name
            if candidate.exists():
                return candidate

            LOGGER.debug("Item %s file not found: %s", item_id, item.FILE_PATH)
            return None
        finally:
            session.close()
    except Exception as exc:
        LOGGER.warning("Failed to resolve item image for %s: %s", item_id, exc)
        return None


def _resolve_persona_image(persona_id: str, saiverse_home: Path) -> Optional[Path]:
    """Resolve a persona's avatar image path from the database."""
    try:
        from database.session import SessionLocal
        from database.models import AI

        session = SessionLocal()
        try:
            ai = session.query(AI).filter(AI.AIID == persona_id).first()
            if not ai or not ai.APPEARANCE_IMAGE_PATH:
                LOGGER.debug("Persona %s not found or has no appearance image", persona_id)
                return None

            image_path = ai.APPEARANCE_IMAGE_PATH

            # Handle API URL format
            if image_path.startswith("/api/media/images/"):
                filename = image_path[len("/api/media/images/"):]
                return saiverse_home / "image" / filename

            # Handle saiverse:// URI
            if image_path.startswith("saiverse://"):
                return resolve_media_uri(image_path)

            # Handle filesystem path
            file_path = Path(image_path)
            if not file_path.is_absolute():
                file_path = saiverse_home / image_path

            if file_path.exists():
                return file_path

            # Try recovery
            if "image" in file_path.parts:
                idx = file_path.parts.index("image")
                rel = Path(*file_path.parts[idx:])
                candidate = saiverse_home / rel
                if candidate.exists():
                    return candidate

            candidate = saiverse_home / "image" / file_path.name
            if candidate.exists():
                return candidate

            LOGGER.debug("Persona %s image not found: %s", persona_id, image_path)
            return None
        finally:
            session.close()
    except Exception as exc:
        LOGGER.warning("Failed to resolve persona image for %s: %s", persona_id, exc)
        return None


def _resolve_building_image(building_id: str, saiverse_home: Path) -> Optional[Path]:
    """Resolve a building's interior image path from the database."""
    try:
        from database.session import SessionLocal
        from database.models import Building

        session = SessionLocal()
        try:
            building = session.query(Building).filter(Building.BUILDINGID == building_id).first()
            if not building or not building.IMAGE_PATH:
                LOGGER.debug("Building %s not found or has no image", building_id)
                return None

            image_path = building.IMAGE_PATH

            # Handle API URL format
            if image_path.startswith("/api/media/images/"):
                filename = image_path[len("/api/media/images/"):]
                return saiverse_home / "image" / filename

            # Handle saiverse:// URI
            if image_path.startswith("saiverse://"):
                return resolve_media_uri(image_path)

            # Handle filesystem path
            file_path = Path(image_path)
            if not file_path.is_absolute():
                file_path = saiverse_home / image_path

            if file_path.exists():
                return file_path

            # Try recovery
            if "image" in file_path.parts:
                idx = file_path.parts.index("image")
                rel = Path(*file_path.parts[idx:])
                candidate = saiverse_home / rel
                if candidate.exists():
                    return candidate

            candidate = saiverse_home / "image" / file_path.name
            if candidate.exists():
                return candidate

            LOGGER.debug("Building %s image not found: %s", building_id, image_path)
            return None
        finally:
            session.close()
    except Exception as exc:
        LOGGER.warning("Failed to resolve building image for %s: %s", building_id, exc)
        return None


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
        
        # Try to resolve path from URI first
        uri = item.get("uri")
        path = None
        if uri:
            path = resolve_media_uri(uri)
        
        # Fallback: use direct path field if URI resolution failed
        if path is None:
            direct_path = item.get("path")
            if direct_path:
                if isinstance(direct_path, Path):
                    path = direct_path
                else:
                    path = Path(direct_path)
        
        if path is None or not path.exists():
            LOGGER.warning("Image URI %s could not be resolved or file missing (path=%s)", uri, path)
            continue
        
        mime_type = item.get("mime_type") or mimetypes.guess_type(path)[0] or "image/png"
        results.append(
            {
                "uri": uri or str(path),
                "path": path,
                "mime_type": mime_type,
            }
        )
    return results


def _summary_path_for_media(path: Path) -> Path:
    return path.with_suffix(path.suffix + SUMMARY_SUFFIX)


def get_media_summary(path: Path) -> Optional[str]:
    summary_path = _summary_path_for_media(path)
    if not summary_path.exists():
        return None
    try:
        return summary_path.read_text(encoding="utf-8").strip()
    except OSError:
        LOGGER.exception("Failed to read media summary: %s", summary_path)
        return None


def save_media_summary(path: Path, summary: str) -> None:
    summary_path = _summary_path_for_media(path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    text = summary.strip()
    if not text:
        text = summary.strip()
    try:
        summary_path.write_text(text, encoding="utf-8")
    except OSError:
        LOGGER.exception("Failed to write media summary: %s", summary_path)


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


def store_document_text(content: str, *, source: str = "generated") -> Tuple[Dict[str, str], Path]:
    """Store text content as a document file and return metadata and path."""
    dest_dir = _ensure_document_dir()
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}.txt"
    dest_path = dest_dir / filename
    try:
        dest_path.write_text(content, encoding="utf-8")
    except OSError:
        LOGGER.exception("Failed to write document file: %s", dest_path)
        raise
    metadata = {
        "type": "document",
        "uri": f"{DOCUMENT_URI_PREFIX}{filename}",
        "mime_type": "text/plain",
        "source": source,
    }
    return metadata, dest_path


def store_document_bytes(
    data: bytes, mime_type: str, *, ext: str = "", source: str = "uploaded"
) -> Tuple[Dict[str, str], Path]:
    """Store binary document data (e.g., PDF) and return metadata and path.

    Args:
        data: Raw binary content
        mime_type: MIME type (e.g., "application/pdf")
        ext: File extension override (e.g., ".pdf"). Auto-detected from mime_type if empty.
        source: Source identifier for metadata
    """
    dest_dir = _ensure_document_dir()
    if not ext:
        ext = mimetypes.guess_extension(mime_type) or ".bin"
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}{ext}"
    dest_path = dest_dir / filename
    try:
        dest_path.write_bytes(data)
    except OSError:
        LOGGER.exception("Failed to write binary document file: %s", dest_path)
        raise
    metadata = {
        "type": "document",
        "uri": f"{DOCUMENT_URI_PREFIX}{filename}",
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


def resize_image_if_needed(data: bytes, mime_type: str, max_bytes: int) -> Tuple[bytes, str]:
    """
    Resize an image if it exceeds max_bytes when base64-encoded.
    Returns (resized_bytes, effective_mime_type).
    Base64 encoding increases size by ~33%, so we target max_bytes * 0.75 for raw bytes.
    """
    if Image is None:
        LOGGER.warning("PIL not available; cannot resize image")
        return data, mime_type

    # Base64 encoding increases size by ~33%, so target 75% of max_bytes
    target_bytes = int(max_bytes * 0.75)

    if len(data) <= target_bytes:
        return data, mime_type

    try:
        img = Image.open(BytesIO(data))

        # Calculate scale factor based on byte size ratio
        scale = (target_bytes / len(data)) ** 0.5  # Square root for 2D scaling
        new_width = int(img.width * scale)
        new_height = int(img.height * scale)

        LOGGER.info(
            "Resizing image from %dx%d (%d bytes) to %dx%d (target: %d bytes)",
            img.width, img.height, len(data), new_width, new_height, target_bytes
        )

        # Resize image
        resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Save as JPEG with quality adjustment if still too large
        buf = BytesIO()
        quality = 85
        output_mime = "image/jpeg"

        for attempt in range(3):
            buf.seek(0)
            buf.truncate()
            resized.convert("RGB").save(buf, format="JPEG", quality=quality)
            result_bytes = buf.getvalue()

            if len(result_bytes) <= target_bytes:
                LOGGER.info("Resized image to %d bytes (quality=%d)", len(result_bytes), quality)
                return result_bytes, output_mime

            quality -= 15  # Reduce quality for next attempt

        # If still too large, return the best we got
        LOGGER.warning(
            "Could not resize image below target (%d bytes > %d bytes); using best effort",
            len(result_bytes), target_bytes
        )
        return result_bytes, output_mime

    except Exception:
        LOGGER.exception("Failed to resize image; using original")
        return data, mime_type


def resize_image_for_llm_context(
    data: bytes,
    mime_type: str,
    max_long_edge: int = 768,
    quality: int = 85,
) -> Tuple[bytes, str]:
    """
    Resize an image so that its longest edge does not exceed max_long_edge pixels.
    This is optimized for LLM visual context to minimize token usage while preserving quality.
    
    For Gemini 2.5/3: Images ≤768px fit in 1 tile = 258 tokens.
    For OpenAI: Low detail = 85 tokens per 512px tile.
    For Claude 4: ~(width * height / 750) tokens.
    
    Args:
        data: Raw image bytes.
        mime_type: MIME type of the image.
        max_long_edge: Maximum length for the longest edge (default: 768px for Gemini optimization).
        quality: JPEG quality for output (default: 85).
    
    Returns:
        Tuple of (resized_bytes, effective_mime_type).
    """
    if Image is None:
        LOGGER.warning("PIL not available; cannot resize image for LLM context")
        return data, mime_type

    try:
        img = Image.open(BytesIO(data))
        original_width, original_height = img.size
        
        # Check if resize is needed
        long_edge = max(original_width, original_height)
        if long_edge <= max_long_edge:
            LOGGER.debug(
                "Image %dx%d already within %dpx limit; no resize needed",
                original_width, original_height, max_long_edge
            )
            return data, mime_type
        
        # Calculate new dimensions preserving aspect ratio
        scale = max_long_edge / long_edge
        new_width = int(original_width * scale)
        new_height = int(original_height * scale)
        
        LOGGER.info(
            "Resizing image for LLM context: %dx%d -> %dx%d (max_long_edge=%d)",
            original_width, original_height, new_width, new_height, max_long_edge
        )
        
        # Resize with high-quality resampling
        resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Convert to JPEG for efficient storage (unless PNG with transparency is needed)
        buf = BytesIO()
        if img.mode in ("RGBA", "P") and mime_type == "image/png":
            # Preserve PNG for images with transparency
            resized.save(buf, format="PNG", optimize=True)
            output_mime = "image/png"
        else:
            # Use JPEG for most images
            resized.convert("RGB").save(buf, format="JPEG", quality=quality)
            output_mime = "image/jpeg"
        
        result_bytes = buf.getvalue()
        LOGGER.info(
            "Resized image: %d bytes -> %d bytes",
            len(data), len(result_bytes)
        )
        return result_bytes, output_mime
        
    except Exception:
        LOGGER.exception("Failed to resize image for LLM context; using original")
        return data, mime_type


def load_image_bytes_for_llm(path: Path, mime_type: str, max_bytes: Optional[int] = None) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Return (bytes, effective_mime) for LLM consumption.
    Converts unsupported formats to PNG when Pillow is available.
    If max_bytes is specified, resizes image to fit within that limit (accounting for base64 encoding).
    """
    target_mime = mime_type.lower()
    if target_mime not in SUPPORTED_LLM_IMAGE_MIME and Image is not None:
        try:
            with Image.open(path) as img:
                buf = BytesIO()
                img.save(buf, format="PNG")
                LOGGER.debug("Converted image %s to PNG for LLM input", path)
                data = buf.getvalue()
                effective_mime = "image/png"
        except Exception:
            LOGGER.exception("Failed to convert image %s to PNG; falling back to raw bytes", path)
            try:
                data = path.read_bytes()
                effective_mime = target_mime
            except OSError:
                LOGGER.exception("Failed to read image for LLM: %s", path)
                return None, None
    else:
        try:
            data = path.read_bytes()
            effective_mime = target_mime
        except OSError:
            LOGGER.exception("Failed to read image for LLM: %s", path)
            return None, None

    if effective_mime not in SUPPORTED_LLM_IMAGE_MIME:
        LOGGER.warning("Using raw bytes for potentially unsupported mime '%s'", mime_type)

    # Resize if max_bytes is specified and image is too large
    if max_bytes is not None:
        data, effective_mime = resize_image_if_needed(data, effective_mime, max_bytes)

    return data, effective_mime
