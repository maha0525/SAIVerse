from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
import shutil
import mimetypes
from media_utils import resize_image_if_needed, resize_image_for_llm_context, _ensure_image_dir, _ensure_document_dir, IMAGE_URI_PREFIX, DOCUMENT_URI_PREFIX

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
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.post("/upload-document")
async def upload_document(file: UploadFile = File(...)):
    """
    Upload a text document file.
    Returns: {"url": "/api/media/documents/...", "relative_path": "documents/..."}
    """
    # Accept text files
    content_type = file.content_type or ""
    if not content_type.startswith("text/") and content_type not in ["application/json", "application/xml"]:
        raise HTTPException(status_code=400, detail="File must be a text document")

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
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    """
    Upload any file (image or document). Auto-detects type and returns appropriate path.
    Returns: {"url": "...", "type": "image"|"document", "relative_path": "..."}
    """
    content_type = file.content_type or ""
    
    if content_type.startswith("image/"):
        return await upload_image(file)
    elif content_type.startswith("text/") or content_type in ["application/json", "application/xml"]:
        return await upload_document(file)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {content_type}. Must be image or text.")

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
