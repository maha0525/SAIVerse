"""音声だけのユーザー発言 (本文が空・音声の添付だけ) が、受付から LLM に送る形まで届くこと。

音声入力を中継するアドオンは、本文に案内文を添えず、本文を空にして音声だけを
送る (docs/intent/system_notice_delivery.md の追補「音声だけの発言」)。本体は
画像だけの発言 (tests/test_image_only_utterance_path.py) と同じ判定で受け付ける
はずなので、同じ三段 (受付 → 建物の記録から記憶への取り込み → 送る形) を音声で
通して、途中で発言が消えないことを固定する。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from builtin_data.tools.get_building_messages import auto_ingest_building_messages
from database.building_messages import insert_building_message
from llm_clients.base import LLMClient

from tests.test_image_only_utterance_path import (  # noqa: F401 (fixture)
    BID,
    LISTENER,
    _FakeMemory,
    _listener,
    _runtime,
    session_factory,
)


@pytest.fixture
def ogg_path(tmp_path) -> Path:
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"OggS" + b"\x00" * 60)
    return path


def _audio_metadata(path: Path) -> dict:
    # 音声入力を中継するアドオンが handle_user_input_stream に渡す形。アドオンは
    # 音声を SAIVerse の音声の置き場に保存して saiverse://audio/ の URI を書くが、
    # テストでは置き場の外の一時ファイルを使うので、URI にファイルの場所を入れる
    # (tests/test_image_only_utterance_path.py の画像と同じ)。
    return {
        "source": "test_voice",
        "media": [{
            "type": "audio",
            "uri": str(path),
            "mime_type": "audio/ogg",
            "path": str(path),
        }],
    }


def test_audio_only_utterance_reaches_the_llm_payload(ogg_path, session_factory):  # noqa: F811
    # 1. 受付が、本文の空な音声だけの発言を断らずに保存する
    service = _runtime([SimpleNamespace(persona_id=LISTENER)])
    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"message_id": "room:1", "_was_inserted": True},
    ) as insert:
        lines = list(service.handle_user_input_stream(
            "", metadata=_audio_metadata(ogg_path), building_id=BID,
        ))
    assert not any("empty_message" in line for line in lines)
    saved_entry = dict(insert.call_args.args[2])
    assert saved_entry["role"] == "user"
    assert saved_entry["content"] == ""

    # 2. 建物の記録から、ペルソナの記憶へ本文の空なまま音声つきで写る
    insert_building_message(session_factory, BID, saved_entry)
    memory = _FakeMemory()
    persona, manager = _listener(session_factory, memory)
    assert auto_ingest_building_messages(persona, manager) == 1
    remembered = memory.appended[0]
    assert remembered["role"] == "user"
    assert remembered["content"] == ""
    assert remembered["metadata"]["media"][0]["type"] == "audio"

    # 3. 音声を聴けないモデルには、音声の説明の文が本文になって届く
    #    (発言が空のまま消えない)
    history = [{
        "role": remembered["role"],
        "content": remembered["content"],
        "metadata": remembered["metadata"],
    }]
    client = LLMClient(supports_audio=False)
    with patch(
        "saiverse.media_summary.ensure_audio_summary", return_value="[合成] 音声の説明",
    ):
        sent = client._inject_unsupported_media_summaries(history)
    assert len(sent) == 1
    assert sent[0]["role"] == "user"
    assert "[合成] 音声の説明" in sent[0]["content"]
    assert ogg_path.name in sent[0]["content"]
