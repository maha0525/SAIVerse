import json
import logging
import os
from pathlib import Path

from google import genai
from google.genai import types


class EmotionControlModule:
    """Lightweight module to adjust emotion parameters using Gemini."""

    def __init__(self, prompt_path: Path = Path("system_prompts/emotion_control.txt"), model: str = "gemini-2.0-flash") -> None:
        self.prompt_template = prompt_path.read_text(encoding="utf-8")
        self.model = model
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY environment variable is not set. "
                "Please set it to your Gemini API key."
            )
        self.client = genai.Client(api_key=api_key)

    def evaluate(self, user_message: str, assistant_message: str):
        """Return emotion delta dict based on the latest interaction."""
        prompt = self.prompt_template.format(
            user_message=user_message, assistant_message=assistant_message
        )
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=[types.Content(parts=[types.Part(text=prompt)], role="user")],
            )
            content = resp.text.strip()
            logging.info("Emotion control module response:\n%s", content)
            return json.loads(content)
        except Exception as e:
            logging.error("Emotion control module failed: %s", e)
            return None
