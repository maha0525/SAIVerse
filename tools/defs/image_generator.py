import os
from datetime import datetime
from pathlib import Path
from google import genai
from google.genai import types
from dotenv import load_dotenv

from tools.defs import ToolSchema, ToolResult

load_dotenv()


def generate_image(prompt: str) -> tuple[str, ToolResult, str | None]:
    """Generate an image and return (prompt, snippet, file path)."""
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
        return prompt, ToolResult(None), None
    cand = resp.candidates[0]
    for part in cand.content.parts:
        if part.inline_data is not None:
            data = part.inline_data.data
            mime = part.inline_data.mime_type or "image/png"
            img_dir = Path("generate_image")
            img_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            ext = mime.split("/")[-1]
            file_path = img_dir / f"{timestamp}.{ext}"
            file_path.write_bytes(data)
            snippet = f"![画像が生成されました]({file_path.as_posix()})"
            text = f"やっほー！お絵描き妖精だよ！画像ができたから使ってね！\n\n使ったプロンプトはこれだよ：\n{prompt}\n\nそれじゃ、また呼んでね！"
            return text, ToolResult(snippet), file_path.as_posix()
    text = "こんにちは、お絵描き妖精だよ！ごめん、失敗しちゃった……。\n\n使ったプロンプトはこれだよ：\n{prompt}\n\n次は頑張るから、また呼んでね！"
    return text, ToolResult(None), None


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
