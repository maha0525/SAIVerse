import logging
from dotenv import load_dotenv

from llm_clients.gemini_utils import build_gemini_clients
from google.genai import types

from tools.core import ToolSchema, ToolResult
from media_utils import store_image_bytes

load_dotenv()

logger = logging.getLogger(__name__)

def generate_image_3pro(prompt: str, title: str | None = None, aspect_ratio: str = "16:9", resolution: str = "4K") -> tuple[str, ToolResult, str | None, dict | None]:
    """Generate an image and return (prompt, snippet, file path, metadata)."""
    from tools.context import get_active_manager, get_active_persona_id

    # 空文字列が渡された場合はデフォルト値を使用
    if not aspect_ratio:
        aspect_ratio = "16:9"
    if not resolution:
        resolution = "4K"

    _free_client, _paid_client, _active_client = build_gemini_clients(prefer_paid=True)
    if _paid_client is None:
        raise RuntimeError("GEMINI_API_KEY (paid tier) is required for image generation.")

    resp = _paid_client.models.generate_content(
        model="gemini-3-pro-image-preview",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=resolution
            )
        )
    )

    # Debug logging for response inspection
    candidates_count = len(resp.candidates) if resp.candidates else 0
    logger.info(f"[image_generator] Response: candidates={candidates_count}")
    if hasattr(resp, 'prompt_feedback') and resp.prompt_feedback:
        pf = resp.prompt_feedback
        logger.info(f"[image_generator] prompt_feedback: block_reason={getattr(pf, 'block_reason', None)}, safety_ratings={getattr(pf, 'safety_ratings', None)}")
    if resp.candidates:
        for i, cand in enumerate(resp.candidates):
            finish_reason = getattr(cand, 'finish_reason', None)
            parts_count = len(cand.content.parts) if cand.content and cand.content.parts else 0
            logger.info(f"[image_generator] Candidate[{i}]: finish_reason={finish_reason}, parts={parts_count}")
            if cand.content and cand.content.parts:
                for j, part in enumerate(cand.content.parts):
                    has_inline = part.inline_data is not None
                    has_text = part.text is not None
                    logger.info(f"[image_generator] Part[{i}][{j}]: has_inline_data={has_inline}, has_text={has_text}")

    if not resp.candidates:
        text = f"画像生成に失敗しました。\n\nPrompt:\n{prompt}"
        return text, ToolResult(None), None, None

    cand = resp.candidates[0]
    for part in cand.content.parts:
        if part.inline_data is not None:
            data = part.inline_data.data
            mime = part.inline_data.mime_type or "image/png"
            metadata_entry, stored_path = store_image_bytes(data, mime, source="tool:generate_image")
            snippet = f"![画像が生成されました]({stored_path.as_posix()})"
            text = f"画像が生成されました。\n\nPrompt:\n{prompt}"
            metadata = {"media": [metadata_entry]}

            # pictureアイテム作成（プロンプトをそのままDescriptionとして使用）
            try:
                summary = prompt  # プロンプトがそのまま説明文として最適

                # アイテムとして登録
                persona_id = get_active_persona_id()
                manager = get_active_manager()
                if persona_id and manager:
                    item_name = title if title else f"生成画像_{stored_path.stem}"
                    item_id = manager.create_picture_item(
                        persona_id=persona_id,
                        name=item_name,
                        description=summary,
                        file_path=str(stored_path),
                    )
                    text += f"\n\n画像をアイテムとして登録しました（アイテムID: {item_id}）。"
            except Exception as exc:
                # アイテム作成に失敗しても画像生成自体は成功しているので続行
                import logging
                logging.warning(f"アイテムとしての作成に失敗しました（画像生成は完了しています）:\n{exc}")

            return text, ToolResult(snippet), stored_path.as_posix(), metadata

    text = f"画像生成に失敗しました。\n\nPrompt:\n{prompt}"
    return text, ToolResult(None), None, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="generate_image_3pro",
        description="Generate an image from a text prompt using Gemini 3 Pro (Nano banana Pro) and save it to a file.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image generation prompt"},
                "title": {"type": "string", "description": "Optional title for the generated image item"}
            },
            "required": ["prompt"],
        },
        result_type="string",
    )
