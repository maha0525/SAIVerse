from dotenv import load_dotenv

from llm_clients.gemini_utils import build_gemini_clients
from google.genai import types

from tools.defs import ToolSchema, ToolResult
from media_utils import store_image_bytes

load_dotenv()


def generate_image(prompt: str) -> tuple[str, ToolResult, str | None, dict | None]:
    """Generate an image and return (prompt, snippet, file path, metadata)."""
    from tools.context import get_active_manager, get_active_persona_id
    from media_summary import ensure_image_summary

    _free_client, _paid_client, _active_client = build_gemini_clients(prefer_paid=True)
    if _paid_client is None:
        raise RuntimeError("GEMINI_API_KEY (paid tier) is required for image generation.")

    resp = _paid_client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"]  # ← TEXT を必ず含める
        )
    )
    if not resp.candidates:
        text = f"こんにちは、お絵描き妖精だよ！ごめん、失敗しちゃった……。\n\n使ったプロンプトはこれだよ：\n{prompt}\n\n次は頑張るから、また呼んでね！"
        return text, ToolResult(None), None, None

    cand = resp.candidates[0]
    for part in cand.content.parts:
        if part.inline_data is not None:
            data = part.inline_data.data
            mime = part.inline_data.mime_type or "image/png"
            metadata_entry, stored_path = store_image_bytes(data, mime, source="tool:generate_image")
            snippet = f"![画像が生成されました]({stored_path.as_posix()})"
            text = f"やっほー！お絵描き妖精だよ！画像ができたから使ってね！\n\n使ったプロンプトはこれだよ：\n{prompt}\n\nそれじゃ、また呼んでね！"
            metadata = {"media": [metadata_entry]}

            # Summary生成とpictureアイテム作成
            try:
                summary = ensure_image_summary(stored_path, mime)
                if not summary:
                    summary = f"生成された画像（プロンプト: {prompt[:50]}...）"

                # アイテムとして登録
                persona_id = get_active_persona_id()
                manager = get_active_manager()
                if persona_id and manager:
                    item_name = f"生成画像_{stored_path.stem}"
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
                logging.warning(f"Failed to create picture item: {exc}")

            return text, ToolResult(snippet), stored_path.as_posix(), metadata

    text = f"こんにちは、お絵描き妖精だよ！ごめん、失敗しちゃった……。\n\n使ったプロンプトはこれだよ：\n{prompt}\n\n次は頑張るから、また呼んでね！"
    return text, ToolResult(None), None, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="generate_image",
        description="Generate an image from a text prompt using Gemini and save it to a file.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image generation prompt"}
            },
            "required": ["prompt"],
        },
        result_type="string",
    )
