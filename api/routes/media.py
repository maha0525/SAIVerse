from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
import shutil
import mimetypes
from media_utils import resize_image_if_needed, _ensure_image_dir, IMAGE_URI_PREFIX

router = APIRouter()

@router.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    """
    Upload an image file. Resizes internally if needed.
    Returns: {"url": "/api/media/images/..."}
    """
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    try:
        content = await file.read()
        
        # Resize logic (Limit to ~500KB raw bytes to ensure smooth UI)
        # 500KB is generous for avatars.
        # media_utils.resize_image_if_needed takes bytes, mime, max_bytes
        # Note: max_bytes in that function is a bit loose (base64 target), but good enough.
        # Let's say 500KB = 500 * 1024
        resized_content, mime_type = resize_image_if_needed(content, file.content_type, 500 * 1024)
        
        dest_dir = _ensure_image_dir()
        
        # Determine extension
        ext = mimetypes.guess_extension(mime_type) or ".png"
        if ext == ".jpe": ext = ".jpg"
        
        # Generate filename (using same pattern as media_utils would be ideal, but we can do simple here)
        from datetime import datetime
        from uuid import uuid4
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex}{ext}"
        dest_path = dest_dir / filename
        
        dest_path.write_bytes(resized_content)
        
        # Return URL
        # We need a way to serve this. We'll add a GET endpoint below.
        return {
            "url": f"/api/media/images/{filename}",
            "filename": filename
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.get("/images/{filename}")
async def serve_image(filename: str):
    """Serve an uploaded image."""
    dest_dir = _ensure_image_dir()
    path = dest_dir / filename
    
    if not path.exists():
        # Security check: ensure path is within dest_dir to prevent traversal (Path gives basic check but good to be sure)
        raise HTTPException(status_code=404, detail="Image not found")
        
    return FileResponse(path)
