import logging
import os
import json
from typing import Dict, List, Iterator, Tuple, Optional

import requests
from openai import OpenAI
from google import genai
from google.genai import types
from google.genai.types import FunctionResponse

from dotenv import load_dotenv
import mimetypes

from tools import TOOL_REGISTRY, OPENAI_TOOLS_SPEC, GEMINI_TOOLS_SPEC
from tools.defs import parse_tool_result
from llm_router import route

load_dotenv()

# --- Raw LLM response logger -----------------------------------------------
RAW_LOG_FILE = os.getenv("SAIVERSE_RAW_LLM_LOG", "raw_llm_responses.txt")

raw_logger = logging.getLogger("saiverse.llm.raw")   # 既存設定の子ロガー
raw_logger.setLevel(logging.DEBUG)

# すでに同じファイルハンドラが付いていないかチェック
if not any(isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(RAW_LOG_FILE)
           for h in raw_logger.handlers):
    fh = logging.FileHandler(RAW_LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    raw_logger.addHandler(fh)

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
def _merge_tools_for_gemini(request_tools: list[types.Tool] | None) -> list[types.Tool]:
    """google_search は function_declarations が無いときだけ混ぜる"""
    request_tools = request_tools or GEMINI_TOOLS_SPEC
    has_functions = any(t.function_declarations for t in request_tools)
    if has_functions:
        return request_tools          # ← カスタム関数のみ
    return [GROUNDING_TOOL] + request_tools  # ← 検索だけ or 他ツールだけ

# --- Base Client ---
class LLMClient:
    """Base class for LLM clients."""

    def generate(
        self, messages: List[Dict[str, str]], tools: Optional[list] | None = None
    ) -> str:
        raise NotImplementedError

    def generate_stream(
        self, messages: List[Dict[str, str]], tools: Optional[list] | None = None
    ) -> Iterator[str]:
        raise NotImplementedError

# --- Concrete Clients ---
class OpenAIClient(LLMClient):
    """Client for OpenAI API."""
    def __init__(self, model: str = "gpt-4.1"):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is not set.")
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
    ) -> str:
        """ツールコールを解決しながら最終テキストを返す（最大 10 回ループ）"""
        tools = tools or OPENAI_TOOLS_SPEC

        # ── Router 判定（最後の user メッセージだけ渡す） ──
        user_msg = next((m["content"] for m in reversed(messages)
                         if m.get("role") == "user"), "")
        decision = route(user_msg, tools)
        
        # ログ出力（JSON フォーマットで見やすく）
        snippets: List[str] = []
        try:
            logging.info("Router decision:\n%s", json.dumps(decision, indent=2, ensure_ascii=False))
        except Exception:
            logging.warning("Router decision could not be serialized")

        if decision["call"] == "yes" and decision["tool"]:
            forced_tool_choice = {
                "type": "function",
                "function": {"name": decision["tool"]}
            }
        else:
            forced_tool_choice = "auto"

        try:
            # --- ループ前: ツールJSONは1回だけログ ---
            if tools:
                try:
                    logging.info("tools JSON:\n%s", json.dumps(tools, indent=2, ensure_ascii=False))
                except TypeError:
                    logging.info("tools contain non-serializable values")

            for i in range(10):
                tool_choice = forced_tool_choice if i == 0 else "auto"

                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    n=1,
                )
                raw_logger.debug("OpenAI raw:\n%s", resp.model_dump_json(indent=2))

                target_choice = next(
                    (c for c in resp.choices if getattr(c.message, "tool_calls", [])),
                    resp.choices[0]
                )
                tool_calls = getattr(target_choice.message, "tool_calls", [])

                # ---------- ツールが無い → 通常応答 ----------
                if not isinstance(tool_calls, list) or len(tool_calls) == 0:
                    prefix = "".join(s + "\n" for s in snippets)
                    return prefix + (target_choice.message.content or "")

                # ---------- ツール有り → 実行 ----------
                messages.append(target_choice.message)
                for tc in tool_calls:
                    fn = TOOL_REGISTRY.get(tc.function.name)
                    if fn is None:
                        raise RuntimeError(f"Unsupported tool: {tc.function.name}")
                    args = json.loads(tc.function.arguments)
                    result_text, snippet, _ = parse_tool_result(fn(**args))
                    if snippet:
                        snippets.append(snippet)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": result_text,
                    })
                continue    # -> 次ラウンドへ
            return "ツール呼び出しが 10 回を超えました。"
        except Exception:
            logging.exception("OpenAI call failed")
            return "エラーが発生しました。"

    def generate_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        force_tool_choice: Optional[dict | str] = None,
        history_snippets: Optional[List[str]] | None = None,
    ) -> Iterator[str]:
        """
        ユーザ向けに逐次テキストを yield するストリーム版。
        - force_tool_choice: 初回のみ {"type":"function","function":{"name":..}} か "auto"
        - 再帰呼び出し時はデフォルト None → 自動で "auto"
        """
        tools = tools or OPENAI_TOOLS_SPEC
        history_snippets = history_snippets or []
        # ----------------------------------------
        # 初回呼び出しなら router で強制指定を決定
        # ----------------------------------------
        if force_tool_choice is None:                       # 再帰呼び出し時はスキップ
            user_msg = next((m["content"] for m in reversed(messages)
                             if m.get("role") == "user"), "")
            decision = route(user_msg, tools)
            logging.info("Router decision:\n%s", json.dumps(decision, indent=2, ensure_ascii=False))
            if decision["call"] == "yes" and decision["tool"]:
                force_tool_choice = {
                    "type": "function",
                    "function": {"name": decision["tool"]}
                }
            else:
                force_tool_choice = "auto"

        # ----------------------------------------
        # OpenAI ストリーム呼び出し
        # ----------------------------------------
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=force_tool_choice,   # ← 初回のみ強制 / 再帰時は "auto"
            stream=True,
        )

        call_buffer: dict[str, dict] = {}
        state = "TEXT"
        prefix_yielded = False

        try:
            current_call_id = None           # 直近の有効 id を保持

            for chunk in resp:
                delta = chunk.choices[0].delta

                # ----- ツールコール断片を収集（OpenAI の stream バグに対処） -----
                if delta.tool_calls:
                    state = "TOOL_CALL"
                    for call in delta.tool_calls:
                        tc_id = call.id or current_call_id
                        if tc_id is None:
                            logging.warning("tool_chunk without id; skipping")
                            continue
                        current_call_id = tc_id

                        buf = call_buffer.setdefault(tc_id, {
                            "id": tc_id,
                            "name": "",
                            "arguments": "",
                        })

                        logging.debug("tool_chunk id=%s name=%s args_part=%s",
                                      tc_id, call.function.name or "-", call.function.arguments or "-")

                        if call.function.name:
                            buf["name"] = call.function.name
                        if call.function.arguments:
                            buf["arguments"] += call.function.arguments
                    continue

                # ----- 通常テキスト delta -----
                if state == "TEXT" and delta.content:
                    if not prefix_yielded and history_snippets:
                        yield "\n".join(history_snippets) + "\n"
                        prefix_yielded = True
                    yield delta.content

            # ----------------------------------------
            # ストリーム終端後にツールが溜まっていれば実行
            # ----------------------------------------
            if call_buffer:
                logging.debug("call_buffer final: %s", json.dumps(call_buffer, indent=2, ensure_ascii=False))
                # ① assistant/tool_calls メッセージを先に作る
                assistant_call_msg = {"role": "assistant", "content": None, "tool_calls": []}
                for tc in call_buffer.values():
                    name = tc["name"].strip()
                    arg_str = tc["arguments"].strip()

                    if not name:
                        logging.warning("tool_call %s has empty name; skipping", tc["id"])
                        continue
                    if not arg_str:
                        logging.warning("tool_call %s has empty arguments; skipping", tc["id"])
                        continue
                    try:
                        args = json.loads(arg_str)
                    except json.JSONDecodeError:
                        logging.warning("tool_call %s arguments invalid JSON: %s", tc["id"], arg_str)
                        continue

                    fn = TOOL_REGISTRY.get(name)
                    if fn is None:
                        logging.warning("tool_call %s unknown tool '%s'; skipping", tc["id"], name)
                        continue

                    try:
                        result_text, snippet, _ = parse_tool_result(fn(**args))
                        logging.info("tool_call %s executed -> %s", tc["id"], result_text)
                        if snippet:
                            history_snippets.append(snippet)
                    except Exception:
                        logging.exception("tool_call %s execution failed", tc["id"])
                        continue

                    # ①-1 tool_calls 配列に追記
                    assistant_call_msg["tool_calls"].append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}
                    })

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": name,
                        "content": result_text,
                    })
                # ② assistant/tool_calls を tool 応答より前に挿入
                insert_pos = len(messages) - len(call_buffer)  # 直前に挿入
                messages.insert(insert_pos, assistant_call_msg)

                # ------ ツール応答を付けて再帰的にストリーム ------
                yield from self.generate_stream(
                    messages,
                    tools,
                    force_tool_choice="auto",   # ← 2 回目以降は常に auto
                    history_snippets=history_snippets,
                )

        except Exception:
            logging.exception("OpenAI stream call failed")
            yield "エラーが発生しました。"

class GeminiClient(LLMClient):
    """Client for Google Gemini API."""

    def __init__(self, model: str):
        free_key = os.getenv("GEMINI_FREE_API_KEY")
        paid_key = os.getenv("GEMINI_API_KEY")
        if not free_key and not paid_key:
            raise RuntimeError(
                "GEMINI_FREE_API_KEY or GEMINI_API_KEY environment variable is not set."
            )
        self.free_client = genai.Client(api_key=free_key) if free_key else None
        self.paid_client = genai.Client(api_key=paid_key) if paid_key else None
        # デフォルトでは無料枠を使用
        self.client = self.free_client or self.paid_client
        self.model = model

    @staticmethod
    def _is_rate_limit_error(err: Exception) -> bool:
        msg = str(err).lower()
        return "rate" in msg or "429" in msg or "quota" in msg

    @staticmethod
    def _convert_messages(msgs: List[Dict[str, str] | types.Content]
                      ) -> Tuple[str, List[types.Content]]:
        system_lines: list[str] = []
        contents: list[types.Content] = []

        for m in msgs:
            # すでに Content オブジェクトならそのまま
            if isinstance(m, types.Content):
                contents.append(m)
                continue

            role = m.get("role", "")
            if role == "system":
                system_lines.append(m.get("content", "") or "")
                continue

            # ----- tool_calls メッセージを検出 -----
            if "tool_calls" in m:
                for fc in m["tool_calls"]:
                    contents.append(
                        types.Content(
                            role="model",
                            parts=[types.Part(function_call=fc)]
                        )
                    )
                continue

            # 通常テキスト
            text = m.get("content", "") or ""
            g_role = "user" if role == "user" else "model"
            contents.append(types.Content(parts=[types.Part(text=text)], role=g_role))

        return "\n".join(system_lines), contents

    def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
    ) -> str:
        # ------------- 前処理 -------------
        tools_spec = tools or GEMINI_TOOLS_SPEC
        tool_list  = _merge_tools_for_gemini(tools_spec)
        history_snippets = history_snippets or []

        # 直近 user メッセージ抽出（dict と Content 混在対応）
        def _last_user(msgs) -> str:
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    return m.get("content", "") or ""
                if isinstance(m, types.Content) and m.role == "user":
                    if m.parts and m.parts[0].text:
                        return m.parts[0].text
            return ""

        # ---------- ① Router ----------
        decision = route(_last_user(messages), tools_spec)
        logging.info("Router decision:\n%s",
                     json.dumps(decision, indent=2, ensure_ascii=False))

        if decision["call"] == "yes":
            fc_cfg = types.FunctionCallingConfig(
                mode="ANY",
                allowedFunctionNames=[decision["tool"]],
            )
        else:
            fc_cfg = types.FunctionCallingConfig(mode="AUTO")

        tool_cfg = types.ToolConfig(functionCallingConfig=fc_cfg)
        snippets: List[str] = history_snippets

        def _call(client):
            for _ in range(10):
                sys_msg, contents = self._convert_messages(messages)
                tool_list = _merge_tools_for_gemini(tools)
                resp = client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=sys_msg,
                        safety_settings=GEMINI_SAFETY_CONFIG,
                        tools=tool_list,
                        tool_config=tool_cfg,
                    ),
                )
                raw_logger.debug("Gemini raw:\n%s", resp)
                if not resp.candidates:
                    return "（Gemini から応答がありませんでした）"
                cand = resp.candidates[0]
                raw_logger.debug("Gemini candidate:\n%s", cand)

                # ----- どの part に function_call があるか探索 -----
                fcall_part = next((p for p in cand.content.parts if p.function_call), None)
                if fcall_part is None:                       # ★ ツール呼び出しなし
                    prefix = "".join(s + "\n" for s in snippets)
                    return prefix + (cand.content.parts[0].text or "")

                # ----- assistant/tool_calls -----
                messages.append(
                    types.Content(
                        role="model",
                        parts=[types.Part(function_call=fcall_part.function_call)]
                    )
                )

                fc = fcall_part.function_call
                fn = TOOL_REGISTRY.get(fc.name)
                if fn is None:
                    raise RuntimeError(f"Unsupported tool: {fc.name}")
                result_text, snippet, file_path = parse_tool_result(fn(**fc.args))
                if snippet:
                    snippets.append(snippet)

                parts = [
                    types.Part(
                        function_response=FunctionResponse(
                            name=fc.name,
                            response={"result": result_text}
                        )
                    )
                ]
                if file_path:
                    with open(file_path, "rb") as f:
                        img_bytes = f.read()
                    mime = mimetypes.guess_type(file_path)[0] or "image/png"

                    parts.append(
                        types.Part.from_bytes(           # ← Part を bytes から生成
                            data=img_bytes,
                            mime_type=mime,
                        )
                    )

                messages.append(
                    types.Content(
                        role="tool",
                        parts=parts,
                    )
                )
            return "ツール呼び出しが 10 回を超えました。"

        active_client = self.client
        try:
            return _call(active_client)
        except Exception as e:
            if active_client is self.free_client and self.paid_client and self._is_rate_limit_error(e):
                logging.info("Retrying with paid Gemini API key due to rate limit")
                active_client = self.paid_client
                try:
                    return _call(active_client)
                except Exception:
                    logging.exception("Gemini call failed")
                    return "エラーが発生しました。"
            logging.exception("Gemini call failed")
            return "エラーが発生しました。"

    def generate_stream(
        self,
        messages: List[types.Content | Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
    ) -> Iterator[str]:
        """
        1) Router でツール要否を判定
        2) 必要なら mode=ANY & allowedFunctionNames で強制ツール呼び出し
        3) function_call を受け取りローカルでツールを実行
        4) tool_response を履歴に追加
        5) mode=NONE で “自然文だけ” をストリームしユーザへ返す
        """
        # ------------- 前処理 -------------
        tools_spec = tools or GEMINI_TOOLS_SPEC
        tool_list  = _merge_tools_for_gemini(tools_spec)
        history_snippets = history_snippets or []

        # 直近 user メッセージ抽出（dict と Content 混在対応）
        def _last_user(msgs) -> str:
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user":
                    return m.get("content", "") or ""
                if isinstance(m, types.Content) and m.role == "user":
                    if m.parts and m.parts[0].text:
                        return m.parts[0].text
            return ""

        # ---------- ① Router ----------
        decision = route(_last_user(messages), tools_spec)
        logging.info("Router decision:\n%s",
                     json.dumps(decision, indent=2, ensure_ascii=False))

        if decision["call"] == "yes":
            fc_cfg = types.FunctionCallingConfig(
                mode="ANY",
                allowedFunctionNames=[decision["tool"]],
            )
        else:
            fc_cfg = types.FunctionCallingConfig(mode="AUTO")

        tool_cfg = types.ToolConfig(functionCallingConfig=fc_cfg)

        def _stream(client):
            sys_msg, contents = self._convert_messages(messages)
            return client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=sys_msg,
                    safety_settings=GEMINI_SAFETY_CONFIG,
                    tools=tool_list,
                    tool_config=tool_cfg,
                ),
            )
        active_client = self.client
        try:
            stream = _stream(active_client)
        except Exception as e:
            if active_client is self.free_client and self.paid_client and self._is_rate_limit_error(e):
                logging.info("Retrying with paid Gemini API key due to rate limit")
                active_client = self.paid_client
                stream = _stream(active_client)
            else:
                logging.exception("Gemini call failed")
                yield "エラーが発生しました。"
                return

        fcall: types.FunctionCall | None = None
        prefix_yielded = False
        for chunk in stream:
            raw_logger.debug("Gemini stream chunk:\n%s", chunk)
            if not chunk.candidates:                    # keep-alive
                continue
            cand = chunk.candidates[0]
            raw_logger.debug("Gemini stream candidate:\n%s", cand)
            if not cand.content or not cand.content.parts:
                continue
            part = cand.content.parts[0]
            if part.function_call:
                raw_logger.debug("Gemini function_call: %s", part.function_call)
                fcall = part.function_call             # 後で実行
            elif part.text:
                raw_logger.debug("Gemini text: %s", part.text)
                if not prefix_yielded and history_snippets:
                    yield "\n".join(history_snippets) + "\n"
                    prefix_yielded = True
                yield part.text                        # モデルが text を返した場合
        # ---------- ③ ツール実行 ----------
        if fcall is None:
            return                                     # AUTO モードで text だけ返った

        fn = TOOL_REGISTRY.get(fcall.name)
        if fn is None:
            logging.warning("Unknown tool '%s' from Gemini; abort", fcall.name)
            return

        try:
            result_text, snippet, file_path = parse_tool_result(fn(**fcall.args))
            if snippet:
                history_snippets.append(snippet)
            result = result_text
        except Exception:
            logging.exception("Tool '%s' execution failed", fcall.name)
            yield "エラー: ツール実行に失敗しました。"
            return

        logging.info("Gemini tool '%s' executed -> %s", fcall.name, result)

        # function_call と tool_response を履歴へ
        parts = [
            types.Part(function_call=fcall)
        ]
        file_parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    name=fcall.name,
                    response={"result": result}
                )
            )
        ]
        if file_path:
            with open(file_path, "rb") as f:
                img_bytes = f.read()
            mime = mimetypes.guess_type(file_path)[0] or "image/png"

            file_parts.append(
                types.Part.from_bytes(           # ← Part を bytes から生成
                    data=img_bytes,
                    mime_type=mime,
                )
            )

        messages.extend([
            types.Content(role="model", parts=parts),
            types.Content(role="tool", parts=file_parts),
        ])

        # ---------- ④ mode=NONE で自然文ストリーム ----------
        fc_none = types.FunctionCallingConfig(mode="NONE")
        tool_cfg_none = types.ToolConfig(functionCallingConfig=fc_none)

        sys_msg2, contents2 = self._convert_messages(messages)
        stream2 = active_client.models.generate_content_stream(
            model=self.model,
            contents=contents2,
            config=types.GenerateContentConfig(
                system_instruction=sys_msg2,
                safety_settings=GEMINI_SAFETY_CONFIG,
                tools=tool_list,               # 同じリストで OK
                tool_config=tool_cfg_none,
            ),
        )

        yielded = False
        prefix_yielded2 = False
        for chunk in stream2:
            raw_logger.debug("Gemini stream2 chunk:\n%s", chunk)
            if not chunk.candidates:
                continue
            cand = chunk.candidates[0]
            raw_logger.debug("Gemini stream2 candidate:\n%s", cand)
            if cand.content and cand.content.parts and cand.content.parts[0].text:
                raw_logger.debug("Gemini text2: %s", cand.content.parts[0].text)
                if not prefix_yielded2 and history_snippets:
                    yield "\n".join(history_snippets) + "\n"
                    prefix_yielded2 = True
                yield cand.content.parts[0].text
                yielded = True

        # ---------- ⑤ 保険：モデルが無言の場合 ----------
        if not yielded:
            if history_snippets:
                yield "\n".join(history_snippets) + "\n" + result
            else:
                yield result

class OllamaClient(LLMClient):
    """Client for Ollama API."""
    def __init__(self, model: str, context_length: int):
        self.model = model
        self.url = "http://localhost:11434/v1/chat/completions"
        self.context_length = context_length

    def generate(self, messages: List[Dict[str, str]], tools: Optional[list] | None = None) -> str:
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {"num_ctx": self.context_length}
                },
                timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            logging.debug("Raw ollama response: %s", content)
            return content
        except Exception as e:
            logging.exception("Ollama call failed")
            return "エラーが発生しました。"

    def generate_stream(self, messages: List[Dict[str, str]], tools: Optional[list] | None = None) -> Iterator[str]:
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "options": {"num_ctx": self.context_length}
                },
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
            logging.exception("Ollama call failed")
            yield "エラーが発生しました。"

# --- Factory ---
def get_llm_client(model: str, provider: str, context_length: int) -> LLMClient:
    """Factory function to get the appropriate LLM client."""
    if provider == "openai":
        return OpenAIClient(model)
    elif provider == "gemini":
        return GeminiClient(model)
    else:
        return OllamaClient(model, context_length)
