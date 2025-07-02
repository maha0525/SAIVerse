import json, logging
from typing import Dict, Any, List
from openai import OpenAI
from google.genai import types as gtypes
from dotenv import load_dotenv
import os

load_dotenv()

log = logging.getLogger("saiverse.router")

ROUTER_MODEL = "gpt-4.1-nano"
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise RuntimeError("OPENAI_API_KEY environment variable is not set.")
client = OpenAI(api_key=api_key)

SYS_TEMPLATE = """\
You are a tool-router.
Return ONLY valid JSON with keys: "call", "tool", "args".

TOOLS:
{tools_block}

RULES:
- If the user message is an arithmetic expression -> call:"yes", tool:"{default_tool}".
- Otherwise -> call:"no".
"""

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
            lines.append(f"- {fn.get('name','?')} : {fn.get('description','')}")
            continue

        # ---- Gemini 形式 Tool ----
        if isinstance(t, gtypes.Tool) and t.function_declarations:
            for decl in t.function_declarations:
                lines.append(f"- {decl.name} : {decl.description}")
            continue

        # その他は無視
    return "\n".join(lines)

def route(user_message: str,
          tools_spec: List[Dict[str, Any]],
          default_tool: str) -> Dict[str, Any]:
    """Return {"call":"yes/no","tool":name,"args":{...}}"""
    sys_prompt = SYS_TEMPLATE.format(
        tools_block=build_tools_block(tools_spec),
        default_tool=default_tool,
    )

    resp = client.chat.completions.create(
        model=ROUTER_MODEL,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    try:
        decision = json.loads(resp.choices[0].message.content)
        if not isinstance(decision, dict):
            raise ValueError
        return decision
    except Exception:
        log.warning("Router JSON parse failed, fallback to auto. Raw: %s",
                    resp.choices[0].message.content)
        return {"call": "no", "tool": "", "args": {}}
