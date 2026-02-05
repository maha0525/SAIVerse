"""Unified image generation tool supporting multiple backends.

Supported models:
- nano_banana: Gemini 2.5 Flash Image (fast, aspect ratio control)
- nano_banana_pro: Gemini 3 Pro Image (high quality, aspect ratio + resolution control)
- gpt_image_1_5: OpenAI GPT Image 1.5 (state of the art)

Input image URI formats:
- saiverse://image/<filename> - Generated image file
- saiverse://item/<item_id>/image - Picture item's image
- saiverse://persona/<persona_id>/image - Persona's avatar
- saiverse://persona/self/image - Your own avatar
- saiverse://building/<building_id>/image - Building's interior
- saiverse://building/current/image - Current building's interior
"""
import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from dotenv import load_dotenv

from tools.core import ToolSchema, ToolResult
from media_utils import store_image_bytes, resolve_extended_media_uri

load_dotenv()

logger = logging.getLogger(__name__)

# Type definitions
ModelType = Literal["nano_banana", "nano_banana_pro", "gpt_image_1_5"]
AspectRatioType = Literal["1:1", "16:9", "9:16", "4:3", "3:4"]
QualityType = Literal["low", "medium", "high", "auto"]


def _aspect_ratio_to_openai_size(aspect_ratio: str) -> str:
    """Convert aspect ratio to OpenAI size format."""
    mapping = {
        "1:1": "1024x1024",
        "16:9": "1536x1024",
        "9:16": "1024x1536",
        "4:3": "1536x1024",  # Use landscape for 4:3
        "3:4": "1024x1536",  # Use portrait for 3:4
    }
    return mapping.get(aspect_ratio, "1024x1024")


def _quality_to_gemini_resolution(quality: str) -> str:
    """Convert quality to Gemini resolution format."""
    mapping = {
        "low": "HD",
        "medium": "HD",
        "high": "4K",
        "auto": "4K",
    }
    return mapping.get(quality, "4K")


def _resolve_input_images(
    input_images: Optional[List[str]],
    persona_id: Optional[str],
    building_id: Optional[str],
) -> List[Path]:
    """Resolve input image URIs to file paths."""
    if not input_images:
        return []

    resolved = []
    for uri in input_images:
        path = resolve_extended_media_uri(uri, persona_id, building_id)
        if path and path.exists():
            resolved.append(path)
            logger.info(f"[image_generator] Resolved input image: {uri} -> {path}")
        else:
            logger.warning(f"[image_generator] Failed to resolve input image: {uri}")

    return resolved


def _load_image_bytes(path: Path) -> Tuple[bytes, str]:
    """Load image bytes and determine MIME type."""
    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
    return path.read_bytes(), mime_type


def _generate_with_nano_banana(
    prompt: str,
    aspect_ratio: str = "1:1",
    input_image_paths: Optional[List[Path]] = None,
) -> Tuple[bytes, str]:
    """Generate image using Gemini 2.5 Flash Image (nano banana)."""
    from llm_clients.gemini_utils import build_gemini_clients
    from google.genai import types

    _free_client, _paid_client, _active_client = build_gemini_clients(prefer_paid=True)
    if _paid_client is None:
        raise RuntimeError("GEMINI_API_KEY (paid tier) is required for image generation.")

    # Build contents with optional input images
    contents: List = []
    if input_image_paths:
        for img_path in input_image_paths:
            img_bytes, img_mime = _load_image_bytes(img_path)
            contents.append(types.Part.from_bytes(data=img_bytes, mime_type=img_mime))
            logger.info(f"[nano_banana] Added input image: {img_path.name}")
    contents.append(prompt)

    resp = _paid_client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio
            )
        )
    )

    _log_gemini_response(resp)

    if not resp.candidates:
        raise RuntimeError("No candidates returned from Gemini")

    for part in resp.candidates[0].content.parts:
        if part.inline_data is not None:
            data = part.inline_data.data
            mime = part.inline_data.mime_type or "image/png"
            return data, mime

    raise RuntimeError("No image data in response")


def _generate_with_nano_banana_pro(
    prompt: str,
    aspect_ratio: str = "16:9",
    quality: str = "high",
    input_image_paths: Optional[List[Path]] = None,
) -> Tuple[bytes, str]:
    """Generate image using Gemini 3 Pro Image (nano banana Pro)."""
    from llm_clients.gemini_utils import build_gemini_clients
    from google.genai import types

    _free_client, _paid_client, _active_client = build_gemini_clients(prefer_paid=True)
    if _paid_client is None:
        raise RuntimeError("GEMINI_API_KEY (paid tier) is required for image generation.")

    resolution = _quality_to_gemini_resolution(quality)

    # Build contents with optional input images
    contents: List = []
    if input_image_paths:
        for img_path in input_image_paths:
            img_bytes, img_mime = _load_image_bytes(img_path)
            contents.append(types.Part.from_bytes(data=img_bytes, mime_type=img_mime))
            logger.info(f"[nano_banana_pro] Added input image: {img_path.name}")
    contents.append(prompt)

    resp = _paid_client.models.generate_content(
        model="gemini-3-pro-image-preview",
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=resolution
            )
        )
    )

    _log_gemini_response(resp)

    if not resp.candidates:
        raise RuntimeError("No candidates returned from Gemini")

    for part in resp.candidates[0].content.parts:
        if part.inline_data is not None:
            data = part.inline_data.data
            mime = part.inline_data.mime_type or "image/png"
            return data, mime

    raise RuntimeError("No image data in response")


def _generate_with_gpt_image(
    prompt: str,
    aspect_ratio: str = "1:1",
    quality: str = "high",
    input_image_paths: Optional[List[Path]] = None,
) -> Tuple[bytes, str]:
    """Generate image using OpenAI GPT Image 1.5."""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set.")

    client = OpenAI(api_key=api_key)
    size = _aspect_ratio_to_openai_size(aspect_ratio)
    effective_quality = quality if quality != "auto" else "high"

    if input_image_paths:
        # Use images.edit for input image processing
        logger.info(
            f"[gpt_image] Editing with {len(input_image_paths)} input images, "
            f"size={size}, quality={effective_quality}"
        )

        # Prepare input images as file-like objects
        image_files = []
        for img_path in input_image_paths:
            image_files.append(open(img_path, "rb"))
            logger.info(f"[gpt_image] Added input image: {img_path.name}")

        try:
            result = client.images.edit(
                model="gpt-image-1.5",
                image=image_files if len(image_files) > 1 else image_files[0],
                prompt=prompt,
                size=size,
                quality=effective_quality,
                n=1,
            )
        finally:
            for f in image_files:
                f.close()
    else:
        # Standard generation without input images
        logger.info(f"[gpt_image] Generating with size={size}, quality={effective_quality}")

        result = client.images.generate(
            model="gpt-image-1.5",
            prompt=prompt,
            size=size,
            quality=effective_quality,
            n=1,
        )

    if not result.data or not result.data[0].b64_json:
        raise RuntimeError("No image data returned from OpenAI")

    image_bytes = base64.b64decode(result.data[0].b64_json)
    # GPT Image returns PNG by default
    return image_bytes, "image/png"


def _log_gemini_response(resp) -> None:
    """Log Gemini response details for debugging."""
    candidates_count = len(resp.candidates) if resp.candidates else 0
    logger.info(f"[image_generator] Response: candidates={candidates_count}")

    if hasattr(resp, 'prompt_feedback') and resp.prompt_feedback:
        pf = resp.prompt_feedback
        logger.info(
            f"[image_generator] prompt_feedback: "
            f"block_reason={getattr(pf, 'block_reason', None)}, "
            f"safety_ratings={getattr(pf, 'safety_ratings', None)}"
        )

    if resp.candidates:
        for i, cand in enumerate(resp.candidates):
            finish_reason = getattr(cand, 'finish_reason', None)
            parts_count = len(cand.content.parts) if cand.content and cand.content.parts else 0
            logger.info(
                f"[image_generator] Candidate[{i}]: "
                f"finish_reason={finish_reason}, parts={parts_count}"
            )
            if cand.content and cand.content.parts:
                for j, part in enumerate(cand.content.parts):
                    has_inline = part.inline_data is not None
                    has_text = part.text is not None
                    logger.info(
                        f"[image_generator] Part[{i}][{j}]: "
                        f"has_inline_data={has_inline}, has_text={has_text}"
                    )


def generate_image(
    prompt: str,
    model: ModelType = "nano_banana_pro",
    aspect_ratio: AspectRatioType = "1:1",
    quality: QualityType = "high",
    title: Optional[str] = None,
    input_images: Optional[List[str]] = None,
) -> Tuple[str, ToolResult, Optional[str], Optional[dict]]:
    """Generate an image using the specified model.

    Args:
        prompt: Image generation prompt describing what to create.
        model: Which image generation model to use:
            - nano_banana: Fast with aspect ratio control (Gemini 2.5 Flash)
            - nano_banana_pro: High quality with aspect ratio + resolution control (Gemini 3 Pro)
            - gpt_image_1_5: State of the art quality (OpenAI GPT Image 1.5)
        aspect_ratio: Image aspect ratio ("1:1", "16:9", "9:16", "4:3", "3:4")
        quality: Image quality level ("low", "medium", "high", "auto")
        title: Optional title for the generated image item
        input_images: Optional list of image URIs to use as reference/input.
            Supported URI formats:
            - saiverse://image/<filename>
            - saiverse://item/<item_id>/image
            - saiverse://persona/<persona_id>/image
            - saiverse://persona/self/image
            - saiverse://building/<building_id>/image
            - saiverse://building/current/image

    Returns:
        Tuple of (text, ToolResult, file_path, metadata)
    """
    from tools.context import get_active_manager, get_active_persona_id

    # Normalize empty strings to defaults
    if not aspect_ratio:
        aspect_ratio = "1:1"
    if not quality:
        quality = "high"
    if not model:
        model = "nano_banana_pro"

    # Get context for resolving URIs
    persona_id = get_active_persona_id()
    manager = get_active_manager()
    building_id = None
    if persona_id and manager:
        persona = manager.all_personas.get(persona_id)
        if persona:
            building_id = getattr(persona, "current_building_id", None)

    # Resolve input image URIs
    input_image_paths = _resolve_input_images(input_images, persona_id, building_id)

    logger.info(
        f"[image_generator] Starting generation: model={model}, "
        f"aspect_ratio={aspect_ratio}, quality={quality}, "
        f"input_images={len(input_image_paths)}"
    )

    try:
        if model == "nano_banana":
            image_data, mime = _generate_with_nano_banana(
                prompt, aspect_ratio, input_image_paths
            )
        elif model == "nano_banana_pro":
            image_data, mime = _generate_with_nano_banana_pro(
                prompt, aspect_ratio, quality, input_image_paths
            )
        elif model == "gpt_image_1_5":
            image_data, mime = _generate_with_gpt_image(
                prompt, aspect_ratio, quality, input_image_paths
            )
        else:
            raise ValueError(f"Unknown model: {model}")

    except Exception as exc:
        logger.exception(f"[image_generator] Generation failed: {exc}")
        error_text = (
            f"画像生成に失敗しました。\n\n"
            f"モデル: {model}\n"
            f"プロンプト:\n{prompt}\n\n"
            f"エラー: {exc}"
        )
        return error_text, ToolResult(None), None, None

    # Store the generated image
    metadata_entry, stored_path = store_image_bytes(
        image_data, mime, source=f"tool:generate_image:{model}"
    )
    snippet = f"![画像が生成されました]({stored_path.as_posix()})"
    metadata = {"media": [metadata_entry]}

    # Create picture item
    try:
        persona_id = get_active_persona_id()
        manager = get_active_manager()
        item_text = ""

        if persona_id and manager:
            item_name = title if title else f"生成画像_{stored_path.stem}"
            item_id = manager.create_picture_item(
                persona_id=persona_id,
                name=item_name,
                description=prompt,
                file_path=str(stored_path),
            )
            item_text = f"\n\n画像をアイテムとして登録しました（アイテムID: {item_id}）。"
    except Exception as exc:
        logger.warning(f"Failed to create picture item: {exc}")
        item_text = ""

    text = (
        f"画像が生成されました。\n\n"
        f"モデル: {model}\n"
        f"プロンプト:\n{prompt}"
        f"{item_text}"
    )

    return text, ToolResult(snippet), stored_path.as_posix(), metadata


def schema() -> ToolSchema:
    return ToolSchema(
        name="generate_image",
        description=(
            "Generate an image from a text prompt, optionally using reference images. "
            "Supports multiple AI models:\n"
            "- nano_banana: Fast generation with aspect ratio control (Gemini 2.5 Flash)\n"
            "- nano_banana_pro: High quality with aspect ratio and resolution control (Gemini 3 Pro)\n"
            "- gpt_image_1_5: State of the art photorealistic quality (OpenAI)\n\n"
            "Prompt tips:\n"
            "- Be specific and detailed about what you want\n"
            "- Include art style, lighting, mood, and composition\n"
            "- For GPT Image: longer, more detailed prompts work best\n\n"
            "Reference image URIs:\n"
            "- saiverse://persona/self/image - Your own avatar\n"
            "- saiverse://building/current/image - Current building's interior\n"
            "- saiverse://persona/<persona_id>/image - Another persona's avatar\n"
            "- saiverse://item/<item_id>/image - A picture item\n"
            "- saiverse://building/<building_id>/image - A building's interior"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed description of the image to generate"
                },
                "model": {
                    "type": "string",
                    "enum": ["nano_banana", "nano_banana_pro", "gpt_image_1_5"],
                    "description": (
                        "Image generation model: "
                        "nano_banana (fast, aspect ratio), nano_banana_pro (high quality, aspect ratio + resolution), "
                        "gpt_image_1_5 (state of the art)"
                    ),
                    "default": "nano_banana_pro"
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                    "description": "Image aspect ratio",
                    "default": "1:1"
                },
                "quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "auto"],
                    "description": "Image quality level",
                    "default": "high"
                },
                "title": {
                    "type": "string",
                    "description": "Optional title for the generated image item"
                },
                "input_images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of reference image URIs for style transfer or variations. "
                        "Use saiverse:// URIs like saiverse://persona/self/image"
                    )
                }
            },
            "required": ["prompt"],
        },
        result_type="string",
    )
