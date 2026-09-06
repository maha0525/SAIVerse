"""コンテキストプレビューの知覚バッジ (2026-09-06 まはー裁定)。

実機では 36,000 字の提示済み知覚バッチが 137 通中 117 番目に無印で混ざり、
ユーザーが見つけられなかった。裁定は「単に見やすくなるだけ」に絞る:

- 送る中身・順序・節 (section) の分類・トークン集計は**一切変えない**
  (section="history" のまま。新しい節も集計の行も作らない — 知覚の量の勘定は
  水位管理が既に持っており、二冊目の帳簿を作らない)。
- 履歴の中のその一通に、その場で印が付くだけ — ``preview_context`` の
  annotated_messages に ``perception_batch`` (提示済みの知覚)、部屋の記帳
  (room_state_json) を持つバッチには ``room_state`` (部屋の様子) が真で載る。
- content は 1 バイトも変えない (提示の描画が変わるとキャッシュの前方一致が
  割れる)。印は metadata / annotated_messages のフィールドのみで、metadata は
  送信時に message preparer が落とすので LLM へは渡らない — ここでも検算する。
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import unittest
from types import SimpleNamespace

from llm_clients.openai_message_preparer import prepare_openai_messages
from sai_memory.perception_buffer import (
    create_consumption_batch,
    init_perception_buffer_table,
    list_pending,
    push_perception,
)
from sai_memory.room_state import render_room_full, room_key
from sea.runtime_context import (
    _perception_block_text,
    list_presented_perception_blocks,
    merge_perception_blocks,
    preview_context,
)

#: 実在の組み込みモデル (get_model_provider / get_context_length が引ける名前)。
_MODEL = "gemini-2.5-flash"

#: プレビューが集計に使う既存の節の全名前 (新しい節を作らない、の検算に使う)。
_KNOWN_SECTIONS = {
    "system_prompt", "memory_weave_chronicle", "memory_weave_memopedia",
    "memory_weave", "visual_context", "history", "realtime_context",
    "perception_buffer", "user_message", "attachments",
}


def _room_bundle(body_lines, *, building="b1", name="工房", key_id="1"):
    """開いたドキュメント 1 個を持つ部屋の束 (test_perception_presentation_cap と同形)。"""
    label = f"[item:{key_id}] [Document] 覚え書き"
    return {
        "building_id": building,
        "building_name": name,
        "packages": [{
            "key": f"item:{key_id}", "family": "item", "label": label,
            "lines": [label, "(Open)", "```", *body_lines, "```"],
            "media": [], "state": "open",
        }],
    }


class _PreviewRuntime:
    """``preview_context`` が呼ぶ口だけ持つ runtime (知覚の組成は本物を通す)。

    ``_prepare_context`` は実経路 (``_merge_consumed_perceptions``) と同じ二枚
    (:func:`list_presented_perception_blocks` + :func:`merge_perception_blocks`)
    を同じ引数 (プレビューは ``advance_cutoff=False``) で繋ぐ。
    """

    session_lifecycle = None  # Chronicle 有効相当 (判定不能 → 隠さない側)

    def __init__(self, recent):
        self._recent = list(recent)

    def _load_playbook_for(self, *args, **kwargs):
        return None

    def _choose_playbook(self, **kwargs):
        return None

    def _get_cache_kwargs(self):
        return {}

    def _prepare_context(self, persona, building_id, user_input=None,
                         warnings=None, preview_only=False):
        blocks = list_presented_perception_blocks(
            self, persona, self._recent, raise_on_error=True,
            advance_cutoff=not preview_only,
        )
        merged = merge_perception_blocks(self._recent, blocks)
        return [{"role": "system", "content": "システムプロンプト"}] + merged


class PreviewPerceptionBadgeTest(unittest.TestCase):
    """提示済みの知覚バッチ (部屋の記帳つき/なし) を実 fixture で組んで検査する。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)
        self.addCleanup(self.conn.close)
        self.persona = SimpleNamespace(
            persona_id="p1",
            persona_name="テスト子",
            model=_MODEL,
            sai_memory=SimpleNamespace(
                conn=self.conn, _db_lock=threading.RLock(),
                is_ready=lambda: True,
            ),
        )
        self.clock = 1000
        # 部屋の記帳を持つバッチ (room_state_json あり = 「部屋の様子」)。
        self.bundle = _room_bundle(["作業台に定規と写真がある。"] * 3)
        self.room_text = render_room_full(self.bundle)
        self.room_id = self._batch(self.room_text, room_state_json=json.dumps([{
            "key": room_key("b1"), "is_diff": False,
            "block": self.room_text, "snapshot": self.bundle,
        }], ensure_ascii=False))
        # 記帳なしの知覚バッチ (通知など)。
        self.plain_text = "「工房の扉が開いた」という通知が届いた。"
        self.plain_id = self._batch(self.plain_text)
        # 生ログ (知覚より古い会話 2 通)。
        self.recent = [
            {"role": "user", "content": "やあ", "created_at": 900},
            {"role": "assistant", "content": "こんにちは!", "created_at": 905},
        ]
        self.runtime = _PreviewRuntime(self.recent)

    def _batch(self, rendered_text, *, room_state_json=None):
        push_perception(self.conn, "world_state", "seed")
        pending = [it.id for it in list_pending(self.conn)]
        self.clock += 10
        return create_consumption_batch(
            self.conn, pending, consumed_at=self.clock,
            rendered_text=rendered_text, room_state_json=room_state_json,
        )

    def _preview(self):
        return preview_context(
            self.runtime, self.persona, "b1", "こんにちは",
        )

    def _rows(self, preview):
        rows = preview["messages"]
        room = next(r for r in rows if self.room_text in r["content"])
        plain = next(r for r in rows if self.plain_text in r["content"])
        conv = next(r for r in rows if r["content"] == "やあ")
        return rows, room, plain, conv

    # ---- 印そのもの (red の芯) ----

    def test_presented_batches_get_identification_fields(self):
        preview = self._preview()
        _, room, plain, conv = self._rows(preview)
        # 提示済みの知覚バッチには perception_batch が真で載る。
        self.assertIs(room.get("perception_batch"), True)
        self.assertIs(plain.get("perception_batch"), True)
        # 部屋の記帳を持つバッチにだけ room_state も真で載る。
        self.assertIs(room.get("room_state"), True)
        self.assertNotIn("room_state", plain)
        # 印の無い行 (普通の会話) にはキーごと載らない。
        self.assertNotIn("perception_batch", conv)
        self.assertNotIn("room_state", conv)

    # ---- 「見やすくなるだけ」の外側は不変 ----

    def test_sections_and_token_accounting_are_untouched(self):
        preview = self._preview()
        _, room, plain, conv = self._rows(preview)
        # 節の分類は history のまま (印は節を動かさない)。
        self.assertEqual(room["section"], "history")
        self.assertEqual(plain["section"], "history")
        self.assertEqual(conv["section"], "history")
        # 新しい節も集計の行も作らない。
        names = {s["name"] for s in preview["sections"]}
        self.assertLessEqual(names, _KNOWN_SECTIONS)
        # 知覚の行は history の勘定に載る (会話 2 + 知覚 2)。
        history = next(s for s in preview["sections"] if s["name"] == "history")
        self.assertEqual(history["message_count"], 4)
        # 集計の合計も節の合算のまま。
        self.assertEqual(
            sum(s["tokens"] for s in preview["sections"]),
            preview["total_input_tokens"],
        )

    def test_send_composition_content_is_unchanged_by_the_marks(self):
        """実送信の文面 (実組成の出力) は印の追加前後で不変 — content は素のまま。"""
        blocks = list_presented_perception_blocks(
            self.runtime, self.persona, self.recent, raise_on_error=True,
        )
        by_text = {b["content"]: b for b in blocks}
        # content は確定文面の <system> 包みそのもの (バッジの文言は混ざらない)。
        self.assertIn(_perception_block_text(self.room_text), by_text)
        self.assertIn(_perception_block_text(self.plain_text), by_text)

    def test_marks_never_reach_the_llm_payload(self):
        """metadata の印は message preparer が落とす — 印の有無で payload 不変。"""
        blocks = list_presented_perception_blocks(
            self.runtime, self.persona, self.recent, raise_on_error=True,
        )
        merged = merge_perception_blocks(self.recent, blocks)
        stripped = copy.deepcopy(merged)
        for msg in stripped:
            meta = msg.get("metadata")
            if isinstance(meta, dict):
                meta.pop("__room_state__", None)
        prepared_marked = prepare_openai_messages(merged, supports_images=False)
        prepared_stripped = prepare_openai_messages(stripped, supports_images=False)
        self.assertEqual(prepared_marked, prepared_stripped)
        for msg in prepared_marked:
            self.assertNotIn("metadata", msg)


if __name__ == "__main__":
    unittest.main()
