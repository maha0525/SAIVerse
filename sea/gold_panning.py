"""gold_panning (砂金採り) — 押し出される記憶からの恒常知識の採取。

Metabolism の eviction 直前、メインラインの温まった prefix (head + 履歴) に注入
プロンプトを 1 手足し、structured output で「コア記憶に採取すべきもの」を返させる。
決定は本人 (ペルソナ)、複製・書き込みはシステム側の決定論。

不変条件 (docs/intent/gold_panning.md §5):
- キャッシュが熱い瞬間にのみ走る (defer-to-hot は SessionLifecycle 側)
- 失敗は Metabolism 本体を止めない (呼び出し側の try/except 隔離)
- scene は参照コピーのみ (ペルソナに書き写させない)。照合失敗は明示
- 「採取なし」が正規の応答。採取を促す圧はプロンプトに入れない
- モデルは (persona, default) 固定。lightweight へ切替えない

詳細設計: docs/intent/gold_panning.md / 実装仕様: gold_panning_impl_spec.md
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# env 設定 (sea/auto_recall.py と同じく毎回 os.getenv を読む)
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("[gold_panning] invalid float for %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("[gold_panning] invalid int for %s=%r; using default %s", name, raw, default)
        return default


def is_enabled() -> bool:
    """全体トグル。"0"/"false" で無効化 (defer-to-hot ごと無効になる)。"""
    return _env_flag("SAIVERSE_GOLD_PANNING_ENABLED", True)


def get_pending_cap() -> float:
    """defer-to-hot 圧力弁の倍率 (high watermark の何倍で「コールドでも実行」に倒すか)。"""
    return _env_float("SAIVERSE_GOLD_PANNING_PENDING_CAP", 1.5)


def get_min_quote_chars() -> int:
    """scene 引用の最小文字数。これ未満は一意に指せないので照合せず失敗扱い。"""
    return _env_int("SAIVERSE_GOLD_PANNING_MIN_QUOTE_CHARS", 10)


def get_close_min_messages() -> int:
    """セッションクローズ (Phase 3) 時のスキップ下限。定義だけ先置き。"""
    return _env_int("SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES", 10)


# ---------------------------------------------------------------------------
# response_schema (Gemini 制約: additionalProperties 禁止、フラット)
# ---------------------------------------------------------------------------

_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "reflection": {
            "type": "string",
            "description": "採取判断の短い独白。採取なしでも一言。",
        },
        "ops": {
            "type": "array",
            "description": "コア記憶への操作列。採取なしなら空配列。",
            "items": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["add", "update", "remove", "add_scene"],
                    },
                    "content": {
                        "type": "string",
                        "description": "add / update の本文。",
                    },
                    "memory_id": {
                        "type": "integer",
                        "description": "update / remove 対象の c:N の N。",
                    },
                    "quote": {
                        "type": "string",
                        "description": "add_scene: 残したい場面の発言の原文引用 1 行。",
                    },
                    "rounds": {
                        "type": "integer",
                        "description": "add_scene: 引用前後の往復数 (既定 3)。",
                    },
                },
                "required": ["op"],
            },
        },
    },
    "required": ["ops"],
}


# ---------------------------------------------------------------------------
# 注入プロンプト
# ---------------------------------------------------------------------------

_SCENE_PREVIEW_CHARS = 80


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _build_panning_prompt(persona: Any) -> str:
    """<system> 包みの注入プロンプトを組む (keepalive の末尾通知と同じ面)。"""
    from sai_memory.core_memory import list_core_memories, total_core_memory_chars
    from builtin_data.tools._core_memory_common import resolve_core_memory_budget

    adapter = getattr(persona, "sai_memory", None)
    persona_id = getattr(persona, "persona_id", None)

    # 現在のコア記憶全項目を [c:id] content 形式で列挙する。
    core_lines: List[str] = []
    total_chars = 0
    if adapter is not None and getattr(adapter, "conn", None) is not None:
        try:
            with adapter._db_lock:
                memories = list_core_memories(adapter.conn)
                total_chars = total_core_memory_chars(adapter.conn)
            for mem in memories:
                body = mem.content or ""
                if mem.kind == "scene":
                    body = _truncate(body.replace("\n", " "), _SCENE_PREVIEW_CHARS)
                core_lines.append(f"- [c:{mem.id}] {body}")
        except Exception:
            LOGGER.warning("[gold_panning] failed to list core memories for prompt", exc_info=True)

    budget = resolve_core_memory_budget(persona_id) if persona_id else 2000

    core_block = "\n".join(core_lines) if core_lines else "（まだコア記憶はありません）"

    prompt = (
        "<system>\n"
        "## 記憶整理の節目 — 砂金採り\n"
        "まもなく古い会話が記憶整理で押し出されます。この機会に、恒常的に携えて\n"
        "おくべきことがあれば、いま「コア記憶」に採取できます。押し出された後では\n"
        "この会話は手元から消えるため、採るならこのタイミングだけです。\n"
        "\n"
        "採取を検討する観点:\n"
        "- 状態の変化（生活・仕事・健康・関係性など、いま進行中の事実）。状態には\n"
        "  日付を含めてください（例: 2026年6月頃〜 ユーザーは海外赴任中、9月帰国予定）。\n"
        "- 既存のコア記憶と矛盾する新情報（帰国・引っ越しなど。矛盾は update で解消）。\n"
        "- 原文のまま残したい印象的な場面（口調・関係性のアンカー）。\n"
        "\n"
        "姿勢:\n"
        "- **採取しないのが普通です。** ほとんどの記憶整理では何も採りません（ops は\n"
        "  空配列）。無理に何かを刻もうとしないでください。\n"
        "- 既にコア記憶にあることは再度採らないでください。\n"
        "\n"
        "場面（scene）を原文で残したい場合は、その場面に含まれる発言を一字一句その\n"
        "まま 1 行 quote に引用してください（要約・言い換えはしない）。システムが\n"
        "その発言を探し当て、前後を含めて原文のまま複製します。\n"
        "\n"
        f"### 現在のコア記憶（合計 {total_chars:,} 字 / 目安 {budget:,} 字）\n"
        f"{core_block}\n"
        "</system>"
    )
    return prompt


# ---------------------------------------------------------------------------
# scene のファジー照合
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """NFKC → 空白類圧縮 → lowercase。"""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text)
    norm = " ".join(norm.split())
    return norm.lower()


def _partial_ratio(shorter: str, longer: str) -> float:
    """shorter を longer 内の最良部分窓に対して照合した比率 (fuzzywuzzy partial_ratio 相当)。"""
    if not shorter or not longer:
        return 0.0
    if len(shorter) > len(longer):
        shorter, longer = longer, shorter
    matcher = difflib.SequenceMatcher(None, shorter, longer)
    best = 0.0
    for i, j, _n in matcher.get_matching_blocks():
        start = max(0, j - i)
        end = start + len(shorter)
        substr = longer[start:end]
        if not substr:
            continue
        ratio = difflib.SequenceMatcher(None, shorter, substr).ratio()
        if ratio > best:
            best = ratio
    return best


def _resolve_quote(
    quote: str,
    current_messages: List[Dict[str, Any]],
    *,
    min_quote_chars: int,
    threshold: float = 0.8,
) -> Optional[str]:
    """quote に対応する message id をファジー照合で解決する。見つからなければ None。

    照合対象は current_messages 全体 (押し出し分に限定しない — 引用が残留窓側でも
    scene として有効)。
    """
    norm_quote = _normalize(quote)
    if len(norm_quote) < min_quote_chars:
        # 短すぎる引用は一意に指せない (誤爆防止)。
        return None

    # 第1段: 正規化部分文字列一致。複数ヒット時は最後 (最新) のメッセージを採用。
    substr_hit: Optional[str] = None
    for msg in current_messages:
        mid = msg.get("id")
        if not mid:
            continue
        norm_content = _normalize(msg.get("content", ""))
        if norm_quote and norm_quote in norm_content:
            substr_hit = mid  # 後勝ち = 最新
    if substr_hit is not None:
        return substr_hit

    # 第2段: SequenceMatcher の最良部分一致比率で threshold 以上の最高スコア。
    best_id: Optional[str] = None
    best_ratio = threshold
    for msg in current_messages:
        mid = msg.get("id")
        if not mid:
            continue
        norm_content = _normalize(msg.get("content", ""))
        if not norm_content:
            continue
        ratio = _partial_ratio(norm_quote, norm_content)
        # ties は後勝ち (最新) — >= で更新する。
        if ratio >= best_ratio:
            best_ratio = ratio
            best_id = mid
    return best_id


# ---------------------------------------------------------------------------
# ops の適用 (tool 関数を経由せず直接)
# ---------------------------------------------------------------------------

def _apply_ops(
    persona: Any,
    ops: List[Dict[str, Any]],
    current_messages: List[Dict[str, Any]],
    *,
    min_quote_chars: int,
) -> tuple[int, int, List[str]]:
    """ops を順に適用し、(成功数, 失敗数, 結果テキスト行) を返す。

    1 op の失敗で残りを止めない。失敗は黙って捨てず結果テキストに明示する。
    """
    from sai_memory.core_memory import (
        add_core_memory,
        create_scene_core_memory,
        remove_core_memory,
        update_core_memory,
    )

    adapter = getattr(persona, "sai_memory", None)
    persona_id = getattr(persona, "persona_id", None)
    persona_name = getattr(persona, "persona_name", None) or persona_id or "assistant"

    applied = 0
    failed = 0
    lines: List[str] = []

    if adapter is None or getattr(adapter, "conn", None) is None:
        return (0, len(ops), ["コア記憶ストレージが利用できず、採取を適用できませんでした。"])

    for op in ops:
        if not isinstance(op, dict):
            failed += 1
            lines.append(f"不正な op 形式のためスキップ: {op!r}")
            continue
        kind = op.get("op")
        try:
            if kind == "add":
                content = (op.get("content") or "").strip()
                if not content:
                    failed += 1
                    lines.append("add 失敗: 本文が空でした。")
                    continue
                with adapter._db_lock:
                    new_id = add_core_memory(adapter.conn, content)
                applied += 1
                # 記録は <system> 包みのシステム通知として SAIMemory に残る
                # (_persist_record 参照)。省略・切り詰めは採取事実の改変になるため、
                # 本文は全文を書く (不変条件 §5-8 / 2026-07-07 まはー指摘)。
                lines.append(f"コア記憶 c:{new_id} に採取: {content}")

            elif kind == "update":
                memory_id = op.get("memory_id")
                content = (op.get("content") or "").strip()
                if memory_id is None:
                    failed += 1
                    lines.append("update 失敗: memory_id が指定されていません。")
                    continue
                if not content:
                    failed += 1
                    lines.append(f"update 失敗: c:{memory_id} の新しい本文が空でした。")
                    continue
                with adapter._db_lock:
                    ok = update_core_memory(adapter.conn, int(memory_id), content)
                if ok:
                    applied += 1
                    lines.append(f"コア記憶 c:{memory_id} を更新: {content}")
                else:
                    failed += 1
                    lines.append(f"update 失敗: c:{memory_id} が見つかりませんでした。")

            elif kind == "remove":
                memory_id = op.get("memory_id")
                if memory_id is None:
                    failed += 1
                    lines.append("remove 失敗: memory_id が指定されていません。")
                    continue
                with adapter._db_lock:
                    ok = remove_core_memory(adapter.conn, int(memory_id))
                if ok:
                    applied += 1
                    lines.append(f"コア記憶 c:{memory_id} を削除しました。")
                else:
                    failed += 1
                    lines.append(f"remove 失敗: c:{memory_id} が見つかりませんでした。")

            elif kind == "add_scene":
                quote = op.get("quote") or ""
                rounds = op.get("rounds")
                try:
                    rounds_int = int(rounds) if rounds is not None else 3
                except (TypeError, ValueError):
                    rounds_int = 3
                if rounds_int <= 0:
                    rounds_int = 3
                mid = _resolve_quote(
                    quote, current_messages, min_quote_chars=min_quote_chars,
                )
                if not mid:
                    failed += 1
                    lines.append(
                        f"scene 照合失敗: 引用「{quote}」に一致する発言が"
                        "見つかりませんでした。"
                    )
                    continue
                with adapter._db_lock:
                    result = create_scene_core_memory(
                        adapter.conn, mid, rounds=rounds_int, persona_name=persona_name,
                    )
                if result is None:
                    failed += 1
                    lines.append(
                        f"scene 採取失敗: メッセージ {mid} が実会話として切り抜けませんでした。"
                    )
                else:
                    applied += 1
                    lines.append(
                        f"コア記憶 c:{result.memory_id}（会話の記憶）に採取: "
                        f"{result.message_count} 発言分（{result.date_start}〜{result.date_end}）。"
                    )
            else:
                failed += 1
                lines.append(f"未知の op '{kind}' をスキップしました。")
        except Exception as exc:
            failed += 1
            lines.append(f"op '{kind}' の適用中にエラー: {exc}")
            LOGGER.warning("[gold_panning] op %r raised", kind, exc_info=True)

    return (applied, failed, lines)


# ---------------------------------------------------------------------------
# 永続化 (判断ターンをペルソナの記憶に残す)
# ---------------------------------------------------------------------------

def _persist_record(
    persona: Any,
    record_text: str,
    prompt_snapshot: str,
    *,
    applied_ops: int,
) -> None:
    """判断ターンを main_line / (committed|discardable) で SAIMemory に残す。

    採取ありなら committed (コンテキストに残る来歴)、なしなら discardable
    (DB には残るが context 復元から除外)。生 JSON は保存しない (自然文のみ)。

    role は "user"、``record_text`` は呼び出し側で ``<system>…</system>`` に
    包んだシステム通知形式で渡る (event_message の確立形式)。プロンプト無しの
    ``role="assistant"`` メッセージは「自分は普段こう喋る」という few-shot 汚染源に
    なるため、ペルソナ発話ではなくナレーションとして残す (2026-07-07 まはー指摘)。
    """
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None:
        return

    pulse_id = None
    try:
        from tools.context import get_active_pulse_context
        pulse_ctx = get_active_pulse_context()
        pulse_id = getattr(pulse_ctx, "pulse_id", None) if pulse_ctx else None
    except Exception:
        pulse_id = None

    try:
        adapter.append_persona_message({
            "role": "user",
            "content": record_text,
            # tz-aware UTC ISO 文字列必須 (naive だと adapter が system TZ 解釈で
            # created_at が ±9h ずれる)。
            "timestamp": datetime.now(timezone.utc).isoformat(),
            # internal/event_message タグで General Chronicle / scene 切り出し /
            # キーワード検索から自動除外させる (storage.CHRONICLE_EXCLUDED_TAGS 他)。
            # 現状は本人会話として Chronicle に混入するバグで、この除外は意図した
            # 挙動変化 (2026-07-07)。
            "metadata": {"tags": ["internal", "event_message", "gold_panning"]},
            "line_role": "main_line",
            "scope": "committed" if applied_ops > 0 else "discardable",
            "pulse_id": pulse_id,
            "paired_action_text": prompt_snapshot,
        })
    except Exception:
        LOGGER.exception("[gold_panning] failed to persist judgment record")


# ---------------------------------------------------------------------------
# pan マーカー永続化 (再起動を跨ぐ「前回採取した末尾 id」)
# ---------------------------------------------------------------------------
#
# マーカーは run_session_close の「新規メッセージ数」ガードの基準。消えると窓
# 全体が新規扱いになり採取 LLM コールが 1 回余分に走る。message id と同じ
# memory.db (embed_metadata KV) に置くことで、memory.db のリストア/差し替えでも
# id の指す先とマーカーがずれない。read-through: 取得は persona 属性→無ければ
# 永続ストア→属性にキャッシュ。保存は属性と永続ストアの両方 (write-through)。

_PAN_MARKER_KEY = "gold_panning_last_pan_id"


def _load_pan_marker(persona: Any) -> Optional[str]:
    """pan マーカー (前回採取した末尾 message id) を取得する (read-through)。

    persona 属性にキャッシュがあればそれを返す。無ければ永続ストア (memory.db の
    embed_metadata KV) からロードし、属性にキャッシュしてから返す。ストア読み出し
    失敗は採取判断を止めない (WARNING して None を返す = マーカー無し扱い)。
    """
    cached = getattr(persona, "_gold_panning_last_pan_id", None)
    if cached is not None:
        return cached
    adapter = getattr(persona, "sai_memory", None)
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None:
        return None
    try:
        from sai_memory.memory.storage import get_embed_metadata
        with adapter._db_lock:
            value = get_embed_metadata(conn, _PAN_MARKER_KEY)
    except Exception:
        LOGGER.warning("[gold_panning] failed to load pan marker from store", exc_info=True)
        return None
    if value:
        persona._gold_panning_last_pan_id = value
    return value


def _save_pan_marker(persona: Any, last_id: str) -> None:
    """pan マーカーを persona 属性と永続ストアの両方に書く (write-through)。

    永続化 (テーブル書き込み) の失敗は採取本体を止めない。WARNING を残し、
    in-memory 属性だけ更新して続行する (プロセス生存中はガードが効き、次回書き込みで
    永続側の回復を試みる)。
    """
    persona._gold_panning_last_pan_id = last_id
    adapter = getattr(persona, "sai_memory", None)
    conn = getattr(adapter, "conn", None) if adapter is not None else None
    if conn is None:
        return
    try:
        from sai_memory.memory.storage import set_embed_metadata
        with adapter._db_lock:
            set_embed_metadata(conn, _PAN_MARKER_KEY, last_id)
    except Exception:
        LOGGER.warning("[gold_panning] failed to persist pan marker to store", exc_info=True)


# ---------------------------------------------------------------------------
# エントリ関数
# ---------------------------------------------------------------------------

def run_gold_panning(
    lifecycle: Any,
    persona: Any,
    building_id: str,
    current_messages: List[Dict[str, Any]],
    evict_count: int,
    event_callback: Optional[Any] = None,
) -> Dict[str, Any]:
    """砂金採り本体。押し出し直前のメインライン prefix に 1 手足して採取を判断させる。

    Args:
        lifecycle: SessionLifecycle インスタンス (lifecycle.runtime で SEARuntime)。
        current_messages: run_metabolism の手元の履歴 (押し出し対象は [:evict_count])。
        evict_count: 押し出される件数。

    Returns:
        {"ops_applied": int, "ops_failed": int, "skipped": bool, "reason": str|None}
    """
    if not is_enabled():
        return {"ops_applied": 0, "ops_failed": 0, "skipped": True, "reason": "disabled"}

    runtime = lifecycle.runtime
    persona_id = getattr(persona, "persona_id", None)
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not getattr(adapter, "is_ready", lambda: False)():
        return {"ops_applied": 0, "ops_failed": 0, "skipped": True, "reason": "no_memory"}

    if event_callback:
        try:
            event_callback({
                "type": "metabolism",
                "status": "gold_panning",
                "content": "覚えておくことを探しています……",
            })
        except Exception:
            LOGGER.debug("[gold_panning] start event_callback raised", exc_info=True)

    # 1. メインラインと同じ context を組み、末尾に注入プロンプトを 1 つ足す。
    #    直前の応答コールで prefix が温まっている前提 (defer-to-hot が保証)。
    messages = list(runtime._prepare_context(persona, building_id, None) or [])
    prompt = _build_panning_prompt(persona)
    messages.append({"role": "user", "content": prompt})

    node_def = SimpleNamespace(id="gold_panning", memorize=None, speak=False)
    # standard tier (default モデル固定)。lightweight への分岐は書かない (intent §5-7)。
    llm_client = runtime._select_llm_client(node_def, persona, needs_structured_output=True)

    result = llm_client.generate(
        messages,
        tools=[],
        response_schema=_RESPONSE_SCHEMA,
        temperature=runtime._default_temperature(persona),
        **runtime._get_cache_kwargs(persona_id),
    )

    # 2. usage 記録 + anchor touch (keepalive の後処理と同じ)。
    usage = llm_client.consume_usage() if hasattr(llm_client, "consume_usage") else None
    if usage is not None:
        try:
            from saiverse.usage_tracker import get_usage_tracker
            get_usage_tracker().record_usage(
                model_id=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_ttl=usage.cache_ttl,
                persona_id=persona_id,
                building_id=building_id,
                node_type="gold_panning",
                playbook_name="gold_panning",
                category="gold_panning",
            )
        except Exception:
            LOGGER.warning("[gold_panning] usage tracking failed (persona=%s)", persona_id, exc_info=True)
        try:
            lifecycle.touch_anchor_after_llm_call(persona, usage)
        except Exception:
            LOGGER.warning("[gold_panning] anchor touch failed (persona=%s)", persona_id, exc_info=True)

    # 3. 返り値を dict へ正規化。str なら json.loads を 1 回試す。
    reflection = ""
    ops: List[Dict[str, Any]] = []
    if isinstance(result, dict):
        reflection = str(result.get("reflection", "") or "")
        raw_ops = result.get("ops", [])
        ops = list(raw_ops) if isinstance(raw_ops, list) else []
    elif isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                reflection = str(parsed.get("reflection", "") or "")
                raw_ops = parsed.get("ops", [])
                ops = list(raw_ops) if isinstance(raw_ops, list) else []
            else:
                reflection = result
        except (ValueError, TypeError):
            LOGGER.warning(
                "[gold_panning] structured output parse failed; treating as no-op (persona=%s)",
                persona_id,
            )
            reflection = result
    else:
        LOGGER.warning(
            "[gold_panning] unexpected result type %s; treating as no-op (persona=%s)",
            type(result).__name__, persona_id,
        )

    # 4. ops を適用。
    min_quote_chars = get_min_quote_chars()
    applied, failed, result_lines = _apply_ops(
        persona, ops, current_messages, min_quote_chars=min_quote_chars,
    )

    # 5. 記録テキスト。event_message 形式のシステム通知として <system> に包む
    #    (ペルソナ発話ではなくナレーション。few-shot 汚染回避、2026-07-07 まはー指摘)。
    #    判断 (reflection) と適用結果行を全文載せる (省略・切り詰め禁止、不変条件 §5-8)。
    persona_name = getattr(persona, "persona_name", None) or persona_id or "assistant"
    body_lines: List[str] = ["記憶整理の節目 — コア記憶の採取判断:"]
    if reflection.strip():
        body_lines.append(f"{persona_name}の判断: {reflection.strip()}")
    if result_lines:
        body_lines.extend(result_lines)
    if applied == 0 and not result_lines:
        body_lines.append("今回は採取しませんでした。")
    record_text = "<system>" + "\n".join(body_lines) + "\n</system>"

    _persist_record(persona, record_text, prompt, applied_ops=applied)

    # 6. 完了通知 (採取ありなら件数を含める)。
    if event_callback and applied > 0:
        try:
            event_callback({
                "type": "metabolism",
                "status": "gold_panning",
                "content": f"覚えておくことを {applied} 件、コア記憶に刻みました。",
            })
        except Exception:
            LOGGER.debug("[gold_panning] completion event_callback raised", exc_info=True)

    # 7. pan マーカー: 次回セッションクローズ (Phase 3) の「新規メッセージ」判定用に、
    #    今回採取対象とした current_messages の末尾 id を記録する。persona 属性と
    #    memory.db (embed_metadata KV) の両方に書き、プロセス再起動を跨いでも
    #    ガードが効くようにする (_save_pan_marker: 永続化失敗は WARNING のみで続行)。
    if current_messages:
        last = current_messages[-1]
        last_id = last.get("id") if isinstance(last, dict) else None
        if last_id:
            _save_pan_marker(persona, last_id)

    LOGGER.info(
        "[gold_panning] done: persona=%s applied=%d failed=%d ops=%d (evict=%d)",
        persona_id, applied, failed, len(ops), evict_count,
    )
    return {"ops_applied": applied, "ops_failed": failed, "skipped": False, "reason": None}


# ---------------------------------------------------------------------------
# セッションクローズ (Phase 3): TTL 発火時にペルソナが Active でない = セッションが
# 閉じた瞬間の砂金採り + Chronicle 前倒し。docs/intent/gold_panning.md §3.6。
# ---------------------------------------------------------------------------

def _count_new_since_marker(
    current_messages: List[Dict[str, Any]], last_pan_id: Optional[str],
) -> int:
    """マーカー (前回採取した末尾 id) 以降の「新規」件数を数える。

    - マーカー無し (None) → 全件が新規。
    - マーカーが窓内にあれば、その次以降の件数。
    - マーカーが窓外 (押し出されて消えた) → 全件が新規。

    マーカーが複数一致する場合は後勝ち (最新) を採用する。
    """
    if not current_messages:
        return 0
    if not last_pan_id:
        return len(current_messages)
    idx: Optional[int] = None
    for i, msg in enumerate(current_messages):
        mid = msg.get("id") if isinstance(msg, dict) else None
        if mid == last_pan_id:
            idx = i
    if idx is None:
        return len(current_messages)
    return len(current_messages) - idx - 1


def run_session_close(lifecycle: Any, persona: Any) -> Dict[str, Any]:
    """セッションクローズ時の砂金採り + Chronicle 前倒し。

    run_cache_keepalive の not-Active 分岐 (セッションが閉じ、anchor がまだ温かい
    可能性が高い唯一の停止点) から別スレッド経由で呼ばれる。

    処理順は docs/intent/gold_panning.md §3.6 / 実装仕様 gold_panning_phase3_spec.md:
    Chronicle 前倒しは採取の成否と独立に実行し、採取はマーカーガードを通過し、かつ
    キャッシュが熱いときだけ走る (不変条件 §5-1: クローズはコールド例外を作らない)。

    Returns:
        {"panned": bool, "chronicle": bool, "skipped_reason": str|None}
    """
    persona_id = getattr(persona, "persona_id", None)

    if not is_enabled():
        return {"panned": False, "chronicle": False, "skipped_reason": "disabled"}

    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not getattr(adapter, "is_ready", lambda: False)():
        return {"panned": False, "chronicle": False, "skipped_reason": "no_memory"}

    # in-flight ガード: 同一ペルソナのクローズが二重に走らないようにする。
    if getattr(persona, "_gold_panning_close_inflight", False):
        LOGGER.debug(
            "[gold_panning] session close already in-flight; skipping (persona=%s)", persona_id,
        )
        return {"panned": False, "chronicle": False, "skipped_reason": "inflight"}

    persona._gold_panning_close_inflight = True
    panned = False
    chronicle_done = False
    skipped_reason: Optional[str] = None
    try:
        # window 取得 (maybe_run_metabolism と同じ形)。
        history_mgr = getattr(persona, "history_manager", None)
        anchor = getattr(history_mgr, "metabolism_anchor_message_id", None) if history_mgr else None
        if not history_mgr or not anchor:
            LOGGER.info(
                "[gold_panning] session close: no anchor; skipping (persona=%s)", persona_id,
            )
            return {"panned": False, "chronicle": False, "skipped_reason": "no_anchor"}

        current_messages = history_mgr.get_history_from_anchor(
            anchor,
            required_line_roles=["main_line"],
            required_scopes=["committed"],
        ) or []

        # マーカーガード: 新規件数が close_min 未満なら採取スキップ (Chronicle は実行してよい)。
        new_count = _count_new_since_marker(current_messages, _load_pan_marker(persona))
        close_min = get_close_min_messages()
        pan_allowed = new_count >= close_min
        if not pan_allowed:
            skipped_reason = "below_min"
            LOGGER.info(
                "[gold_panning] session close: new messages below close_min "
                "(persona=%s new=%d min=%d); skipping pan", persona_id, new_count, close_min,
            )

        # Chronicle 前倒し (採取の成否と独立)。run_metabolism と同じゲート。
        memory_weave_enabled = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "").lower() in ("true", "1")
        if memory_weave_enabled and lifecycle.is_chronicle_enabled_for_persona(persona):
            try:
                # force=True: 確認ダイアログ・pulse_type 判定を回避 (persona._current_pulse_type
                # は前回 Pulse の残留値で不定。session_lifecycle.generate_chronicle docstring)。
                lifecycle.generate_chronicle(persona, force=True)
                chronicle_done = True
            except Exception:
                LOGGER.exception(
                    "[gold_panning] session close: generate_chronicle failed (persona=%s)", persona_id,
                )
            try:
                lifecycle.generate_track_chronicle(persona)
            except Exception:
                LOGGER.exception(
                    "[gold_panning] session close: generate_track_chronicle failed (persona=%s)", persona_id,
                )
        # ensure_recall_embeddings はゲート外で必ず実行 (run_metabolism と同じ思想:
        # ローカル・無料で、Chronicle 生成の成否・トグルに相乗りさせない)。
        try:
            lifecycle.ensure_recall_embeddings(persona)
        except Exception:
            LOGGER.exception(
                "[gold_panning] session close: ensure_recall_embeddings failed (persona=%s)", persona_id,
            )

        # 採取 (マーカーガード通過時のみ、かつ hot のときだけ)。
        if pan_allowed:
            building_id = getattr(persona, "current_building_id", None)
            if not building_id:
                skipped_reason = "no_building"
                LOGGER.info(
                    "[gold_panning] session close: no current_building_id (persona=%s); skipping pan",
                    persona_id,
                )
            elif not lifecycle._is_cache_hot(persona):
                skipped_reason = "cold"
                LOGGER.info(
                    "[gold_panning] session close: cache cold (persona=%s); skipping pan. "
                    "Chronicle already done, so invariant §5-1 (no cold exception) holds", persona_id,
                )
            else:
                run_gold_panning(
                    lifecycle, persona, building_id, current_messages,
                    evict_count=0, event_callback=None,
                )
                panned = True

        LOGGER.info(
            "[gold_panning] session close done: persona=%s panned=%s chronicle=%s skipped_reason=%s",
            persona_id, panned, chronicle_done, skipped_reason,
        )
        return {"panned": panned, "chronicle": chronicle_done, "skipped_reason": skipped_reason}
    finally:
        persona._gold_panning_close_inflight = False
