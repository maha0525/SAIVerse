import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

from tools.defs import ToolSchema, ToolResult
from media_utils import store_image_bytes

load_dotenv()


def generate_image(prompt: str) -> tuple[str, ToolResult, str | None, dict | None]:
    """Generate an image and return (prompt, snippet, file path, metadata)."""
    free_key = os.getenv("GEMINI_FREE_API_KEY")
    paid_key = os.getenv("GEMINI_API_KEY")
    if not free_key and not paid_key:
        raise RuntimeError(
            "GEMINI_FREE_API_KEY or GEMINI_API_KEY environment variable is not set."
        )
    client = genai.Client(api_key=free_key or paid_key)
    resp = client.models.generate_content(
        model="gemini-2.0-flash-preview-image-generation",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"]  # ← TEXT を必ず含める
        )
    )
    if not resp.candidates:
        return prompt, ToolResult(None), None, None
    cand = resp.candidates[0]
    for part in cand.content.parts:
        if part.inline_data is not None:
            data = part.inline_data.data
            mime = part.inline_data.mime_type or "image/png"
            metadata_entry, stored_path = store_image_bytes(data, mime, source="tool:generate_image")
            snippet = f"![画像が生成されました]({stored_path.as_posix()})"
            text = f"やっほー！お絵描き妖精だよ！画像ができたから使ってね！\n\n使ったプロンプトはこれだよ：\n{prompt}\n\nそれじゃ、また呼んでね！"
            metadata = {"media": [metadata_entry]}
            return text, ToolResult(snippet), stored_path.as_posix(), metadata
    text = "こんにちは、お絵描き妖精だよ！ごめん、失敗しちゃった……。\n\n使ったプロンプトはこれだよ：\n{prompt}\n\n次は頑張るから、また呼んでね！"
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
