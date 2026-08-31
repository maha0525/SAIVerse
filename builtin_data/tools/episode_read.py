"""episode_read: 出来事 (episode:N) の当時の記録を全文で読み返すスペル。

体験の構造 (docs/intent/experience_structure.md §3.2) の「読み口 (unfold) は
origin_episode 層0タグで生ログまで到達する」の原始関数。digest 統合
(judgment_points.md §6 改定 / W1 Chunk C D10) で保存側の状況テキストから
セッション原本を落とした代わりに、この読み口が全文到達手段になる —
**切り詰めなし** (feedback_no_mechanical_truncation_design: 上限は全文到達
手段とセットが原則で、これ自体がその手段)。

原本 = messages.origin_episode 列 (層0タグの専用列) で引ける、当該出来事の
間に保存された全行 (volatile 含む — 作業セッションの生ログはセッションと共に
文脈から消えるが、memory.db には残っている)。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema

LOGGER = logging.getLogger("saiverse.tools.episode_read")

# episode の kind 表示名 (judgment_finalize._EPISODE_KIND_LABELS と同語彙)。
_KIND_LABELS = {
    "conversation": "会話",
    "work_session": "作業セッション",
    "slot": "コマ",
    "presence": "在室",
    "stroll": "散策",
    "other": "その他",
}


def _fmt_epoch(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> Optional[str]:
    """epoch 秒 → 表示用時刻文字列 (不正値は None)。"""
    if not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return None


def _speaker_label(role: str, persona_name: str) -> str:
    if role in ("assistant", "model"):
        return persona_name
    if role == "system":
        return "システム"
    if role == "user":
        return "ユーザー"
    return role or "?"


def episode_read(episode: str) -> str:
    """出来事の参照 (episode:N) から、当時の記録の原本を全文で返す。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("episode_read requires an active persona context")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("episode_read requires an active manager context")

    ref = str(episode or "").strip()
    if not ref:
        return (
            "出来事の参照が指定されていません。episode:N の形式で指定してください。"
        )

    from saiverse import episodes as episodes_mod

    try:
        ep = episodes_mod.get_by_ref(manager, persona_id, ref)
    except episodes_mod.EpisodeNotFoundError:
        return f"出来事 {ref} は見つかりませんでした。"

    episode_ref = ep.get("episode_ref") or ref
    kind_label = _KIND_LABELS.get(str(ep.get("kind") or ""), str(ep.get("kind") or "?"))
    meta = ep.get("meta") if isinstance(ep.get("meta"), dict) else {}
    title = str(meta.get("title") or "").strip()
    started = _fmt_epoch(ep.get("started_at")) or "?"
    ended = _fmt_epoch(ep.get("ended_at")) or (
        "(進行中)" if ep.get("status") == "open" else "?"
    )

    header: List[str] = [f"出来事 {episode_ref}（{kind_label}）"]
    if title:
        header.append(f"表題: {title}")
    header.append(f"期間: {started} 〜 {ended}")

    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    fetch = getattr(adapter, "get_messages_by_origin_episode", None)
    if not callable(fetch):
        return "\n".join(header + [
            "", "記憶データベースからこの出来事の記録を取得できませんでした。",
        ])
    try:
        rows: List[Dict[str, Any]] = fetch(str(episode_ref))
    except Exception:
        LOGGER.exception(
            "[episode_read] transcript fetch failed (persona=%s episode=%s)",
            persona_id, episode_ref,
        )
        return "\n".join(header + [
            "", "記憶データベースからこの出来事の記録を取得できませんでした。",
        ])
    if not rows:
        return "\n".join(header + [
            "", "この出来事の記録は残っていません。",
        ])

    persona_name = str(getattr(persona, "persona_name", None) or persona_id)
    body: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        speaker = _speaker_label(str(row.get("role") or ""), persona_name)
        stamp = _fmt_epoch(row.get("created_at"), "%H:%M")
        prefix = f"[{stamp}] " if stamp else ""
        body.append(f"{prefix}{speaker}:\n{content}")
    if not body:
        return "\n".join(header + [
            "", "この出来事の記録は残っていません。",
        ])

    # 全文レンダリング — 切り詰めなし (これ自体が全文到達手段)。
    return "\n".join(header) + "\n\n" + "\n\n".join(body)


def schema() -> ToolSchema:
    return ToolSchema(
        name="episode_read",
        description=(
            "出来事の参照 (episode:N) から、その出来事の間に記録された内容"
            "（発話・スペルの実行と結果）の全文を読み返します。作業セッションの"
            "要約 (digest) の元になった原本を確認したいときに使ってください。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "episode": {
                    "type": "string",
                    "description": "出来事の参照 (例: episode:12)",
                },
            },
            "required": ["episode"],
        },
        result_type="string",
        spell=True,
        spell_display_name="出来事の記録を読む",
    )
