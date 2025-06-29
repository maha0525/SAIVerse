import logging
import os
from typing import Dict, List, Iterator, Tuple

import requests
from openai import OpenAI
from google import genai
from google.genai import types

# --- Constants ---
GEMINI_SAFETY_CONFIG = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
]

GROUNDING_TOOL = types.Tool(google_search=types.GoogleSearch())

# --- Base Client ---
class LLMClient:
    """Base class for LLM clients."""
    def generate(self, messages: List[Dict[str, str]]) -> str:
        raise NotImplementedError

    def generate_stream(self, messages: List[Dict[str, str]]) -> Iterator[str]:
        raise NotImplementedError

# --- Concrete Clients ---
class OpenAIClient(LLMClient):
    """Client for OpenAI API."""
    def __init__(self, model: str = "gpt-4o"):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is not set.")
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def generate(self, messages: List[Dict[str, str]]) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
            )
            content = resp.choices[0].message.content
            logging.debug("Raw openai response: %s", content)
            return content or ""
        except Exception as e:
            logging.error("OpenAI call failed: %s", e)
            return "エラーが発生しました。"

    def generate_stream(self, messages: List[Dict[str, str]]) -> Iterator[str]:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
            )
            for chunk in resp:
                yield chunk.choices[0].delta.content or ""
        except Exception as e:
            logging.error("OpenAI call failed: %s", e)
            yield "エラーが発生しました。"

class GeminiClient(LLMClient):
    """Client for Google Gemini API."""
    def __init__(self, model: str):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
        self.client = genai.Client(api_key=api_key)
        self.model = model

    @staticmethod
    def _convert_messages(msgs: List[Dict[str, str]]) -> Tuple[str, List[types.Content]]:
        system_instruction_lines: List[str] = []
        contents: List[types.Content] = []
        for m in msgs:
            role = m.get("role", "")
            text = m.get("content", "")
            if role == "system":
                system_instruction_lines.append(text)
            else:
                g_role = "user" if role == "user" else "model"
                contents.append(types.Content(parts=[types.Part(text=text)], role=g_role))
        return "\n".join(system_instruction_lines), contents

    def generate(self, messages: List[Dict[str, str]]) -> str:
        try:
            system_instruction, contents = self._convert_messages(messages)
            resp = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    safety_settings=GEMINI_SAFETY_CONFIG,
                    tools=[GROUNDING_TOOL]
                ),
            )
            logging.debug("Raw gemini response: %s", resp.text)
            return resp.text
        except Exception as e:
            logging.error("Gemini call failed: %s", e)
            return "エラーが発生しました。"

    def generate_stream(self, messages: List[Dict[str, str]]) -> Iterator[str]:
        try:
            system_instruction, contents = self._convert_messages(messages)
            resp = self.client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    safety_settings=GEMINI_SAFETY_CONFIG,
                    tools=[GROUNDING_TOOL]
                ),
            )
            for chunk in resp:
                yield chunk.text or ""
        except Exception as e:
            logging.error("Gemini call failed: %s", e)
            yield "エラーが発生しました。"

class OllamaClient(LLMClient):
    """Client for Ollama API."""
    def __init__(self, model: str):
        self.model = model
        self.url = "http://localhost:11434/v1/chat/completions"

    def generate(self, messages: List[Dict[str, str]]) -> str:
        try:
            resp = requests.post(
                self.url,
                json={"model": self.model, "messages": messages, "stream": False},
                timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logging.debug("Raw ollama response: %s", content)
            return content
        except Exception as e:
            logging.error("Ollama call failed: %s", e)
            return "エラーが発生しました。"

    def generate_stream(self, messages: List[Dict[str, str]]) -> Iterator[str]:
        try:
            resp = requests.post(
                self.url,
                json={"model": self.model, "messages": messages, "stream": True},
                timeout=300,
                stream=True,
            )
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                chunk = line.decode("utf-8")
                if chunk.startswith("data: "):
                    chunk = chunk[len("data: ") :]
                if chunk.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                    delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    yield delta
                except json.JSONDecodeError:
                    logging.warning("Failed to parse stream chunk: %s", chunk)
        except Exception as e:
            logging.error("Ollama call failed: %s", e)
            yield "エラーが発生しました。"

# --- Factory ---
def get_llm_client(model: str) -> LLMClient:
    """Factory function to get the appropriate LLM client."""
    if model == "gpt-4o":
        return OpenAIClient(model)
    elif model.startswith("gemini"):
        return GeminiClient(model)
    else:
        return OllamaClient(model)
