"""purpose_adopt: 候補を目的の木に接ぐ / 木に小目標を加える (P2c-1)。

**adopt = 木に接ぐ** — 候補を生むのは ``purpose_seed`` (別動詞、P2c-0 決定4)。
2 つの使い方がある (旧ツールとの 1:1 対応):

1. ``candidate_ref`` (task:N) を渡す — 既存の候補を**採用**して木に接ぐ
   (saiverse/purpose_tree.py の adopt。行はコピーせず段階と親だけ変わる)。
   ``parent_ref`` (track:N) を添えるとその枝の下に、省略すると第一階層に
   小さく立つ。
2. ``title`` + ``parent_ref`` (track:N) を渡す — その枝に新しい小目標を
   直接作る (旧 task_add の後継。既に木の中の営みに小目標を足す操作)。

詳細: docs/intent/persona_cognition/life_concept_map.md §3.1 (種・段階・位置)
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from database.session import SessionLocal
from saiverse import purpose_tree
from saiverse.track_manager import TrackManager, TrackNotFoundError
from tools.context import get_active_persona_id
from tools.core import ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)
# purpose_tree の関数群は「SessionLocal を持つ manager」を受ける。スペル層は
# world manager を持たないので、同じ factory を包んだ shim を渡す。
_manager = SimpleNamespace(SessionLocal=SessionLocal)


def purpose_adopt(
    candidate_ref: Optional[str] = None,
    title: Optional[str] = None,
    parent_ref: Optional[str] = None,
) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"

    has_candidate = bool(candidate_ref and str(candidate_ref).strip())
    has_title = bool(title and str(title).strip())
    if has_candidate == has_title:
        return (
            "Error: candidate_ref（既存候補の採用）と title（新しい小目標の作成）の"
            "どちらか一方だけを指定してください。"
        )

    if has_candidate:
        # --- 1. 既存候補の採用 (接ぎ木) ---
        try:
            node = purpose_tree.adopt(
                _manager, persona_id, str(candidate_ref).strip(),
                parent_ref=(str(parent_ref).strip() if parent_ref else None),
            )
        except (purpose_tree.NodeNotFoundError, purpose_tree.PurposeTreeError,
                ValueError) as exc:
            return f"Error: {exc}"
        where = f"{parent_ref} の下" if parent_ref else "第一階層"
        return f"候補を採用しました: {node['ref']}「{node['title']}」→ {where}"

    # --- 2. 新しい小目標を枝に直接作る (旧 task_add) ---
    if not parent_ref or not str(parent_ref).strip():
        return (
            "Error: 新しい小目標 (title) を作るには parent_ref（接ぎ先の枝、"
            "例: track:1）が必要です。候補として書き留めるだけなら "
            "purpose_seed を使ってください。"
        )
    try:
        track_id = _track_manager.resolve_track_ref(
            persona_id, str(parent_ref).strip()
        )
    except TrackNotFoundError as exc:
        return f"Error: {exc}"
    _track_manager.add_task(track_id, str(title).strip())
    return (
        f"小目標を追加しました: {title}\n\n"
        f"{_track_manager.format_task_list(track_id)}"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="purpose_adopt",
        description=(
            "目的の木に接ぎます（adopt = 接ぐ。候補を生むのは purpose_seed）。"
            "candidate_ref（task:N）を指定すると、書き留めてあった候補を採用して"
            "木に接ぎます — parent_ref（track:N）を添えるとその枝の下に、"
            "省略すると第一階層に立ちます。"
            "title と parent_ref（track:N）を指定すると、その枝に新しい小目標を"
            "直接作ります。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "candidate_ref": {
                    "type": "string",
                    "description": "採用する候補の参照（例: task:5）。title とは排他",
                },
                "title": {
                    "type": "string",
                    "description": "新しく作る小目標の題名。candidate_ref とは排他",
                },
                "parent_ref": {
                    "type": "string",
                    "description": (
                        "接ぎ先の枝の参照（例: track:1）。candidate_ref のときは任意"
                        "（省略で第一階層）、title のときは必須"
                    ),
                },
            },
        },
        result_type="string",
        spell=True,
        spell_display_name="目的の木に接ぐ",
    )
