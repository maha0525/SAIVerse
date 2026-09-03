"""Arasuji (Chronicle) 一次あらすじ生成の共有部品。

W4 (体験の構造 工程(2)) で旧 20 件固定バッチ経路 (ArasujiGenerator /
maybe_consolidate / gap-fill / dismantle) は撤去された — 生成経路の後継は
sai_memory/arasuji/alignment.py (整列計画) + executor.py (チャンク実行) +
bands.py (列のあふれ束ね)。

本モジュールに残るのは:

- :func:`generate_level1_arasuji` — 単一エントリの再生成
  (UI の regenerate → scripts/arasuji/build_arasuji_core.regenerate_entry_from_messages)
  が使う一次あらすじ生成の一回分。
- プロンプト整形・usage 記録のユーティリティ (_format_* / _record_llm_usage)
  — executor / bands / note_extractor / note_organizer と共有。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence

from llm_clients.exceptions import EmptyResponseError
from sai_memory.memory.storage import MECHANISM_TAGS, Message
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    create_entry,
    get_leaf_entries_by_level,
    get_max_level,
)

LOGGER = logging.getLogger(__name__)


def _record_llm_usage(client, persona_id: Optional[str], node_type: str) -> None:
    """Record LLM usage from the client to usage tracker."""
    try:
        usage = client.consume_usage()
        if usage:
            from saiverse.usage_tracker import get_usage_tracker
            get_usage_tracker().record_usage(
                model_id=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_ttl=usage.cache_ttl,
                persona_id=persona_id,
                node_type=node_type,
                category="memory_weave_generate",
            )
    except Exception as e:
        LOGGER.warning(f"Failed to record chronicle usage: {e}")


# ---------------------------------------------------------------------------
# 空応答の再試行 (2026-09-03 まはー裁定)
#
# 推論モデル (reasoning_content を持つ llama.cpp / OpenAI 互換の端点) は、
# 出力を全部 reasoning_content に書いて content を空のまま finish_reason=stop で
# 閉じることが確率的にある。クライアントはこれを EmptyResponseError にする。
# 純生成 (チャンク / 束ね / 吸収) の LLM 呼び出しは副作用を持たない (結果を得て
# から書く) ので、空応答だけは同じ呼び出しをやり直してよい — 結果が「空」と
# 分かっている呼び出しの再実行であって、「結果不明の LLM を自動で再実行しない」
# 規則には触れない。他の LLMError (rate limit / timeout / auth) はここでは
# 再試行しない — それぞれの再試行方針はクライアント側にある。
# ---------------------------------------------------------------------------

#: 空応答の再試行を含めた総試行回数の既定値。
DEFAULT_EMPTY_RESPONSE_ATTEMPTS = 3


def empty_response_attempts() -> int:
    """空応答に対する総試行回数 (env ``SAIVERSE_CHRONICLE_EMPTY_RESPONSE_RETRIES``)。

    値は「再試行を含めた総試行回数」。1 = 再試行しない。不正な値・0 以下は既定値。
    """
    raw = os.getenv("SAIVERSE_CHRONICLE_EMPTY_RESPONSE_RETRIES")
    if not raw:
        return DEFAULT_EMPTY_RESPONSE_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_EMPTY_RESPONSE_ATTEMPTS
    return value if value >= 1 else DEFAULT_EMPTY_RESPONSE_ATTEMPTS


def generate_text_with_empty_retry(
    client,
    messages,
    *,
    purpose: str,
    max_attempts: Optional[int] = None,
    persona_id: Optional[str] = None,
    usage_node_type: Optional[str] = None,
    **kwargs,
) -> str:
    """純生成の LLM 呼び出し。空応答だけを ``max_attempts`` 回まで試し直す。

    ``client.generate(messages=messages, tools=[], **kwargs)`` を呼び、strip した
    本文を返す。``EmptyResponseError`` または空文字 (空白のみ) の応答は再試行の
    対象。最後の試行でも空なら、最後の ``EmptyResponseError`` をそのまま (無ければ
    新しい ``EmptyResponseError`` を) 送出する — error_code は ``empty_response``
    のまま。それ以外の例外は 1 回目でもそのまま propagate する。

    ``usage_node_type`` を渡すと試行のたびに :func:`_record_llm_usage` を呼ぶ
    (空応答でも実際の API 呼び出しなので勘定に入れる)。

    ``max_attempts`` 未指定時は env ``SAIVERSE_CHRONICLE_EMPTY_RESPONSE_RETRIES``
    (既定 3) を呼び出し時に読む。
    """
    attempts = max_attempts if max_attempts is not None else empty_response_attempts()
    attempts = max(1, int(attempts))
    last_error: Optional[EmptyResponseError] = None
    for attempt in range(1, attempts + 1):
        last_error = None
        try:
            response = client.generate(messages=messages, tools=[], **kwargs)
        except EmptyResponseError as exc:
            # 空応答も往復は済んでいる (usage が付く) — 勘定に入れる。
            last_error = exc
            response = None
        if usage_node_type is not None:
            _record_llm_usage(client, persona_id, usage_node_type)
        content = (response or "").strip()
        if content:
            return content
        if attempt < attempts:
            LOGGER.warning(
                "[arasuji] empty LLM response for %s (attempt %d/%d); retrying",
                purpose, attempt, attempts,
            )
    if last_error is not None:
        raise last_error
    raise EmptyResponseError(
        f"empty LLM response for {purpose} after {attempts} attempt(s)"
    )


# Default settings
DEFAULT_BATCH_SIZE = 20  # messages per level-1 arasuji
DEFAULT_CONSOLIDATION_SIZE = 10  # entries per higher-level arasuji


def _format_timestamp(ts: Optional[int]) -> str:
    """Format Unix timestamp to readable string."""
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 機構名義の行の長さ規則 (2026-08-29 まはー裁定)
#
# 本人の提示に立った行はすべて編纂の材料に入る。ただし機構名義の行
# (MECHANISM_TAGS = handy_tool / spell / event_message) と知覚バッチは、
# 本文が閾値を超えたら**材料を組む時だけ**決定論の一行に縮む — DB の行
# (保存) は全文のまま。出力 (digest 本文) への機械的な付記は行わない:
# あらすじ本文は次のレベルの畳みで LLM の手本になるため、機構の定型ブロック
# を埋め込むと LLM がその形式を模倣して偽の機械記録を書き始める危険がある。
# ---------------------------------------------------------------------------

#: 機構名義の行をそのまま材料に入れる本文の上限 (字数)。
#: まはーが実機のチャット画面 1 画面分を実測した値。調整パラメータ。
MECHANISM_TEXT_MAX_CHARS = 500

#: 決定論の一行で、名前が取れなかったときに残す先頭行の冒頭字数。
_CONDENSE_HEAD_CHARS = 40

#: 先頭行の ``[Spell Result: ...]`` 等の機構プレフィックスの抽出。
_CONDENSE_NAME_RE = re.compile(r"^\[([^\[\]\n]{1,120})\]")


def _strip_system_wrap(text: str) -> str:
    """``<system>…</system>`` 包み (システム通知の確立形式) を剥がす。"""
    out = (text or "").strip()
    if out.startswith("<system>"):
        out = out[len("<system>"):]
    if out.endswith("</system>"):
        out = out[: -len("</system>")]
    return out.strip()


def condense_mechanism_text(text: str) -> str:
    """閾値超えの機構名義テキストを決定論の一行に縮める (LLM 不使用)。

    名前は本文の ``[Spell Result: ◯◯]`` 形のプレフィックスから機械抽出し、
    取れなければ先頭行の冒頭 + 字数に落とす。字数は元の全文の長さ。
    """
    total = len(text or "")
    first_line = next(
        (ln.strip() for ln in _strip_system_wrap(text).splitlines() if ln.strip()),
        "",
    )
    m = _CONDENSE_NAME_RE.match(first_line)
    if m:
        return f"[{m.group(1)}] を受け取った ({total:,} 字)"
    head = first_line[:_CONDENSE_HEAD_CHARS]
    ellipsis = "…" if len(first_line) > _CONDENSE_HEAD_CHARS else ""
    return f"{head}{ellipsis} ({total:,} 字)"


def _message_tags(msg: Message) -> tuple:
    meta = getattr(msg, "metadata", None)
    tags = meta.get("tags") if isinstance(meta, dict) else None
    return tuple(tags) if isinstance(tags, (list, tuple)) else ()


def is_mechanism_message(msg: Message) -> bool:
    """機構名義の行か (tags に MECHANISM_TAGS のいずれかを持つか)。

    判定はタグのみ — role では判定しない (システム通知は user role で
    書かれるため)。
    """
    return any(t in MECHANISM_TAGS for t in _message_tags(msg))


def material_text_for(content: str, tags: Sequence[str]) -> str:
    """本文とタグだけから材料テキストを決める、長さ規則の純テキスト形。

    Message オブジェクトを持たない呼び出し側 (退場計画 sea/eviction_plan.py の
    提示 payload) と :func:`material_text` が**同じ一枚の規則**を共有するための
    一点 — 閾値や縮め方をここ以外に二重実装しない (2026-08-29 まはー裁定:
    U 判定の物差しは材料字数)。
    """
    content = content or ""
    if (
        any(t in MECHANISM_TAGS for t in tags)
        and len(content) > MECHANISM_TEXT_MAX_CHARS
    ):
        return condense_mechanism_text(content)
    return content


def material_len(content: str, tags: Sequence[str]) -> int:
    """:func:`material_text_for` の字数形 (payload 側の字数勘定用)。"""
    return len(material_text_for(content, tags))


def material_text(msg: Message) -> str:
    """メッセージ 1 行の材料テキスト (長さ規則の適用点)。

    機構名義の行で閾値超えなら決定論の一行。それ以外は本文そのまま。
    """
    return material_text_for(msg.content or "", _message_tags(msg))


def material_chars(msg: Message) -> int:
    """材料としての字数 (チャンクの字数勘定用)。

    整列計画 (alignment) の U 計算はこれで数える — 勘定と材料の実体が
    ズレると発火閾値が狂うため (材料が一行に縮む行を全文の字数で数えると、
    LLM が実際に受け取る量よりはるかに早くチャンクが閉じる)。
    """
    return len(material_text(msg))


def _is_recall_text(text: str) -> bool:
    """入室時の想起の再提示か (本文が ``[想起:`` で始まる)。"""
    s = _strip_system_wrap(text)
    return s.startswith("[想起:")


#: 材料の種別ラベル (プロンプトに出す表記)。
_LABEL_RECALL = "【想起 (過去の再提示)】"
_LABEL_SPELL = "【スペル結果】"
_LABEL_NOTICE = "【通知】"
_LABEL_PERCEPTION = "【知覚】"


def _message_kind_label(msg: Message) -> str:
    """材料の種別ラベル (arasuji_levels.md §3-4 — 材料には種別を明示する)。

    - 作業セッションのダイジェスト行 (tag 'session_digest' =
      sea.work_session.DIGEST_TAG。sai_memory は sea に依存できないため
      リテラル) は「既に要約されたまとめ」であることを LLM に明示する —
      生の会話と同じ扱いで再展開されたり、発言として引用されたりしないため。
    - 機構名義の行 (2026-08-29 裁定で編纂材料に入った): スペル・ツール結果は
      【スペル結果】、システム通知は【通知】。本文が ``[想起:`` で始まる行は
      過去の再提示なので【想起 (過去の再提示)】が優先。
    """
    tags = _message_tags(msg)
    if "session_digest" in tags:
        return " [作業のまとめ]"
    if _is_recall_text(msg.content or ""):
        return f" {_LABEL_RECALL}"
    if "spell" in tags or "handy_tool" in tags:
        return f" {_LABEL_SPELL}"
    if "event_message" in tags:
        return f" {_LABEL_NOTICE}"
    return ""


def has_recall_material(
    messages: List[Message],
    extra_items: Optional[List[dict]] = None,
) -> bool:
    """材料に【想起 (過去の再提示)】ラベルの項目が含まれるか (プロンプト注記用)。"""
    for msg in messages:
        if _is_recall_text(msg.content or ""):
            return True
    for item in extra_items or []:
        if _is_recall_text(str(item.get("text") or "")):
            return True
    return False


#: 想起が材料に混ざるときにプロンプトへ足す一文
#: (一次あらすじの両生成経路 — executor._build_batch_prompt と
#: generate_level1_arasuji — で共通)。
RECALL_PROMPT_NOTE = (
    "- 【想起 (過去の再提示)】と印の付いた項目は、過去の出来事の再提示です。"
    "この期間に新しく起きた出来事として語らないでください"
)


def _format_messages_for_prompt(
    messages: List[Message],
    *,
    include_timestamp: bool = True,
    extra_items: Optional[List[dict]] = None,
) -> str:
    """Format messages (+ 知覚バッチ等の追加材料) for the arasuji prompt.

    Args:
        messages: Messages to format (時系列順)
        include_timestamp: If False, omit timestamps from output
        extra_items: メッセージ行ではない材料 (知覚の消費バッチ等)。
            ``{"at": epoch, "text": str}`` の list。時刻順の正しい位置に
            差し込む (同秒はメッセージが先 — 消費は書き込みの後に起きた事実)。
            本文が ``[想起:`` で始まる項目は【想起 (過去の再提示)】、それ以外は
            【知覚】とラベルし、長さ規則 (MECHANISM_TEXT_MAX_CHARS) を適用する。
    """
    entries: List[tuple] = []  # (at, tie, seq, line)
    for seq, msg in enumerate(messages):
        role = msg.role
        if role == "model":
            role = "assistant"
        content = material_text(msg).strip()
        if not content:
            continue
        kind = _message_kind_label(msg)
        prefix = (
            f"[{_format_timestamp(msg.created_at)}] " if include_timestamp else ""
        )
        entries.append(
            (msg.created_at or 0, 0, seq, f"{prefix}[{role}]{kind}: {content}")
        )
    for seq, item in enumerate(extra_items or []):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        label = _LABEL_RECALL if _is_recall_text(text) else _LABEL_PERCEPTION
        if len(text) > MECHANISM_TEXT_MAX_CHARS:
            text = condense_mechanism_text(text)
        at = int(item.get("at") or 0)
        prefix = f"[{_format_timestamp(at)}] " if include_timestamp else ""
        entries.append((at, 1, seq, f"{prefix}{label}: {text}"))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))
    return "\n\n".join(e[3] for e in entries)


def _format_entries_for_prompt(entries: List[ArasujiEntry], *, include_timestamp: bool = True) -> str:
    """Format arasuji entries for consolidation prompt."""
    lines: List[str] = []
    for i, entry in enumerate(entries, 1):
        if include_timestamp:
            start = _format_timestamp(entry.start_time)
            end = _format_timestamp(entry.end_time)
            lines.append(f"### あらすじ {i} ({start} ~ {end})")
        else:
            lines.append(f"### あらすじ {i}")
        lines.append(entry.content)
        lines.append("")
    return "\n".join(lines)


def _get_context_summaries(conn: sqlite3.Connection, current_level: int, *, include_timestamp: bool = True) -> str:
    """Get context summaries from higher levels for generation context.

    Retrieves unconsolidated entries from levels above the current level
    to provide context about what happened before.
    """
    context_parts: List[str] = []
    max_level = get_max_level(conn)

    # Start from highest level down to current_level + 1
    for level in range(max_level, current_level, -1):
        entries = get_leaf_entries_by_level(conn, level)
        if entries:
            # Calculate messages per entry at this level
            # Level 1 = batch_size, Level 2 = batch_size * consolidation_size, etc.
            context_parts.append(f"## レベル{level}のあらすじ（より大きな流れ）")
            for entry in entries:
                if include_timestamp:
                    start = _format_timestamp(entry.start_time)
                    end = _format_timestamp(entry.end_time)
                    context_parts.append(f"【{start} ~ {end}】")
                context_parts.append(entry.content)
                context_parts.append("")

    return "\n".join(context_parts) if context_parts else ""


def generate_level1_arasuji(
    client,
    conn: sqlite3.Connection,
    messages: List[Message],
    *,
    dry_run: bool = False,
    include_timestamp: bool = True,
    memopedia_context: Optional[str] = None,
    debug_log_path: Optional[Path] = None,
    persona_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    extra_items: Optional[List[dict]] = None,
    db_lock=None,
) -> Optional[ArasujiEntry]:
    """Generate a level-1 arasuji from messages.

    Args:
        client: LLM client with generate() method
        conn: Database connection
        messages: Messages to summarize
        dry_run: If True, don't save to database
        include_timestamp: If False, omit timestamps from prompt (useful when dates are unreliable)
        memopedia_context: Optional semantic memory context (page titles, summaries, keywords)
        extra_items: メッセージ行ではない材料 (知覚の消費バッチ等、
            ``{"at", "text"}`` の list)。再生成の付記継承が旧 entry の
            バッチを渡す (_format_messages_for_prompt 参照)。
        db_lock: SAIMemoryAdapter の ``_db_lock`` (Codex 七巡 K1)。``conn`` が
            adapter と共有のとき、文脈の読み取りと保存 (create/commit) を錠の
            内側で行う — ロック外の commit は他所の開いたトランザクションを
            途中で確定させる (memopedia_writers_bypass_adapter_lock と同族)。
            LLM 呼び出しは錠の外のまま。None = 従来どおり (専用 conn の
            CLI / API 再生成ルート)。

    Returns:
        Created ArasujiEntry or None on failure
    """
    from contextlib import nullcontext

    if not messages:
        return None

    # Extract time range from messages first (needed for temporal isolation)
    start_time = min(msg.created_at for msg in messages) if messages else None
    end_time = max(msg.created_at for msg in messages) if messages else None

    # Get episode context BEFORE this time range (temporal isolation)
    # This ensures we only see past Chronicles, not future ones during regeneration
    if start_time and end_time:
        from sai_memory.arasuji.context import get_episode_context_for_timerange
        with (db_lock or nullcontext()):
            context = get_episode_context_for_timerange(
                conn,
                start_time=start_time,
                end_time=end_time,
                max_entries=20
            )
    else:
        context = ""

    # Format messages (+ 追加材料)
    conversation = _format_messages_for_prompt(
        messages, include_timestamp=include_timestamp, extra_items=extra_items,
    )
    if not conversation.strip():
        return None

    prompt_parts = [
        "あなたは記憶の記録者です。以下の会話から、出来事のあらすじを書いてください。",
        "",
    ]

    if context:
        prompt_parts.extend([
            "## これまでの流れ（参考）",
            context,
            "",
        ])

    if memopedia_context:
        prompt_parts.extend([
            "## 意味記憶（人物・用語の背景情報）",
            memopedia_context,
            "",
        ])

    prompt_parts.extend([
        "## 今回記録する会話",
        conversation,
        "",
        "## 指示",
        "- 3〜5文程度で、何が起きたか、誰と何を話したかを要約",
        "- 時系列の流れがわかるように書く",
        "- 固有名詞や重要な詳細は保持する",
        "- 感情や雰囲気も含める",
        "- 「〜について話した」のような抽象的な記述は避け、具体的に書く",
    ])
    if has_recall_material(messages, extra_items):
        prompt_parts.append(RECALL_PROMPT_NOTE)
    prompt_parts.extend([
        "- **日時情報（【2025-01-07 23:56 ~】など）は書かないでください**（自動で付与されます）",
        "- **「あらすじ」などの見出しは書かないでください**（本文のみ出力）",
        "",
        "あらすじを日本語で書いてください。",
    ])

    prompt = "\n".join(prompt_parts)

    # Debug log: write prompt
    if debug_log_path:
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"[CHRONICLE Lv1] {datetime.now().isoformat()}\n")
            f.write("=" * 80 + "\n")
            f.write("--- PROMPT ---\n")
            f.write(prompt)
            f.write("\n")

    # --- LLM call (空応答だけ再試行。他の LLMError の再試行方針はクライアント側) ---
    try:
        response = generate_text_with_empty_retry(
            client,
            [{"role": "user", "content": prompt}],
            purpose="chronicle_level1 regenerate",
            persona_id=persona_id,
            usage_node_type="chronicle_level1",
        )
    except Exception as e:
        LOGGER.error(f"LLM call failed for level-1 arasuji: {e}")
        from llm_clients.exceptions import LLMError
        if isinstance(e, LLMError):
            raise  # Propagate all LLM errors (empty, safety, timeout, etc.)
        return None

    # Debug log: write response
    if debug_log_path:
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write("--- RESPONSE ---\n")
            f.write(response)
            f.write("\n")

    # 空応答は generate_text_with_empty_retry が EmptyResponseError にして
    # 上の except で propagate 済み — ここに来た response は非空。
    content = response

    # Extract message IDs (time range already calculated at the beginning)
    source_ids = [msg.id for msg in messages]

    if dry_run:
        LOGGER.info(f"[DRY RUN] Would create level-1 arasuji: {content}")
        return ArasujiEntry(
            id="dry-run",
            level=1,
            content=content,
            source_ids=source_ids,
            start_time=start_time,
            end_time=end_time,
            source_count=len(messages),
            message_count=len(messages),
            parent_id=None,
            is_consolidated=False,
            created_at=0,
        )

    # --- DB save with retry (LLM result is already obtained, no re-call) ---
    max_db_retries = 3
    for attempt in range(max_db_retries):
        try:
            # 保存は錠の内側 (K1)。リトライの sleep は錠の外 (下) — 錠を
            # 持ったまま待たない。
            with (db_lock or nullcontext()):
                entry = create_entry(
                    conn,
                    level=1,
                    content=content,
                    source_ids=source_ids,
                    start_time=start_time,
                    end_time=end_time,
                    source_count=len(messages),
                    message_count=len(messages),
                    thread_id=thread_id,
                    # 被覆字数 (材料としての字数 = 圧縮後) — executor 経路と同じ
                    # 勘定。ここで刻まないと bands.backfill_coverage が生ログの
                    # 素の字数で埋め、長さ規則 (material_chars) と食い違う。
                    # 知覚バッチ (extra_items) は executor 経路の coverage_chars
                    # も勘定に入れていないので、ここでも入れない (一貫性優先)。
                    extra_metadata={
                        "coverage_chars": sum(
                            material_chars(m) for m in messages
                        ),
                    },
                )
            LOGGER.info("Created level-1 arasuji: content=%s", content[:60])
            return entry
        except Exception as e:
            LOGGER.warning(
                "DB save failed for level-1 arasuji (attempt %d/%d): %s",
                attempt + 1, max_db_retries, e,
            )
            # rollback も錠の内側 (Codex 八巡) — 錠外の rollback は他所の開いた
            # トランザクションを巻き戻しうる。錠の外に残すのは sleep だけ。
            with (db_lock or nullcontext()):
                try:
                    conn.rollback()
                except Exception:
                    pass
            if attempt < max_db_retries - 1:
                time.sleep(2 ** attempt)

    LOGGER.error("DB save failed after %d attempts for level-1 arasuji", max_db_retries)
    return None


