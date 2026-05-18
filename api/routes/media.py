import logging
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import mimetypes

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse

LOGGER = logging.getLogger(__name__)
from saiverse.media_utils import (
    resize_image_if_needed,
    resize_image_for_llm_context,
    convert_image_to_webp,
    _ensure_image_dir,
    _ensure_document_dir,
    _ensure_audio_dir,
    _ensure_video_dir,
    IMAGE_URI_PREFIX,  # noqa: F401  (re-exported for legacy callers)
    DOCUMENT_URI_PREFIX,  # noqa: F401
)
from saiverse.ffmpeg_runner import (
    is_ffmpeg_available,
    normalize_audio,
    normalize_video,
)

# Hard upper bounds (raw input file size, before normalization).
# These are sanity checks so we don't accept arbitrarily large uploads;
# the real long-tail control is the duration check inside ffmpeg_runner.
AUDIO_INPUT_MAX_BYTES = 100 * 1024 * 1024   # 100 MB raw
VIDEO_INPUT_MAX_BYTES = 500 * 1024 * 1024   # 500 MB raw
AUDIO_MAX_DURATION_SEC = 300.0  # 5 minutes
VIDEO_MAX_DURATION_SEC = 90.0   # 90 seconds

router = APIRouter()

@router.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    """
    Upload an image file. Resizes to max 768px long edge for LLM optimization.
    Returns: {"url": "/api/media/images/..."}
    """
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    try:
        content = await file.read()
        
        # Step 1: Resize for LLM context (max long edge = 768px for optimal tokenization)
        resized_content, mime_type = resize_image_for_llm_context(
            content, file.content_type, max_long_edge=768
        )
        
        # Step 2: Further compress if still too large (~500KB limit for avatars/general use)
        resized_content, mime_type = resize_image_if_needed(resized_content, mime_type, 500 * 1024)
        
        dest_dir = _ensure_image_dir()
        
        # Determine extension
        ext = mimetypes.guess_extension(mime_type) or ".png"
        if ext == ".jpe": ext = ".jpg"
        
        # Generate filename
        from datetime import datetime
        from uuid import uuid4
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}{ext}"
        dest_path = dest_dir / filename
        
        dest_path.write_bytes(resized_content)
        
        return {
            "url": f"/api/media/images/{filename}",
            "filename": filename,
            "type": "image",
            "relative_path": f"image/{filename}"
        }

    except Exception as e:
        LOGGER.error("Upload failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.post("/upload-hires")
async def upload_image_hires(file: UploadFile = File(...)):
    """
    Upload an image without LLM-oriented downscaling.
    Only converts to WebP (lossless-ish, q=90) to keep file size reasonable
    while preserving the original resolution. Intended for assets that are
    *displayed* at full size (City map background, future wallpapers, etc.)
    Returns: {"url": "/api/media/images/..."}
    """
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    try:
        content = await file.read()

        converted, mime_type = convert_image_to_webp(content, file.content_type, quality=90)

        dest_dir = _ensure_image_dir()

        ext = mimetypes.guess_extension(mime_type) or ".webp"
        if ext == ".jpe":
            ext = ".jpg"

        from datetime import datetime
        from uuid import uuid4
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}{ext}"
        dest_path = dest_dir / filename

        dest_path.write_bytes(converted)

        return {
            "url": f"/api/media/images/{filename}",
            "filename": filename,
            "type": "image",
            "relative_path": f"image/{filename}"
        }

    except Exception as e:
        LOGGER.error("Hi-res upload failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.post("/upload-document")
async def upload_document(file: UploadFile = File(...)):
    """
    Upload a text document file.
    Returns: {"url": "/api/media/documents/...", "relative_path": "documents/..."}
    """
    # Accept text files
    content_type = file.content_type or ""
    accepted_types = {"application/json", "application/xml", "application/pdf"}
    if not content_type.startswith("text/") and content_type not in accepted_types:
        raise HTTPException(status_code=400, detail="File must be a text document or PDF")

    try:
        content = await file.read()
        
        dest_dir = _ensure_document_dir()
        
        # Determine extension from original filename or content type
        original_ext = Path(file.filename or "").suffix if file.filename else ""
        if not original_ext:
            ext = mimetypes.guess_extension(content_type) or ".txt"
        else:
            ext = original_ext
        
        from datetime import datetime
        from uuid import uuid4
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}{ext}"
        dest_path = dest_dir / filename
        
        dest_path.write_bytes(content)
        
        return {
            "url": f"/api/media/documents/{filename}",
            "filename": filename,
            "type": "document",
            "relative_path": f"documents/{filename}"
        }

    except Exception as e:
        LOGGER.error("Upload failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.post("/upload-audio")
async def upload_audio(file: UploadFile = File(...)):
    """
    Upload an audio file. Normalizes to opus 24kbps mono 16kHz ogg via ffmpeg.
    Rejects audio longer than 5 minutes. Returns:
        {"url": "/api/media/audio/...", "type": "audio", "relative_path": "audio/..."}
    """
    content_type = (file.content_type or "").lower()
    if not content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="File must be an audio file")

    if not is_ffmpeg_available():
        raise HTTPException(
            status_code=503,
            detail="ffmpeg is not available in this environment; audio upload is disabled.",
        )

    content = await file.read()
    if len(content) > AUDIO_INPUT_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Audio file too large ({len(content)} bytes > {AUDIO_INPUT_MAX_BYTES} bytes).",
        )

    tmp_input: Path | None = None
    try:
        # Persist upload to a temp file for ffmpeg input
        original_ext = Path(file.filename or "").suffix or mimetypes.guess_extension(content_type) or ".bin"
        with tempfile.NamedTemporaryFile(suffix=original_ext, delete=False) as tmp:
            tmp.write(content)
            tmp_input = Path(tmp.name)

        # Normalize directly to the destination so we don't leave intermediate files
        dest_dir = _ensure_audio_dir()
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}.ogg"
        dest_path = dest_dir / filename

        success, error = normalize_audio(
            tmp_input, dest_path, max_duration=AUDIO_MAX_DURATION_SEC
        )
        if not success:
            # Distinguish duration overflow from other ffmpeg failures
            if "exceeds limit" in error:
                raise HTTPException(status_code=400, detail=f"音声が長すぎます: {error}")
            LOGGER.warning("Audio normalization failed: %s", error)
            raise HTTPException(status_code=500, detail=f"Audio normalization failed: {error}")

        return {
            "url": f"/api/media/audio/{filename}",
            "filename": filename,
            "type": "audio",
            "relative_path": f"audio/{filename}",
            "mime_type": "audio/ogg",
        }
    except HTTPException:
        raise
    except Exception as e:
        LOGGER.error("Audio upload failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
    finally:
        if tmp_input is not None and tmp_input.exists():
            try:
                tmp_input.unlink()
            except OSError:
                LOGGER.warning("Failed to remove temp audio file: %s", tmp_input)


@router.post("/upload-video")
async def upload_video(file: UploadFile = File(...)):
    """
    Upload a video file. Normalizes to 1FPS 480p + opus 24kbps mono 16kHz mp4 via ffmpeg.
    Rejects video longer than 90 seconds. Returns:
        {"url": "/api/media/video/...", "type": "video", "relative_path": "video/..."}
    """
    content_type = (file.content_type or "").lower()
    if not content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="File must be a video file")

    if not is_ffmpeg_available():
        raise HTTPException(
            status_code=503,
            detail="ffmpeg is not available in this environment; video upload is disabled.",
        )

    content = await file.read()
    if len(content) > VIDEO_INPUT_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Video file too large ({len(content)} bytes > {VIDEO_INPUT_MAX_BYTES} bytes).",
        )

    tmp_input: Path | None = None
    try:
        original_ext = Path(file.filename or "").suffix or mimetypes.guess_extension(content_type) or ".bin"
        with tempfile.NamedTemporaryFile(suffix=original_ext, delete=False) as tmp:
            tmp.write(content)
            tmp_input = Path(tmp.name)

        dest_dir = _ensure_video_dir()
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}.mp4"
        dest_path = dest_dir / filename

        success, error = normalize_video(
            tmp_input, dest_path, max_duration=VIDEO_MAX_DURATION_SEC
        )
        if not success:
            if "exceeds limit" in error:
                raise HTTPException(status_code=400, detail=f"動画が長すぎます: {error}")
            LOGGER.warning("Video normalization failed: %s", error)
            raise HTTPException(status_code=500, detail=f"Video normalization failed: {error}")

        return {
            "url": f"/api/media/video/{filename}",
            "filename": filename,
            "type": "video",
            "relative_path": f"video/{filename}",
            "mime_type": "video/mp4",
        }
    except HTTPException:
        raise
    except Exception as e:
        LOGGER.error("Video upload failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")
    finally:
        if tmp_input is not None and tmp_input.exists():
            try:
                tmp_input.unlink()
            except OSError:
                LOGGER.warning("Failed to remove temp video file: %s", tmp_input)


@router.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    """
    Upload any file (image, audio, video, or document). Auto-detects type and returns
    the appropriate path.
    Returns: {"url": "...", "type": "image"|"audio"|"video"|"document", "relative_path": "..."}
    """
    content_type = (file.content_type or "").lower()

    if content_type.startswith("image/"):
        return await upload_image(file)
    elif content_type.startswith("audio/"):
        return await upload_audio(file)
    elif content_type.startswith("video/"):
        return await upload_video(file)
    elif content_type.startswith("text/") or content_type in [
        "application/json", "application/xml", "application/pdf",
    ]:
        return await upload_document(file)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {content_type}. Must be image, audio, video, text, or PDF.",
        )

@router.get("/images/{filename}")
async def serve_image(filename: str):
    """Serve an uploaded image."""
    dest_dir = _ensure_image_dir()
    path = dest_dir / filename
    
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
        
    return FileResponse(path)

@router.get("/documents/{filename}")
async def serve_document(filename: str):
    """Serve an uploaded document."""
    dest_dir = _ensure_document_dir()
    path = dest_dir / filename

    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    return FileResponse(path)


@router.get("/audio/{filename}")
async def serve_audio(filename: str):
    """Serve a normalized audio file."""
    dest_dir = _ensure_audio_dir()
    path = dest_dir / filename

    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")

    return FileResponse(path, media_type="audio/ogg")


@router.get("/video/{filename}")
async def serve_video(filename: str):
    """Serve a normalized video file."""
    dest_dir = _ensure_video_dir()
    path = dest_dir / filename

    if not path.exists():
        raise HTTPException(status_code=404, detail="Video not found")

    return FileResponse(path, media_type="video/mp4")
