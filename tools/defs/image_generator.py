import base64
from google import genai
from google.genai import types

from tools.defs import ToolSchema


def generate_image(prompt: str) -> str:
    """Generate an image from text prompt and return a data URI."""
    client = genai.Client()
    resp = client.models.generate_content(
        model="gemini-2.0-flash-preview-image-generation",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"]
        )
    )
    if not resp.candidates:
        return ""
    cand = resp.candidates[0]
    for part in cand.content.parts:
        if part.inline_data is not None:
            data = part.inline_data.data
            mime = part.inline_data.mime_type or "image/png"
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:{mime};base64,{b64}"
    return ""


def schema() -> ToolSchema:
    return ToolSchema(
        name="generate_image",
        description="Generate an image from a text prompt using Gemini and return it as a data URI.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image generation prompt"}
            },
            "required": ["prompt"],
        },
        result_type="string",
    )
