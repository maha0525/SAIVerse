import json
import logging
import re
from pathlib import Path
from typing import Dict, Optional

from google import genai
from llm_clients.gemini_utils import build_gemini_clients
from google.genai import types


class EmotionControlModule:
    """Lightweight module to adjust emotion parameters using Gemini."""

    def __init__(self, prompt_path: Path = None, model: str = "gemini-2.5-flash-lite-preview-09-2025") -> None:
        if prompt_path is None:
            from saiverse.data_paths import find_file, PROMPTS_DIR
            prompt_path = find_file(PROMPTS_DIR, "emotion_control.txt") or Path("system_prompts/emotion_control.txt")
        self.prompt_template = prompt_path.read_text(encoding="utf-8")
        self.model = model
        self.free_client, self.paid_client, self.client = build_gemini_clients()

    def evaluate(
        self,
        user_message: str,
        assistant_message: str,
        current_emotion: Optional[Dict[str, Dict[str, float]]] = None,
    ):
        """Return emotion delta dict based on the latest interaction."""
        emotion_vals = current_emotion or {}
        prompt = self.prompt_template.format(
            user_message=user_message,
            assistant_message=assistant_message,
            stability_mean=emotion_vals.get("stability", {}).get("mean", 0),
            stability_var=emotion_vals.get("stability", {}).get("variance", 0),
            affect_mean=emotion_vals.get("affect", {}).get("mean", 0),
            affect_var=emotion_vals.get("affect", {}).get("variance", 0),
            resonance_mean=emotion_vals.get("resonance", {}).get("mean", 0),
            resonance_var=emotion_vals.get("resonance", {}).get("variance", 0),
            attitude_mean=emotion_vals.get("attitude", {}).get("mean", 0),
            attitude_var=emotion_vals.get("attitude", {}).get("variance", 0),
        )

        def _call(client):
            return client.models.generate_content(
                model=self.model,
                contents=[types.Content(parts=[types.Part(text=prompt)], role="user")],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )

        active_client = self.client
        try:
            resp = _call(active_client)
        except Exception as e:
            if active_client is self.free_client and self.paid_client and "rate" in str(e).lower():
                logging.info("Retrying emotion module with paid Gemini API key due to rate limit")
                active_client = self.paid_client
                try:
                    resp = _call(active_client)
                except Exception as e2:
                    logging.error("Emotion control module failed: %s", e2)
                    return None
            else:
                logging.error("Emotion control module failed: %s", e)
                return None

        content = resp.text.strip()
        logging.info("Emotion control module response:\n%s", content)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise
