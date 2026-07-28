"""Chronicle chunk executor (arasuji_levels.md — レベル0 の畳みの実行).

alignment.plan_alignment が作った AlignmentPlan を実行し、一次あらすじを
チャンク単位の単一トランザクションで確定する。設計正典は
docs/intent/arasuji_levels.md (旧: 2026-07-21 W4 handoff D4)。

原子性の契約 (M2 の解の生成側):

- チャンク 1 個 = 単一 tx (重複再検査 → INSERT → commit)。途中失敗しても
  確定済みチャンクはそのまま残り、source_ids の重複検査と processed_ids の
  再計算により再試行で二重生成されない。
- LLM 呼び出しは tx の外 (応答を得てから書く)。
- 失敗はチャンク境界で例外として上げる — 呼び出し側 (generate_chronicle)
  が status="failed" に写像し、anchor は据え置かれる (S2 ゲート)。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from sai_memory.arasuji.alignment import (
    AlignmentPlan,
    PlannedChunk,
)
from sai_memory.arasuji.storage import ArasujiEntry, create_entry

LOGGER = logging.getLogger(__name__)


class ChunkExecutionError(RuntimeError):
    """チャンク実行の失敗 (LLM 応答空・保存失敗など LLMError 以外の失敗)。"""


@dataclass
class ExecutionResult:
    """execute_plan の結果。"""

    created: List[ArasujiEntry] = field(default_factory=list)
    skipped_duplicates: int = 0
    cancelled: bool = False

    @property
    def created_count(self) -> int:
        return len(self.created)


def _record_llm_usage(client, persona_id: Optional[str], node_type: str) -> None:
    """LLM usage 記録 (generator._record_llm_usage を共用 — 失敗しても止めない)。"""
    from sai_memory.arasuji.generator import _record_llm_usage as _impl
    _impl(client, persona_id, node_type)


def _is_already_compiled(conn: sqlite3.Connection, first_source_id: str) -> bool:
    """チャンク先頭 message が既に一次あらすじの source に含まれているか (重複再検査)。

    M1 claim (プロセス間) と adapter._db_lock (プロセス内) の背後の三段目 —
    台帳の無い degrade 環境で同一 plan が並走したときの防御 (W4 D4)。plan は
    同一入力から決定論に作られるため、再実行のチャンク境界は一致し、先頭
    id の一致検査で過不足なく skip できる。
    """
    row = conn.execute(
        "SELECT 1 FROM arasuji_entries, json_each(source_ids_json) "
        "WHERE level = 1 AND json_each.value = ? LIMIT 1",
        (first_source_id,),
    ).fetchone()
    return row is not None


def _build_batch_prompt(
    chunk: PlannedChunk,
    conn: sqlite3.Connection,
    *,
    include_timestamp: bool,
    memopedia_context: Optional[str],
) -> str:
    """llm_batch チャンクの圧縮プロンプト (現行 Lv1 General 版の後継)。"""
    from sai_memory.arasuji.context import get_episode_context_for_timerange
    from sai_memory.arasuji.generator import _format_messages_for_prompt

    start_time = min(m.created_at for m in chunk.messages)
    end_time = max(m.created_at for m in chunk.messages)
    context = get_episode_context_for_timerange(
        conn, start_time=start_time, end_time=end_time, max_entries=20,
    )
    conversation = _format_messages_for_prompt(
        chunk.messages, include_timestamp=include_timestamp,
    )

    parts = [
        "あなたは記憶の記録者です。以下の会話から、出来事のあらすじを書いてください。",
        "",
    ]
    if context:
        parts.extend(["## これまでの流れ（参考）", context, ""])
    if memopedia_context:
        parts.extend(["## 意味記憶（人物・用語の背景情報）", memopedia_context, ""])
    parts.extend([
        "## 今回記録する会話",
        conversation,
        "",
        "## 指示",
        "- 3〜5文程度で、何が起きたか、誰と何を話したかを要約",
        "- 時系列の流れがわかるように書く",
        "- 固有名詞や重要な詳細は保持する",
        "- 感情や雰囲気も含める",
        "- 「〜について話した」のような抽象的な記述は避け、具体的に書く",
        "- [作業のまとめ] と印の付いた項目は、既に要約された作業の記録です。"
        "発言として引用せず、出来事の流れの一部として織り込む",
        "- **日時情報（【2025-01-07 23:56 ~】など）は書かないでください**（自動で付与されます）",
        "- **「あらすじ」などの見出しは書かないでください**（本文のみ出力）",
        "",
        "あらすじを日本語で書いてください。",
    ])
    return "\n".join(parts)


def _chunk_content(
    chunk: PlannedChunk,
    client,
    conn: sqlite3.Connection,
    *,
    persona_id: Optional[str],
    include_timestamp: bool,
    memopedia_context: Optional[str],
) -> str:
    """チャンクの content を得る (常に LLM 圧縮 — 小さくても要約する)。"""
    prompt = _build_batch_prompt(
        chunk, conn,
        include_timestamp=include_timestamp,
        memopedia_context=memopedia_context,
    )
    from llm_clients.exceptions import LLMError
    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        _record_llm_usage(client, persona_id, "chronicle_level1")
    except LLMError as exc:
        # 現行 generator と同じ契約: LLMError は文脈を付けて propagate
        # (frontend がバッチナビゲーションに使う)。
        exc.user_message = (
            f"メッセージ {len(chunk.messages)} 件のチャンク処理中: "
            f"{exc.user_message}"
        )
        exc.batch_meta = {
            "message_ids": chunk.message_ids,
            "start_time": min(m.created_at for m in chunk.messages),
            "end_time": max(m.created_at for m in chunk.messages),
        }
        raise
    content = (response or "").strip()
    if not content:
        raise ChunkExecutionError(
            f"empty LLM response for chunk ({len(chunk.messages)} messages)"
        )
    return content


def execute_plan(
    plan: AlignmentPlan,
    client,
    conn: sqlite3.Connection,
    *,
    persona_id: Optional[str] = None,
    include_timestamp: bool = True,
    memopedia_context: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    batch_callback: Optional[Callable[[List, Optional[str]], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> ExecutionResult:
    """整列計画を実行して一次あらすじを確定する。

    Args:
        plan: plan_alignment の出力。
        client: LLM client (llm_batch チャンクでのみ使用)。
        conn: persona memory.db の connection。
        progress_callback: callback(processed_messages, total_messages)。
        batch_callback: callback(messages, entry_id)。Fragment 抽出
            (entity_extractor) 用。全チャンクで呼ぶ (現設計は全チャンクが
            LLM 圧縮 — 恒等圧縮の廃止で抽出の発火点はここに一本化された)。
        cancel_check: True を返すと以降のチャンクを中断 (確定済みは残る)。

    Returns:
        ExecutionResult (created は確定順)。

    Raises:
        LLMError / ChunkExecutionError: チャンク失敗。確定済みチャンクは
        残る (再試行は processed_ids 再計算と重複再検査で冪等)。
    """
    result = ExecutionResult()
    total = plan.total_unprocessed
    processed = 0

    for chunk in plan.chunks:
        if cancel_check and cancel_check():
            LOGGER.info(
                "[executor] chunk execution cancelled (%d/%d chunks done)",
                len(result.created), len(plan.chunks),
            )
            result.cancelled = True
            break

        if progress_callback:
            progress_callback(processed, total)

        if not chunk.messages:
            continue

        # 事前の重複検査 (LLM コストの節約 — 整合性の防御は下の tx 内検査)。
        if _is_already_compiled(conn, chunk.messages[0].id):
            LOGGER.warning(
                "[executor] chunk skipped: first source already compiled "
                "(first_id=%s kind=%s size=%d)",
                chunk.messages[0].id, chunk.kind, len(chunk.messages),
            )
            result.skipped_duplicates += 1
            processed += len(chunk.messages)
            continue

        # LLM (tx 外) → チャンク単一 tx で確定。
        content = _chunk_content(
            chunk, client, conn,
            persona_id=persona_id,
            include_timestamp=include_timestamp,
            memopedia_context=memopedia_context,
        )
        try:
            # tx 内再検査 (Codex W4 #1 / 二巡 #1 / 三巡 #1): BEGIN IMMEDIATE で
            # write ロックを先に取ってから検査する — sqlite3 は SELECT では
            # 暗黙 BEGIN を張らないため、明示的にロックを取らないと検査と
            # INSERT の間に別コネクションが commit できてしまう。既に tx 内
            # (呼び出し元が開いた tx) なら参加する。それ以外の失敗
            # (database is locked = busy_timeout 超過) は握り潰さず raise —
            # ロック無しで検査に進むと原子化が無効になる。
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            if _is_already_compiled(conn, chunk.messages[0].id):
                conn.rollback()
                LOGGER.warning(
                    "[executor] chunk skipped in-tx: first source compiled "
                    "concurrently (first_id=%s kind=%s)",
                    chunk.messages[0].id, chunk.kind,
                )
                result.skipped_duplicates += 1
                processed += len(chunk.messages)
                continue
            entry = create_entry(
                conn,
                level=1,
                content=content,
                source_ids=chunk.message_ids,
                start_time=min(m.created_at for m in chunk.messages),
                end_time=max(m.created_at for m in chunk.messages),
                source_count=len(chunk.messages),
                message_count=len(chunk.messages),
                extra_metadata={
                    "digest_origin": chunk.kind,
                    "coverage_chars": chunk.coverage_chars,
                    "episode_refs": chunk.episode_refs,
                },
                commit=False,
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

        result.created.append(entry)
        processed += len(chunk.messages)
        LOGGER.info(
            "[executor] chunk committed: kind=%s messages=%d coverage=%d "
            "episodes=%s entry=%s",
            chunk.kind, len(chunk.messages), chunk.coverage_chars,
            ",".join(chunk.episode_refs) or "-", entry.id[:8],
        )

        if batch_callback:
            try:
                batch_callback(chunk.messages, entry.id)
            except Exception:
                LOGGER.exception(
                    "[executor] batch_callback failed (entry=%s); continuing",
                    entry.id[:8],
                )

    if progress_callback and not result.cancelled:
        progress_callback(processed, total)
    return result
