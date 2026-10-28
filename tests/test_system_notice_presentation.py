"""システム通知の提示の形 (docs/intent/system_notice_delivery.md「読み出しと messages 列の組み立て」)。

固定すること:

- 記録から読み出した role=system の行は、role=system・タグ無し本文のまま、
  識別の印 (``SYSTEM_NOTICE_KEY``) を含む metadata ごと messages 列に出る
  (以前は読み出しのときに ``<system>`` で囲んだ role=user に変えていた)。
- 返事と対で残した指示文 (paired_action_text) の展開も、role=system の
  システム通知として出る。
- 送る列に足す通知の組み立て (:func:`system_notice_message`) と判定
  (:func:`is_system_notice`) — head の system メッセージは通知ではない。

リアルタイム情報・自動想起を残す行の提示と記録の一字一致は
tests/test_persist_realtime_and_recall.py、音声通話の host 行は
tests/test_voice_call.py が見ている。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from sai_memory.memory.storage import (
    HEAD_MESSAGE_KEY,
    SYSTEM_NOTICE_KEY,
    is_system_notice,
    system_notice_message,
)

PERSONA_ID = "notice-presentation-tester"


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    with patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter

        persona_path = tmp_path / "personas" / PERSONA_ID
        persona_path.mkdir(parents=True)
        a = SAIMemoryAdapter(PERSONA_ID, persona_dir=persona_path, resource_id=PERSONA_ID)
        yield a
        try:
            a.close()
        except Exception:
            pass


def test_system_row_is_read_out_as_role_system_with_its_metadata(adapter):
    adapter.append_persona_message({
        "role": "user", "content": "おはよう",
        "timestamp": "2026-10-28T00:00:00+00:00",
    })
    adapter.append_persona_message({
        "role": "system", "content": "スケジュールの実行時刻です。",
        "timestamp": "2026-10-28T00:00:01+00:00",
        "metadata": {"with": ["system"], SYSTEM_NOTICE_KEY: "schedule"},
    })

    payloads = adapter.recent_persona_messages(10_000)

    assert [p["role"] for p in payloads] == ["user", "system"]
    notice = payloads[1]
    assert notice["content"] == "スケジュールの実行時刻です。"
    assert "<system" not in notice["content"]
    assert notice["metadata"][SYSTEM_NOTICE_KEY] == "schedule"
    assert is_system_notice(notice)


def test_paired_action_is_expanded_as_a_system_notice(adapter):
    adapter.append_persona_message({
        "role": "assistant", "content": "はい、手短に。",
        "timestamp": "2026-10-28T00:00:02+00:00",
        "paired_action_text": "手短に答えて。",
    })

    payloads = adapter.recent_persona_messages(10_000)

    assert [p["role"] for p in payloads] == ["system", "assistant"]
    action = payloads[0]
    assert action["content"] == "手短に答えて。"
    assert action["metadata"][SYSTEM_NOTICE_KEY] == "paired_action"
    assert "paired_action_text" not in payloads[1]


def test_system_notice_message_and_the_head_mark():
    notice = system_notice_message("本文", "tool_result", metadata={"media": []})
    assert notice == {
        "role": "system", "content": "本文",
        "metadata": {"media": [], SYSTEM_NOTICE_KEY: "tool_result"},
    }
    assert is_system_notice(notice)
    head = {"role": "system", "content": "人格", "metadata": {HEAD_MESSAGE_KEY: True}}
    assert not is_system_notice(head)
    # 印の無い system (head を組まない呼び出しの指示など) は通知ではない
    assert not is_system_notice({"role": "system", "content": "指示"})
    assert not is_system_notice({"role": "user", "content": "x",
                                 "metadata": {SYSTEM_NOTICE_KEY: "schedule"}})
