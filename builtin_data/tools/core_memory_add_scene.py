"""コア記憶 (記憶アーキv2 ゾーン A) に「実会話の切り抜き (scene)」を刻むスペル。

scene ＝口調・性格・ユーザーとの関係性を固定するアンカー。ペルソナらしさが最も
強く出た実際の会話を、原文のまま head に常駐させる。

**必ず「参照によるコピー」で取り込む** — LLM を一切経由しない決定論コピー。
ペルソナに本文を書き写させると言い換えドリフトで手本が劣化するため、これが
この機能の存在理由そのもの (docs/intent/memory_architecture_v2.md §5)。

詳細: docs/intent/memory_architecture_v2.md §5, §4.2
"""
from __future__ import annotations

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import (
    format_budget_note,
    parse_message_ref,
    resolve_core_memory_budget,
    resolve_persona_display_name,
)

DEFAULT_ROUNDS = 3


def core_memory_add_scene(message_id: str, rounds: int = DEFAULT_ROUNDS) -> str:
    """実際の会話をアンカーに、その前後を切り抜いてコア記憶に刻む。

    - message_id: アンカーとなるメッセージの ID。生の ID / ``saiverse://self/
      message/<id>`` URI 形式のどちらでも可 (自動想起のハンドルをそのまま渡せる)。
    - rounds: アンカー前後に含めるおおよその往復数 (既定 3)。
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    mid = parse_message_ref(message_id)
    if not mid:
        return f"Error: message_id を解釈できませんでした: {message_id}"

    try:
        rounds_int = int(rounds)
    except (TypeError, ValueError):
        rounds_int = DEFAULT_ROUNDS
    if rounds_int <= 0:
        rounds_int = DEFAULT_ROUNDS

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    # 窓切り出し＋トランスクリプト整形＋保存は sai_memory.core_memory の共通ロジック
    # に集約されている (スペルと REST API で共用、複製禁止)。
    from sai_memory.core_memory import create_scene_core_memory

    persona_name = resolve_persona_display_name(persona_id)

    with adapter._db_lock:
        result = create_scene_core_memory(
            adapter.conn, mid, rounds=rounds_int, persona_name=persona_name,
        )

    if result is None:
        return (
            f"Error: メッセージ {mid} が見つからないか、実会話ではありません"
            "（ツール実行ログ等は切り抜き対象外です）。"
        )

    budget = resolve_core_memory_budget(persona_id)
    note = format_budget_note(result.total_chars, budget)
    return (
        f"コア記憶 c:{result.memory_id}（会話の記憶）を追加しました。"
        f"{result.message_count} 発言分（{result.date_start}〜{result.date_end}）を含めました。"
        f"現在 {result.total_chars:,} 字 / 目安 {budget:,} 字。"
        f"head への反映は次の記憶整理（Metabolism）からです。{note}"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="core_memory_add_scene",
        description=(
            "実際にあった会話の一場面を、原文のままコア記憶に刻みます。"
            "口調・性格・ユーザーとの関係性を固定するアンカーとして使います。"
            "message_id で指定したメッセージを中心に、前後の会話を自動で"
            "そのまま複製します（言い換えは行いません）。"
            "message_id は自動想起で示されたハンドル（saiverse://self/message/... 形式）"
            "や生の ID をそのまま渡せます。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "切り抜きの中心にするメッセージの ID（URI 形式可）",
                },
                "rounds": {
                    "type": "integer",
                    "description": "アンカー前後に含めるおおよその往復数（既定 3）",
                },
            },
            "required": ["message_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="会話をコア記憶に刻む",
    )
