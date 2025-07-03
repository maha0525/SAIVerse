import json
import logging
import os
from pathlib import Path

import re
from google import genai
from google.genai import types
from typing import Dict, Optional


class EmotionControlModule:
    """Lightweight module to adjust emotion parameters using Gemini."""

    def __init__(self, prompt_path: Path = Path("system_prompts/emotion_control.txt"), model: str = "gemini-2.0-flash") -> None:
        self.prompt_template = prompt_path.read_text(encoding="utf-8")
        self.model = model
        free_key = os.getenv("GEMINI_FREE_API_KEY")
        paid_key = os.getenv("GEMINI_API_KEY")
        if not free_key and not paid_key:
            raise RuntimeError(
                "GEMINI_FREE_API_KEY or GEMINI_API_KEY environment variable is not set."
            )
        self.free_client = genai.Client(api_key=free_key) if free_key else None
        self.paid_client = genai.Client(api_key=paid_key) if paid_key else None
        self.client = self.free_client or self.paid_client

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
