import json, logging
import os
from typing import Any, Dict, List

from dotenv import load_dotenv

from google import genai
from google.genai import types as gtypes
from llm_clients.gemini_utils import build_gemini_clients
from tools.core import ToolSchema

load_dotenv()

log = logging.getLogger("saiverse.router")

ROUTER_MODEL = "gemini-2.5-flash-lite-preview-09-2025"
_free_client, _paid_client, client = build_gemini_clients()
_CLIENT_LABELS = {
    id(_free_client): "free",
    id(_paid_client): "paid",
}


def rebuild_clients():
    """Rebuild Gemini clients from current os.environ.

    Called after API keys are updated at runtime (e.g. via tutorial or admin UI)
    so that the router uses the latest credentials.
    """
    global _free_client, _paid_client, client, _CLIENT_LABELS
    _free_client, _paid_client, client = build_gemini_clients()
    _CLIENT_LABELS = {
        id(_free_client): "free",
        id(_paid_client): "paid",
    }
    log.info("Router Gemini clients rebuilt with updated API keys")


def _is_rate_limit_error(err: Exception) -> bool:
    msg = str(err).lower()
    for keyword in ("rate", "quota", "429", "503", "unavailable", "overload"):
        if keyword in msg:
            return True
    return False

SYS_TEMPLATE = """\
You are a tool-router.
Return ONLY valid JSON with keys: "call", "tool", "args".

TOOLS:
{tools_block}

RULES:
 - "call" must be either "yes" or "no". Do NOT use other values.
 - Pick the tool whose name or description best matches the user message.
 - Arguments must use the parameter names from that tool's schema.
 - If no tool fits or you are uncertain, respond with call:"no" and an empty "tool".
"""

GEMINI_SAFETY_CONFIG = [
    gtypes.SafetySetting(
        category=gtypes.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=gtypes.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
    gtypes.SafetySetting(
        category=gtypes.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=gtypes.HarmBlockThreshold.BLOCK_NONE,
    ),
    gtypes.SafetySetting(
        category=gtypes.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=gtypes.HarmBlockThreshold.BLOCK_NONE,
    ),
    gtypes.SafetySetting(
        category=gtypes.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=gtypes.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
]

def build_tools_block(tools_spec: list) -> str:
    """
    tools_spec が
      • OpenAI 形式: {"type":"function","function":{name,description,…}}
      • Gemini 形式: google.genai.types.Tool
    の両方を受け取れるようにする
    """
    lines = []
    for t in tools_spec:
        # ---- OpenAI 形式 dict ----
        if isinstance(t, dict):
            fn = t.get("function", {})
            props = fn.get("parameters", {}).get("properties", {})
            arglist = ", ".join(props.keys())
            lines.append(f"- {fn.get('name','?')} : {fn.get('description','')} (args: {arglist})")
            continue

        # ---- Gemini 形式 Tool ----
        if isinstance(t, gtypes.Tool) and t.function_declarations:
            for decl in t.function_declarations:
                if hasattr(decl, "parameters") and decl.parameters is not None:
                    props = getattr(decl.parameters, "properties", {})
                else:
                    props = {}
                arglist = ", ".join(props.keys())
                lines.append(f"- {decl.name} : {decl.description} (args: {arglist})")
            continue

        # ---- ToolSchema ----
        if isinstance(t, ToolSchema):
            arglist = ", ".join(t.parameters.get("properties", {}).keys())
            lines.append(f"- {t.name} : {t.description} (args: {arglist})")
            continue

        # その他は無視
    return "\n".join(lines)

def extract_tool_names(tools_spec: list) -> set[str]:
    names: set[str] = set()
    for t in tools_spec:
        if isinstance(t, dict):
            name = t.get("function", {}).get("name")
            if name:
                names.add(name)
            continue
        if isinstance(t, gtypes.Tool) and t.function_declarations:
            for decl in t.function_declarations:
                names.add(decl.name)
            continue
        if isinstance(t, ToolSchema):
            names.add(t.name)
    return names

def route(user_message: str, tools_spec: List[Any]) -> Dict[str, Any]:
    """Return {"call":"yes/no","tool": name, "args": {...}}"""
    sys_prompt = SYS_TEMPLATE.format(
        tools_block=build_tools_block(tools_spec),
    )

    def _client_label(target_client) -> str:
        if target_client is None:
            return "unknown"
        return _CLIENT_LABELS.get(id(target_client), "unknown")

    def _invoke(target_client):
        log.debug(
            "Router Gemini call start", extra={"client": _client_label(target_client)}
        )
        return target_client.models.generate_content(
            model=ROUTER_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part(text=user_message)])],
            config=gtypes.GenerateContentConfig(
                system_instruction=sys_prompt,
                safety_settings=GEMINI_SAFETY_CONFIG,
                response_mime_type="application/json",
                temperature=0,
            ),
        )

    global client
    active_client = client
    try:
        resp = _invoke(active_client)
    except Exception as exc:
        if active_client is _free_client and _paid_client is not None and _is_rate_limit_error(exc):
            log.info(
                "Router retry due to rate limit",
                extra={
                    "client": _client_label(active_client),
                    "retry_client": _client_label(_paid_client),
                    "error": str(exc),
                },
            )
            active_client = _paid_client
            try:
                resp = _invoke(active_client)
            except Exception as exc_paid:
                log.error(
                    "Router Gemini retry failed",
                    extra={
                        "client": _client_label(active_client),
                        "error": str(exc_paid),
                    },
                )
                raise
        else:
            log.error(
                "Router Gemini call failed",
                extra={"client": _client_label(active_client), "error": str(exc)},
            )
            raise

    if active_client is not client:
        log.info(
            "Router switched active Gemini client",
            extra={
                "previous": _client_label(client),
                "current": _client_label(active_client),
            },
        )
        client = active_client
    try:
        cand = resp.candidates[0]
        text = getattr(cand, "text", None)
        if not text and getattr(cand, "content", None) and cand.content.parts:
            text = cand.content.parts[0].text
        decision = json.loads(text)
        if not isinstance(decision, dict):
            raise ValueError

        tool_names = extract_tool_names(tools_spec)
        call = str(decision.get("call", "")).strip().lower()
        tool = str(decision.get("tool", "")).strip()
        if call not in {"yes", "no"}:
            call = "yes" if tool in tool_names else "no"
        return {"call": call, "tool": tool, "args": decision.get("args", {})}
    except Exception:
        raw = text if 'text' in locals() else str(resp)
        log.warning("Router JSON parse failed, fallback to auto. Raw: %s", raw)
        return {"call": "no", "tool": "", "args": {}}
