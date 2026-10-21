"""画像だけの発言 (本文 ""、添付あり) が、受付からペルソナに送る形まで届くこと。

2026-10-14 まで、UI は画像だけの発言を送らせるのに、受付
(manager/runtime.py の handle_user_input_stream) が本文の空を ``empty_message``
で断り、どのペルソナにも届いていなかった。受付の先にも同じ「本文が空なら
捨てる」が、建物の記録からペルソナの記憶への取り込み
(builtin_data/tools/get_building_messages.py の _transcribe_message) と、
xAI / OpenAI 互換の組み立てにあった。

ここでは、受付が保存した発言をそのまま隔離 DB (メモリ上の SQLite) に入れ、
取り込みで偽の記憶に写し、写った発言を Grok / OpenAI 互換に送る形へ組む。
合成の PNG だけを使い、LLM は呼ばない。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from builtin_data.tools.get_building_messages import auto_ingest_building_messages
from database.building_messages import insert_building_message
from database.models import Base
from llm_clients.openai_message_preparer import prepare_openai_messages
from llm_clients.xai import _build_xai_messages
from manager.runtime import RuntimeService
from persona.history_manager import HistoryManager
from saiverse.media_utils import describe_attachments, has_attachments
from saiverse.user_conversation import _utterance_text_for_judgment

BID = "room"
LISTENER = "listener"


@pytest.fixture(autouse=True)
def no_real_summary():
    # 画像の説明の生成は LLM を呼ぶので、どの経路から届いても固定の文で返す
    with patch(
        "saiverse.media_summary.ensure_image_summary", return_value="[合成] 画像の説明",
    ) as mocked, patch("llm_clients.gemini.ensure_image_summary", new=mocked):
        yield mocked


@pytest.fixture
def png_path(tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGB", (8, 8), (0, 128, 0)).save(path, format="PNG")
    return path


def _image_metadata(path: Path) -> dict:
    # /chat/send (api/routes/chat.py) が画像の添付に書く形
    return {
        "images": [{
            "uri": str(path),
            "path": str(path),
            "mime_type": "image/png",
            "item_id": "item-1",
            "item_name": "photo.png",
        }],
    }


def _runtime(responders) -> RuntimeService:
    # tests/test_user_utterance_durability.py の _runtime と同じ組み方
    service = RuntimeService.__new__(RuntimeService)
    service.state = SimpleNamespace(user_id="user_1", user_current_building_id=BID)
    service.SessionLocal = MagicMock(name="SessionLocal")
    service.personas = {}
    service.occupants = {}
    service.building_map = {}
    service._canonical_building_id = lambda building_id: building_id
    service._build_responding_personas = lambda building_id: list(responders)
    service._save_modified_buildings = lambda: None
    service.manager = SimpleNamespace(
        _active_stop_events={},
        _active_sse_callbacks={},
        pulse_dispatcher=MagicMock(),
        cancel_active_generation=MagicMock(),
    )
    return service


def _events(lines) -> list:
    return [json.loads(line) for line in lines if line.strip()]


class _FakeMemory:
    """memory.db の代役。写された発言をそのまま控える。"""

    def __init__(self):
        self.appended = []

    def is_ready(self):
        return True

    def append_persona_message(self, message, **_kw):
        self.appended.append(message)
        return str(len(self.appended))

    def find_message_by_building_ref(self, ref):
        return None


def _listener(session_factory, memory):
    hm = HistoryManager(
        persona_id=LISTENER,
        persona_log_path=Path("personas") / LISTENER / "log.json",
        building_memory_paths={},
        initial_persona_history=[],
        db_session_factory=session_factory,
        memory_adapter=memory,
    )
    persona = SimpleNamespace(
        persona_id=LISTENER,
        current_building_id=BID,
        history_manager=hm,
        pulse_cursors={BID: 0},
        entry_markers={},
        buildings={},
        sai_memory=memory,
        id_to_name_map={},
    )
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        id_to_name_map={},
        personas={LISTENER: persona},
        all_personas={LISTENER: persona},
    )
    return persona, manager


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autocommit=False, autoflush=False)
    engine.dispose()


# --- 受付 -------------------------------------------------------------------

def test_reception_accepts_an_image_only_utterance(png_path):
    persona = SimpleNamespace(persona_id=LISTENER)
    service = _runtime([persona])
    metadata = _image_metadata(png_path)

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"message_id": "room:1", "_was_inserted": True},
    ) as insert:
        events = _events(service.handle_user_input_stream(
            "", metadata=metadata, building_id=BID, client_message_id="cmd-1",
        ))

    assert not any(e.get("error_code") == "empty_message" for e in events)
    saved_entry = insert.call_args.args[2]
    assert saved_entry["content"] == ""
    assert saved_entry["metadata"]["images"][0]["path"] == str(png_path)
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_called_once()


def test_reception_still_refuses_an_utterance_with_nothing_in_it():
    service = _runtime([SimpleNamespace(persona_id=LISTENER)])

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
    ) as insert:
        events = _events(service.handle_user_input_stream(
            "  ", metadata={}, building_id=BID,
        ))

    assert any(e.get("error_code") == "empty_message" for e in events)
    insert.assert_not_called()


def test_non_streaming_reception_accepts_an_image_only_utterance(png_path):
    service = _runtime([SimpleNamespace(persona_id=LISTENER)])

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"message_id": "room:1", "_was_inserted": True},
    ):
        replies = service.handle_user_input("", metadata=_image_metadata(png_path))

    assert not any("入力が空でした" in reply for reply in replies)
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_called_once()


def test_non_streaming_reception_still_refuses_an_utterance_with_nothing_in_it():
    service = _runtime([SimpleNamespace(persona_id=LISTENER)])

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
    ) as insert:
        replies = service.handle_user_input("", metadata={"images": []})

    assert any("入力が空でした" in reply for reply in replies)
    insert.assert_not_called()


# --- 受付 → 建物の記録 → 記憶 → 送る形 ----------------------------------------

def test_image_only_utterance_reaches_the_llm_payload(png_path, session_factory):
    # 1. 受付が保存する発言を、そのまま取り出す
    service = _runtime([SimpleNamespace(persona_id=LISTENER)])
    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"message_id": "room:1", "_was_inserted": True},
    ) as insert:
        list(service.handle_user_input_stream(
            "", metadata=_image_metadata(png_path), building_id=BID,
        ))
    saved_entry = dict(insert.call_args.args[2])
    assert saved_entry["role"] == "user"
    assert LISTENER in saved_entry["heard_by"]

    # 2. 建物の記録 (隔離 DB) に入れ、取り込みでペルソナの記憶へ写す
    insert_building_message(session_factory, BID, saved_entry)
    memory = _FakeMemory()
    persona, manager = _listener(session_factory, memory)
    count = auto_ingest_building_messages(persona, manager)

    assert count == 1
    remembered = memory.appended[0]
    assert remembered["role"] == "user"
    assert remembered["content"] == ""
    assert remembered["metadata"]["images"][0]["path"] == str(png_path)

    # 3. 写った発言を、そのまま Grok / OpenAI 互換に送る形へ組む
    history = [{
        "role": remembered["role"],
        "content": remembered["content"],
        "metadata": remembered["metadata"],
    }]
    xai_messages = _build_xai_messages(history, supports_images=True)
    assert len(xai_messages) == 1
    assert [p.WhichOneof("content") for p in xai_messages[0].content] == ["image_url"]

    openai_messages = prepare_openai_messages(history, supports_images=True)
    assert len(openai_messages) == 1
    assert [p["type"] for p in openai_messages[0]["content"]] == ["image_url"]


def test_ingest_still_skips_an_empty_message_without_attachments(session_factory):
    insert_building_message(session_factory, BID, {
        "role": "user", "content": "", "heard_by": [LISTENER],
        "timestamp": "2026-10-14T12:00:00+00:00",
    })
    memory = _FakeMemory()
    persona, manager = _listener(session_factory, memory)

    assert auto_ingest_building_messages(persona, manager) == 0
    assert memory.appended == []


# --- 応じるかの判断に見せる文 -------------------------------------------------

def test_judgment_text_says_what_was_sent_when_there_is_no_text(png_path):
    event = {"role": "user", "content": "", "metadata": _image_metadata(png_path)}
    assert _utterance_text_for_judgment(event) == "(文章は無く、画像 1 件を送ってきました)"


def test_judgment_text_is_the_text_itself_when_there_is_text(png_path):
    event = {"role": "user", "content": "見て", "metadata": _image_metadata(png_path)}
    assert _utterance_text_for_judgment(event) == "見て"


# --- 添付の判定 ---------------------------------------------------------------

def test_attachment_kinds_written_by_chat_send_are_all_counted():
    metadata = {
        "images": [{"path": "a.png"}],
        "documents": [{"path": "b.txt"}],
        "media": [
            {"type": "audio", "path": "c.ogg"},
            {"mime_type": "video/mp4", "path": "d.mp4"},
        ],
    }
    assert has_attachments(metadata)
    assert describe_attachments(metadata) == "画像 1 件、ドキュメント 1 件、音声 1 件、動画 1 件"


def test_odd_media_entries_do_not_break_the_reception_check():
    # 受付の判定で例外が出ると発言ごと失敗するので、型の崩れた項目は数えずに進む
    metadata = {"media": [{"type": 3}, {"mime_type": None}, "x", {"type": "audio", "path": "v.ogg"}]}
    assert has_attachments(metadata)
    assert describe_attachments(metadata) == "音声 1 件"


def test_no_attachments_means_empty():
    for metadata in (None, {}, {"images": []}, {"media": "x"}, {"tags": ["conversation"]}):
        assert not has_attachments(metadata)
        assert describe_attachments(metadata) == ""


# --- 画像と音声を一緒に送った発言 ------------------------------------------------

def test_image_survives_when_sent_together_with_audio(png_path, tmp_path):
    # /chat/send は画像を images、音声を media に書く。media があるときに images を
    # 読まないと、画像が全プロバイダで消える (2026-10-14 まで実際に消えていた)。
    from saiverse.media_utils import iter_image_media

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"ogg")
    metadata = _image_metadata(png_path)
    metadata["media"] = [{"type": "audio", "path": str(audio), "mime_type": "audio/ogg"}]

    images = iter_image_media(metadata)
    assert [str(item["path"]) for item in images] == [str(png_path)]

    history = [{"role": "user", "content": "", "metadata": metadata}]
    xai_messages = _build_xai_messages(history, supports_images=True)
    assert "image_url" in [p.WhichOneof("content") for p in xai_messages[0].content]


def test_same_image_listed_in_both_places_is_sent_once(png_path):
    from saiverse.media_utils import iter_image_media

    metadata = _image_metadata(png_path)
    metadata["media"] = [{"type": "image", "path": str(png_path), "mime_type": "image/png"}]
    assert len(iter_image_media(metadata)) == 1


# --- 自動想起 ---------------------------------------------------------------------

def test_entity_recall_does_not_reuse_an_older_utterance_for_an_image_only_turn(png_path):
    # 最新のユーザー発言が画像だけのとき、前の発言を「いま言われたこと」として
    # エンティティ想起を引き直さない
    from sea.auto_recall import _last_user_utterance

    messages = [
        {"role": "user", "content": "アイフィの話をしよう"},
        {"role": "assistant", "content": "いいね"},
        {"role": "user", "content": "", "metadata": _image_metadata(png_path)},
    ]
    assert _last_user_utterance(messages) is None

    messages[-1]["content"] = "これ見て"
    assert _last_user_utterance(messages) == "これ見て"


def _enriched(messages):
    # 実際の道と同じく、文脈を組む関数で「添付アイテム作成」の文を足してから渡す
    from sea.runtime import SEARuntime

    return SEARuntime._enrich_history_with_attachments(object(), messages)


def test_entity_recall_ignores_the_attachment_note_on_an_image_only_turn(png_path):
    # 添付の名前が知っている名前でも、ユーザーが言った言葉ではないので想起しない
    from sea import auto_recall

    metadata = _image_metadata(png_path)
    metadata["images"][0]["item_name"] = "Alice.png"
    messages = _enriched([
        {"role": "user", "content": "今日はいい天気"},
        {"role": "assistant", "content": "そうだね"},
        {"role": "user", "content": "", "metadata": metadata},
    ])
    assert "Alice.png" in messages[-1]["content"]  # 機構の文が足されている

    assert auto_recall._last_user_utterance(messages) is None
    rows = [("page_alice", 7, "Alice", "Alice の要約")]
    with patch("sea.auto_recall._fetch_memopedia_titles", return_value=rows), \
            patch("sai_memory.unified_recall.unified_recall", return_value=[]):
        result = auto_recall.run_auto_recall(
            conn=object(), embedder=object(), messages=messages,
            persona_id="p", thread_id="p:user_conversation",
        )
    assert not result.injected


def test_entity_recall_reads_only_what_was_said_when_text_comes_with_an_image(png_path):
    from sea import auto_recall

    metadata = _image_metadata(png_path)
    metadata["images"][0]["item_name"] = "Alice.png"
    messages = _enriched([{"role": "user", "content": "これ見て", "metadata": metadata}])

    assert auto_recall._last_user_utterance(messages) == "これ見て"


def test_same_image_written_differently_is_still_sent_once(png_path):
    # 同じファイルを相対パスと絶対パスで両方の置き場に書いても、一枚として扱う
    import os

    from saiverse.media_utils import describe_attachments, iter_image_media

    metadata = _image_metadata(png_path)
    metadata["media"] = [{
        "type": "image", "mime_type": "image/png",
        "path": os.path.relpath(str(png_path)),
    }]
    assert len(iter_image_media(metadata)) == 1
    assert describe_attachments(metadata) == "画像 1 件"


# --- 画像だけのターンの、埋め込み検索の検索語 -----------------------------------

def _image_only_turn(png_path):
    from sea.eviction_plan import CONSUMED_PERCEPTION_KEY

    metadata = _image_metadata(png_path)
    metadata["images"][0]["summary"] = "緑の四角の画像"
    return _enriched([
        {"role": "user", "content": "アイフィの話をしよう"},
        {"role": "assistant", "content": "いいね、アイフィについて話そう"},
        # 画像を送る前のターンに目に入った部屋の様子
        {"role": "user", "content": "<system>部屋に図書館の机がある</system>",
         "metadata": {CONSUMED_PERCEPTION_KEY: True}},
        {"role": "user", "content": "", "metadata": metadata},
    ])


def test_image_only_turn_does_not_search_with_older_text_or_room_view(png_path, monkeypatch):
    from sea import auto_recall

    monkeypatch.delenv("SAIVERSE_MEDIA_RECALL_ENABLED", raising=False)
    messages = _image_only_turn(png_path)

    assert auto_recall._seed_is_user_utterance(messages)
    assert auto_recall.build_query(messages) == ""
    assert auto_recall._fresh_observation_texts(messages) == []


def test_image_only_turn_searches_with_the_attachment_summary_when_media_recall_is_on(
    png_path, monkeypatch,
):
    from sea import auto_recall

    monkeypatch.setenv("SAIVERSE_MEDIA_RECALL_ENABLED", "1")
    assert auto_recall.build_query(_image_only_turn(png_path)) == "緑の四角の画像"


def test_reflex_judgment_material_says_an_image_was_sent(png_path):
    # 言葉の無い発言は会話本文に入らないので、反射判断の会話の末尾に機構の文で置く
    from sea import auto_recall

    state, _questions, _keys = auto_recall._build_reflex_request(
        _image_only_turn(png_path), [], context_messages=2,
    )
    conversation = state["conversation"]
    assert len(conversation) == 2
    assert conversation[-1] == {
        "role": "user", "text": "(文章は無く、画像 1 件を送ってきました)",
    }


# --- 受付の細部 -------------------------------------------------------------------

def test_whitespace_only_text_with_an_image_is_saved_as_no_text(png_path):
    service = _runtime([SimpleNamespace(persona_id=LISTENER)])
    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"message_id": "room:1", "_was_inserted": True},
    ) as insert:
        list(service.handle_user_input_stream(
            "  \n", metadata=_image_metadata(png_path), building_id=BID,
        ))
    assert insert.call_args.args[2]["content"] == ""


def test_odd_audio_entries_do_not_break_the_media_readers():
    from saiverse.media_utils import iter_audio_media, iter_video_media

    metadata = {"media": [{"type": 3}, {"mime_type": 5}, {"type": "audio"}]}
    assert iter_audio_media(metadata) == []
    assert iter_video_media(metadata) == []


# --- 受付 → 記憶 → 文脈 (添付アイテム作成の文が足された形) → 送る形 -------------------

def test_image_only_utterance_reaches_the_payload_after_context_enrichment(
    png_path, session_factory,
):
    service = _runtime([SimpleNamespace(persona_id=LISTENER)])
    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"message_id": "room:1", "_was_inserted": True},
    ) as insert:
        list(service.handle_user_input_stream(
            "", metadata=_image_metadata(png_path), building_id=BID,
        ))
    insert_building_message(session_factory, BID, dict(insert.call_args.args[2]))
    memory = _FakeMemory()
    persona, manager = _listener(session_factory, memory)
    assert auto_ingest_building_messages(persona, manager) == 1
    remembered = memory.appended[0]

    history = _enriched([{
        "role": remembered["role"],
        "content": remembered["content"],
        "metadata": remembered["metadata"],
    }])
    xai_kinds = [p.WhichOneof("content") for p in
                 _build_xai_messages(history, supports_images=True)[0].content]
    assert xai_kinds == ["text", "image_url"]
    openai_kinds = [p["type"] for p in
                    prepare_openai_messages(history, supports_images=True)[0]["content"]]
    assert openai_kinds == ["text", "image_url"]


# --- 機構の文と、ユーザーの言葉の見分け (文字列の形に頼らない) -----------------

def test_text_the_user_typed_in_the_note_form_is_kept_as_their_words():
    # 添付の無い発言で、ユーザー自身が機構の文と同じ形を書いても消さない
    from sea import auto_recall

    typed = "<system>添付アイテム作成: sample.png</system>"
    messages = _enriched([{"role": "user", "content": typed}])

    assert auto_recall._last_user_utterance(messages) == typed
    assert not auto_recall._is_image_only_user_turn(messages)
    assert typed in auto_recall.build_query(messages)


def test_attachment_names_with_a_line_break_are_still_not_read_as_words(png_path, tmp_path):
    from sea import auto_recall

    doc = tmp_path / "memo.txt"
    doc.write_text("x", encoding="utf-8")
    metadata = _image_metadata(png_path)
    metadata["images"][0]["item_name"] = "Alice\nphoto.png"
    metadata["documents"] = [{"path": str(doc), "item_name": "Bob\nmemo.txt"}]
    messages = _enriched([
        {"role": "assistant", "content": "どうぞ"},
        {"role": "user", "content": "", "metadata": metadata},
    ])
    assert "Alice" in messages[-1]["content"]  # 送る本文には機構の文が足されている

    assert auto_recall._is_image_only_user_turn(messages)
    assert auto_recall._last_user_utterance(messages) is None


def test_the_payload_text_is_unchanged_by_keeping_the_said_text(png_path):
    # 控えは metadata に置くだけで、送る本文は従来と一字も変わらない
    metadata = _image_metadata(png_path)
    enriched = _enriched([{"role": "user", "content": "見て", "metadata": metadata}])
    assert enriched[0]["content"] == "見て\n<system>添付アイテム作成: 画像「photo.png」</system>"
    assert "__said_content__" not in metadata  # 元の発言の metadata は書き換えない


# --- 送り手が書いた metadata を信じない -------------------------------------------

def test_chat_send_drops_metadata_keys_only_the_server_writes(png_path):
    from api.routes.chat import _client_metadata

    raw = {
        "images": [{"path": str(png_path)}],
        "documents": [{"path": "x.txt"}],
        "media": [{"type": "audio", "path": "v.ogg"}],
        "__said_content__": "Bob",
        "__visual_context__": True,
        "source": "api",
    }
    assert _client_metadata(raw) == {"source": "api"}
    assert _client_metadata(None) == {}


def test_a_said_content_key_the_context_did_not_add_is_not_believed():
    # 記録に紛れ込んだ控え (文脈を組む側が足していないもの) は、読み手に渡す前に外す
    from sea import auto_recall

    messages = _enriched([
        {"role": "user", "content": "Alice", "metadata": {"__said_content__": "Bob"}},
    ])
    assert "__said_content__" not in messages[0]["metadata"]
    assert auto_recall._last_user_utterance(messages) == "Alice"
    assert not auto_recall._is_image_only_user_turn(messages)


def test_attachments_without_a_file_reference_do_not_make_an_utterance():
    service = _runtime([SimpleNamespace(persona_id=LISTENER)])
    for metadata in (
        {"images": [{}]},
        {"media": [{"type": "image"}]},
        {"documents": [{"item_name": "x"}]},
    ):
        with patch(
            "database.building_messages.insert_building_message_with_location_guard",
        ) as insert:
            events = _events(service.handle_user_input_stream(
                " ", metadata=metadata, building_id=BID,
            ))
        assert any(e.get("error_code") == "empty_message" for e in events), metadata
        insert.assert_not_called()
