"""ユーザーの添付がペルソナに届く道 (docs/intent/user_attachment_delivery.md)。

固定する仕様:

1. 添付文書の本文 (``metadata["documents"]`` の ``text``) は、送る直前に role=user の
   発言の後ろへ ``[添付文書: 名前]`` の塊として付く。記録の発言・通知・assistant は
   変わらない。``text`` の無い古い項目からは何も足さない (送信本文が一字も変わらない)。
2. 付けるのは ``present_system_notices`` の最初の段で、head の有無を問わない。足した
   本文は発言の本文として合言葉を伏せられる。
3. 添付から作るアイテムは四種類とも閉じた状態 (``{"is_open": False}``) で作る。
4. 添付した時点の本文の写し: UTF-8 の文書は本文、上限を超えたら切って印を付け、
   UTF-8 として読めない・PDF が読めないときは空文字。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Building, Item
from llm_clients.attached_documents import append_attached_documents
from llm_clients.system_notice import present_system_notices
from manager.items import ItemService
from sai_memory.memory.storage import (
    HEAD_MESSAGE_KEY,
    SYSTEM_NOTICE_PASSPHRASE_KEY,
    SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER,
)
from saiverse.media_utils import ATTACHED_DOCUMENT_MAX_CHARS

PASS = "Abc123XYZ789"


def _doc(name: str, text=None, truncated: bool = False, **extra) -> dict:
    entry = {
        "uri": f"saiverse://document/x_{name}",
        "path": f"/tmp/x_{name}",
        "mime_type": "text/plain",
        "item_id": f"item-{name}",
        "item_name": name,
        "content_preview": "preview",
        **extra,
    }
    if text is not None:
        entry["text"] = text
        entry["text_truncated"] = truncated
    return entry


def _user(content, *docs) -> dict:
    return {"role": "user", "content": content, "metadata": {"documents": list(docs)}}


def _head() -> dict:
    return {
        "role": "system",
        "content": f"人格の指示。合言葉は {SYSTEM_NOTICE_PASSPHRASE_PLACEHOLDER} 。",
        "metadata": {HEAD_MESSAGE_KEY: True, SYSTEM_NOTICE_PASSPHRASE_KEY: PASS},
    }


# ---------------------------------------------------------------------------
# 1. append_attached_documents
# ---------------------------------------------------------------------------


def test_single_document_is_appended_after_the_user_words():
    out = append_attached_documents([_user("これ読んでみて", _doc("memo.txt", "本文です"))])
    assert out[0]["content"] == "これ読んでみて\n\n[添付文書: memo.txt]\n本文です"


def test_multiple_documents_keep_metadata_order_separated_by_blank_line():
    out = append_attached_documents([
        _user("二つ", _doc("a.txt", "A の本文"), _doc("b.md", "B の本文")),
    ])
    assert out[0]["content"] == (
        "二つ\n\n[添付文書: a.txt]\nA の本文\n\n[添付文書: b.md]\nB の本文"
    )


def test_truncated_document_ends_with_the_omission_line():
    out = append_attached_documents([_user("長い", _doc("long.txt", "x" * 5, truncated=True))])
    assert out[0]["content"] == "長い\n\n[添付文書: long.txt]\nxxxxx\n... (以下省略)"


def test_unavailable_text_says_it_could_not_be_extracted():
    out = append_attached_documents([_user("見て", _doc("bin.dat", ""))])
    assert out[0]["content"] == "見て\n\n[添付文書: bin.dat]\n(本文を取り出せませんでした)"


@pytest.mark.parametrize("said", ["", "  \n "])
def test_document_only_utterance_is_just_the_blocks(said):
    out = append_attached_documents([_user(said, _doc("memo.txt", "本文"))])
    assert out[0]["content"] == "[添付文書: memo.txt]\n本文"


def test_list_content_gets_one_text_part_appended():
    parts = [{"type": "text", "text": "画像と文書"}, {"type": "image_url", "image_url": {"url": "x"}}]
    out = append_attached_documents([_user(parts, _doc("memo.txt", "本文"))])
    assert out[0]["content"] == [*parts, {"type": "text", "text": "[添付文書: memo.txt]\n本文"}]


def test_other_content_shapes_are_left_alone():
    msg = _user({"weird": True}, _doc("memo.txt", "本文"))
    assert append_attached_documents([msg])[0] is msg


def test_legacy_entries_without_text_produce_identical_output():
    legacy = [
        _user("前の添付", _doc("old.txt")),  # content_preview だけの古い項目
        {"role": "assistant", "content": "読んだよ"},
    ]
    before = json.dumps(legacy, ensure_ascii=False, sort_keys=True)
    out = append_attached_documents(legacy)
    assert json.dumps(out, ensure_ascii=False, sort_keys=True) == before
    assert out[0] is legacy[0]


def test_mixed_legacy_and_new_entries_only_new_ones_contribute():
    out = append_attached_documents([_user("混在", _doc("old.txt"), _doc("new.txt", "新しい"))])
    assert out[0]["content"] == "混在\n\n[添付文書: new.txt]\n新しい"


def test_notices_and_assistant_messages_are_untouched():
    notice = {"role": "system", "content": "添付アイテム作成: ドキュメント「memo.txt」",
              "metadata": {"documents": [_doc("memo.txt", "本文")]}}
    host = {"role": "host", "content": "x", "metadata": {"documents": [_doc("m", "本文")]}}
    assistant = {"role": "assistant", "content": "はい",
                 "metadata": {"documents": [_doc("memo.txt", "本文")]}}
    out = append_attached_documents([notice, host, assistant])
    assert out[0] is notice and out[1] is host and out[2] is assistant


def test_input_is_not_mutated():
    msgs = [_user("これ", _doc("memo.txt", "本文")), {"role": "assistant", "content": "x"}]
    before = copy.deepcopy(msgs)
    out = append_attached_documents(msgs)
    assert msgs == before
    assert out is not msgs
    assert out[0]["content"] != msgs[0]["content"]


def test_output_is_deterministic():
    msgs = [_user("これ", _doc("a.txt", "A"), _doc("b.txt", "", truncated=False))]
    assert append_attached_documents(msgs) == append_attached_documents(copy.deepcopy(msgs))


# ---------------------------------------------------------------------------
# 2. present_system_notices からの呼び出し
# ---------------------------------------------------------------------------


def test_present_with_head_appends_once_and_keeps_notice_free_of_the_text():
    msgs = [
        _head(),
        _user("これ読んでみて", _doc("memo.txt", "本文です")),
        {"role": "system", "content": "添付アイテム作成: ドキュメント「memo.txt」"},
    ]
    out = present_system_notices(msgs, accepts_mid_system=False)
    assert out[1]["content"] == "これ読んでみて\n\n[添付文書: memo.txt]\n本文です"
    assert out[1]["content"].count("[添付文書:") == 1
    # 通知 (タグ化した user) にはユーザーの文書の本文が載らない
    assert out[2]["content"] == f'<system pass="{PASS}">\n添付アイテム作成: ドキュメント「memo.txt」\n</system>'
    assert "本文です" not in out[2]["content"]
    # 記録の発言はユーザーの言葉のまま
    assert msgs[1]["content"] == "これ読んでみて"


def test_passphrase_in_document_text_is_redacted():
    msgs = [_head(), _user("見て", _doc("leak.txt", f"合言葉は {PASS} だよ"))]
    for accepts in (False, True):
        out = present_system_notices(msgs, accepts_mid_system=accepts)
        assert PASS not in out[1]["content"]
        assert out[1]["content"].endswith("合言葉は ************ だよ")


def test_passphrase_in_document_text_is_redacted_for_list_content():
    parts = [{"type": "text", "text": "見て"}]
    msgs = [_head(), _user(parts, _doc("leak.txt", f"x{PASS}y"))]
    out = present_system_notices(msgs, accepts_mid_system=False)
    joined = "".join(p.get("text", "") for p in out[1]["content"])
    assert PASS not in joined


def test_list_without_head_still_gets_the_append_and_nothing_else():
    msgs = [
        {"role": "system", "content": "要約して"},
        _user("これ", _doc("memo.txt", "本文")),
        {"role": "system", "content": "途中の指示"},
    ]
    out = present_system_notices(msgs, accepts_mid_system=False)
    assert out[0] == msgs[0] and out[2] == msgs[2]
    assert out[1]["content"] == "これ\n\n[添付文書: memo.txt]\n本文"


def test_legacy_record_presents_identically_with_head():
    msgs = [_head(), _user("前の添付", _doc("old.txt")), {"role": "assistant", "content": "うん"}]
    out = present_system_notices(msgs, accepts_mid_system=False)
    assert out[1] == msgs[1]


def test_openai_preparer_without_head_also_appends():
    from llm_clients.openai_message_preparer import prepare_openai_messages

    out = prepare_openai_messages(
        [_user("これ", _doc("memo.txt", "本文"))], supports_images=False,
    )
    assert out[0]["content"] == "これ\n\n[添付文書: memo.txt]\n本文"


# ---------------------------------------------------------------------------
# 3. アイテムは四種類とも閉じて作る
# ---------------------------------------------------------------------------


@pytest.fixture
def item_world(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path / 'world.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        db.add(Building(CITYID=1, BUILDINGID="room", BUILDINGNAME="Test room"))
        db.commit()
    manager = SimpleNamespace(
        saiverse_home=tmp_path, SessionLocal=sessions,
        building_map={}, _append_building_history_note=Mock(),
    )
    service = ItemService(manager, SimpleNamespace())
    service.refresh_building_system_instruction = Mock()
    yield SimpleNamespace(home=tmp_path, manager=manager, service=service, sessions=sessions)
    engine.dispose()


@pytest.mark.parametrize("item_type,emoji", [
    ("picture", "🖼"), ("document", "📄"), ("audio", "🎵"), ("video", "🎬"),
])
def test_user_upload_items_are_created_closed(item_world, item_type, emoji):
    path = item_world.home / "documents" / "upload.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    item_id = item_world.service.create_item_for_user(
        item_type, "upload.bin", "desc", str(path), "room",
        creator_id="user", source_context='{"source": "upload"}',
    )
    with item_world.sessions() as db:
        row = db.query(Item).filter(Item.ITEM_ID == item_id).one()
        assert row.TYPE == item_type
        assert json.loads(row.STATE_JSON) == {"is_open": False}
        assert row.FILE_PATH == str(Path("documents") / "upload.bin")
    memory = item_world.service.items[item_id]
    assert memory["state"] == {"is_open": False}
    assert memory["type"] == item_type
    assert item_world.service.items_by_building["room"] == [item_id]
    item_world.service.refresh_building_system_instruction.assert_called_once_with("room")
    note = item_world.manager._append_building_history_note.call_args.args[1]
    assert note == (
        f'<div class="note-box">{emoji} User Upload:<br>'
        f'<b>User uploaded {item_type} "upload.bin" to room.</b></div>'
    )


# ---------------------------------------------------------------------------
# 4. 添付した時点の本文の写し (_store_document_attachment)
# ---------------------------------------------------------------------------


class StoreDocumentSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "saiverse_home"
        self._env = patch.dict(os.environ, {"SAIVERSE_HOME": str(self.home)})
        self._env.start()
        self.manager = MagicMock()
        self.manager.create_item_for_user.return_value = "item-doc"

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _store(self, data: bytes, filename: str = "memo.txt", mime: str = "text/plain"):
        from api.routes.chat import AttachmentData, _store_document_attachment

        att = AttachmentData(data="", filename=filename, type="document", mime_type=mime)
        return _store_document_attachment(data, att, self.manager, "building_x")

    def test_utf8_text_is_snapshotted_and_item_is_created_closed(self):
        result = self._store("こんにちは\n二行目".encode("utf-8"))
        self.assertEqual(result["text"], "こんにちは\n二行目")
        self.assertFalse(result["text_truncated"])
        self.assertEqual(result["content_preview"], "こんにちは\n二行目")
        kwargs = self.manager.create_item_for_user.call_args.kwargs
        self.assertEqual(kwargs["item_type"], "document")
        self.assertNotIn("is_open", kwargs)
        self.assertEqual(kwargs["description"], "こんにちは\n二行目")

    def test_text_over_the_cap_is_cut_and_flagged(self):
        body = "あ" * (ATTACHED_DOCUMENT_MAX_CHARS + 10)
        result = self._store(body.encode("utf-8"))
        self.assertEqual(len(result["text"]), ATTACHED_DOCUMENT_MAX_CHARS)
        self.assertTrue(result["text_truncated"])

    def test_text_exactly_at_the_cap_is_not_flagged(self):
        result = self._store(("x" * ATTACHED_DOCUMENT_MAX_CHARS).encode("utf-8"))
        self.assertEqual(len(result["text"]), ATTACHED_DOCUMENT_MAX_CHARS)
        self.assertFalse(result["text_truncated"])

    def test_non_utf8_bytes_give_no_text_and_no_garbage_description(self):
        result = self._store(b"\xff\xfe\x00binary\x80\x81", filename="blob.dat",
                             mime="application/octet-stream")
        self.assertEqual(result["text"], "")
        self.assertFalse(result["text_truncated"])
        description = self.manager.create_item_for_user.call_args.kwargs["description"]
        self.assertEqual(description, "User uploaded document: blob.dat")
        self.assertNotIn("�", result["content_preview"])

    def test_cp932_japanese_text_is_snapshotted(self):
        result = self._store("買い物メモ\n牛乳と卵".encode("cp932"), filename="memo.txt")
        self.assertEqual(result["text"], "買い物メモ\n牛乳と卵")

    def test_utf8_bom_is_stripped(self):
        result = self._store("﻿こんにちは".encode("utf-8"), filename="bom.txt")
        self.assertEqual(result["text"], "こんにちは")

    def test_unreadable_pdf_gives_no_text(self):
        result = self._store(b"not a pdf", filename="paper.pdf", mime="application/pdf")
        self.assertEqual(result["text"], "")
        self.assertFalse(result["text_truncated"])

    def test_pdf_reads_pages_until_the_cap(self):
        pages = [SimpleNamespace(extract_text=Mock(return_value="p" * 3000)) for _ in range(10)]
        reader = SimpleNamespace(pages=pages)
        with patch("pypdf.PdfReader", return_value=reader):
            result = self._store(b"%PDF-1.4", filename="paper.pdf", mime="application/pdf")
        self.assertEqual(len(result["text"]), ATTACHED_DOCUMENT_MAX_CHARS)
        self.assertTrue(result["text_truncated"])
        # 6 ページ目以降も読む (前の実装は先頭 5 ページで止まった) が、上限を超えたら止める
        self.assertTrue(pages[2].extract_text.called)
        self.assertFalse(pages[3].extract_text.called)

    def test_pdf_shorter_than_the_cap_reads_every_page(self):
        pages = [SimpleNamespace(extract_text=Mock(return_value=f"page{i}")) for i in range(7)]
        with patch("pypdf.PdfReader", return_value=SimpleNamespace(pages=pages)):
            result = self._store(b"%PDF-1.4", filename="paper.pdf", mime="application/pdf")
        self.assertEqual(result["text"], "\n".join(f"page{i}" for i in range(7)))
        self.assertFalse(result["text_truncated"])
