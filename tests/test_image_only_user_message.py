"""本文が空で画像だけの user 発言が、各プロバイダ向けの組み立てで消えないこと。

画像だけの発言は、本文 "" と metadata の画像で表される。組み立てが「本文が空なら
捨てる」を画像より先に判定すると、発言ごと LLM に届かない (xAI と OpenAI 互換で
そうなっていた)。合成の PNG だけを使い、LLM は呼ばない (画像の説明の生成は差し替える)。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from llm_clients.anthropic_request_builder import _prepare_anthropic_messages
from llm_clients.openai_message_preparer import prepare_openai_messages
from llm_clients.xai import _build_xai_messages

SUMMARY = "[合成] 青い四角の画像"


@pytest.fixture
def png_path(tmp_path):
    path = tmp_path / "only_image.png"
    Image.new("RGB", (8, 8), (0, 0, 255)).save(path, format="PNG")
    return path


@pytest.fixture(autouse=True)
def fake_summary():
    # 画像の説明は本来 LLM で作るので、隔離テストでは固定の文に差し替える。
    # Gemini は関数を自分のモジュールに取り込んでいるので、そちらも差し替える
    # (片方だけだと本物の生成まで届く)。
    with patch(
        "saiverse.media_summary.ensure_image_summary", return_value=SUMMARY,
    ) as mocked, patch(
        "llm_clients.gemini.ensure_image_summary", new=mocked,
    ):
        yield mocked


def _image_only_user(path, **extra_metadata):
    metadata = {
        "media": [{"path": str(path), "mime_type": "image/png", "uri": str(path)}],
    }
    metadata.update(extra_metadata)
    return {"role": "user", "content": "", "metadata": metadata}


# --- xAI -------------------------------------------------------------------

def test_xai_keeps_image_only_user_with_embedded_image(png_path):
    result = _build_xai_messages([_image_only_user(png_path)], supports_images=True)

    assert len(result) == 1
    message = result[0]
    kinds = [part.WhichOneof("content") for part in message.content]
    # 空の文字部品を作らず、画像の部品だけで組む
    assert kinds == ["image_url"]
    assert message.content[0].image_url.image_url.startswith("data:image/png;base64,")


def test_xai_image_only_user_gets_note_when_model_has_no_vision(png_path):
    result = _build_xai_messages([_image_only_user(png_path)], supports_images=False)

    assert len(result) == 1
    parts = result[0].content
    assert [part.WhichOneof("content") for part in parts] == ["text"]
    assert SUMMARY in parts[0].text


def test_xai_image_only_user_with_unreadable_file_falls_back_to_note(png_path):
    # 画像が読み込めなくても、発言ごと消さずに説明の文で届ける。
    # (ファイル自体が無い画像は、手前の iter_image_media が全プロバイダ共通で外す)
    with patch("llm_clients.xai.load_image_bytes_for_llm", return_value=(None, None)):
        result = _build_xai_messages([_image_only_user(png_path)], supports_images=True)

    assert len(result) == 1
    parts = result[0].content
    assert [part.WhichOneof("content") for part in parts] == ["text"]
    assert SUMMARY in parts[0].text


def test_xai_text_and_image_stays_one_message(png_path):
    message = _image_only_user(png_path)
    message["content"] = "見て"
    result = _build_xai_messages([message], supports_images=True)

    assert len(result) == 1
    kinds = [part.WhichOneof("content") for part in result[0].content]
    assert kinds == ["text", "image_url"]
    assert result[0].content[0].text == "見て"


def test_xai_still_drops_empty_user_without_images():
    result = _build_xai_messages(
        [{"role": "user", "content": ""}, {"role": "assistant", "content": ""}],
        supports_images=True,
    )
    assert result == []


def test_xai_respects_skip_image_summary_marker(png_path, fake_summary):
    # 画像の説明を作る依頼そのもの (saiverse/media_summary.py) で説明を作り始めると
    # 再帰になる。他のクライアントと同じく、印があれば説明を作らない。
    message = _image_only_user(png_path, __skip_image_summary__=True)
    result = _build_xai_messages([message], supports_images=False)

    fake_summary.assert_not_called()
    assert len(result) == 1
    assert SUMMARY not in result[0].content[0].text


# --- OpenAI 互換 -----------------------------------------------------------

def test_openai_keeps_image_only_user_with_embedded_image(png_path):
    prepared = prepare_openai_messages([_image_only_user(png_path)], supports_images=True)

    assert len(prepared) == 1
    assert prepared[0]["role"] == "user"
    content = prepared[0]["content"]
    assert [part["type"] for part in content] == ["image_url"]


def test_openai_image_only_user_gets_note_when_model_has_no_vision(png_path):
    prepared = prepare_openai_messages([_image_only_user(png_path)], supports_images=False)

    assert len(prepared) == 1
    assert SUMMARY in prepared[0]["content"]


def test_openai_still_drops_empty_user_without_images():
    prepared = prepare_openai_messages(
        [{"role": "user", "content": ""}], supports_images=True,
    )
    assert prepared == []


# --- Anthropic (もともと残していた側。揃えた先の基準として固定する) ----------

def test_anthropic_keeps_image_only_user(png_path):
    prepared = _prepare_anthropic_messages(
        [_image_only_user(png_path)], supports_images=True, enable_cache=False,
    )

    assert len(prepared) == 1
    assert [block["type"] for block in prepared[0]["content"]] == ["image"]


# --- Gemini (画像非対応のモデルでも、画像を説明の文にして送る) -------------------

def _gemini_contents(messages, *, supports_images):
    from llm_clients.gemini import GeminiClient
    from saiverse.model_configs import get_model_config

    with patch(
        "llm_clients.gemini.build_gemini_clients",
        return_value=(MagicMock(), MagicMock(), MagicMock()),
    ):
        client = GeminiClient(
            "gemini-4-argon", config=get_model_config("gemini-4-argon"),
            supports_images=supports_images,
        )
    _system, contents = client._convert_messages(messages)
    return contents


def test_gemini_image_only_user_gets_note_when_model_has_no_vision(png_path):
    contents = _gemini_contents([_image_only_user(png_path)], supports_images=False)

    user_parts = [c for c in contents if c.role == "user"][-1].parts
    texts = [p.text for p in user_parts if p.text]
    assert any(SUMMARY in t for t in texts)
    assert not any(p.inline_data for p in user_parts)


def test_gemini_keeps_image_only_user_with_embedded_image(png_path):
    contents = _gemini_contents([_image_only_user(png_path)], supports_images=True)

    user_parts = [c for c in contents if c.role == "user"][-1].parts
    assert any(p.inline_data for p in user_parts)
