"""画像の枚数の上限の既定廃止と、バイトの非常弁 (llm_clients/utils.py)。

docs/issues/image_attachment_limit_breaks_cache_prefix.md:

- 枚数の上限は env (``SAIVERSE_{PROVIDER}_ATTACHMENT_LIMIT`` /
  ``SAIVERSE_ATTACHMENT_LIMIT``) を明示したときだけ効く。既定は上限なしで、
  画像はすべて画像のまま送る。
- 天井 = 1 回の API 送信に埋め込む画像の合計バイト (base64 後の見積もり) の上限。
  プロバイダの要求サイズ上限 (Gemini 約 20MB) に当たらないための余裕。これを
  超える送信でだけ、古い側から床まで画像を要約の文字に置き換える。
- 床 = 弁が開いたとき、次に開くまでの間隔を稼ぐために下げておく水準。
- 一度置き換えた画像は、後ろに画像が足されるだけなら画像に戻らない
  (弁が開かない回は、前回の送信が今回の送信の先頭とそのまま一致する)。

合成の PNG だけを使い、LLM は呼ばない (画像の要約は差し替える)。
"""
from __future__ import annotations

import base64
import math
import os
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from llm_clients import utils as attachment_utils
from llm_clients.anthropic_request_builder import _prepare_anthropic_messages
from llm_clients.openai_message_preparer import prepare_openai_messages
from llm_clients.utils import (
    DEFAULT_ATTACHMENT_BYTE_CEILING,
    DEFAULT_ATTACHMENT_BYTE_FLOOR,
    attachment_positions_to_replace,
    estimate_inline_image_bytes,
    mark_attachment_limit_off,
    parse_attachment_byte_bounds,
    parse_attachment_limit,
    select_embedded_attachment_keys,
)
from llm_clients.xai import _build_xai_messages

SUMMARY = "[合成] 画像の要約"


@pytest.fixture(autouse=True)
def clean_attachment_env(monkeypatch):
    # 実行環境の .env が上限・非常弁を変えていても、既定から始める。
    for key in list(os.environ):
        if key.startswith("SAIVERSE_") and (
            key.endswith("ATTACHMENT_LIMIT")
            or key.endswith("ATTACHMENT_BYTE_CEILING")
            or key.endswith("ATTACHMENT_BYTE_FLOOR")
        ):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def summary_mock():
    # 画像の要約は本来 LLM で作るので差し替える。Gemini は関数を自分の
    # モジュールに取り込んでいるので、そちらも同じ偽物にする。
    mocked = MagicMock(return_value=SUMMARY)
    with patch("saiverse.media_summary.ensure_image_summary", new=mocked), patch(
        "llm_clients.gemini.ensure_image_summary", new=mocked,
    ):
        yield mocked


def _write_images(tmp_path, count):
    """同じ中身 (= 同じ大きさ) の PNG を ``count`` 枚、別のファイルに書く。"""
    paths = []
    for i in range(count):
        path = tmp_path / f"img{i}.png"
        Image.new("RGB", (16, 16), (10, 20, 30)).save(path, format="PNG")
        paths.append(path)
    return paths


def _image_messages(paths):
    return [
        {
            "role": "user",
            "content": f"message {i}",
            "metadata": {"media": [
                {"type": "image", "path": str(path), "mime_type": "image/png", "uri": str(path)},
            ]},
        }
        for i, path in enumerate(paths)
    ]


def _set_valve_for_unit(monkeypatch, provider, unit, ceiling_units, floor_units):
    """天井・床を画像 1 枚の見積もり ``unit`` の倍数で設定する。"""
    monkeypatch.setenv(
        f"SAIVERSE_{provider}_ATTACHMENT_BYTE_CEILING", str(int(unit * ceiling_units)),
    )
    monkeypatch.setenv(
        f"SAIVERSE_{provider}_ATTACHMENT_BYTE_FLOOR", str(int(unit * floor_units)),
    )


# --- 枚数の上限 ----------------------------------------------------------------


def test_attachment_limit_is_unlimited_unless_set(monkeypatch):
    assert parse_attachment_limit("GEMINI") is None
    assert parse_attachment_limit("") is None

    monkeypatch.setenv("SAIVERSE_ATTACHMENT_LIMIT", "3")
    assert parse_attachment_limit("GEMINI") == 3
    monkeypatch.setenv("SAIVERSE_GEMINI_ATTACHMENT_LIMIT", "1")
    assert parse_attachment_limit("GEMINI") == 1
    assert parse_attachment_limit("OPENAI") == 3

    monkeypatch.setenv("SAIVERSE_OPENAI_ATTACHMENT_LIMIT", "many")
    assert parse_attachment_limit("OPENAI") is None


# --- 天井と床 ------------------------------------------------------------------


def test_byte_bounds_defaults_and_env(monkeypatch):
    # 天井: 1 回の送信に埋め込む画像の合計 (base64 後) の上限。Gemini の要求サイズ
    # 上限 約 20MB より下に余裕を残す。床: 弁が開いたら下げておく水準。
    assert DEFAULT_ATTACHMENT_BYTE_CEILING == 12 * 1024 * 1024
    assert DEFAULT_ATTACHMENT_BYTE_FLOOR == 6 * 1024 * 1024
    assert DEFAULT_ATTACHMENT_BYTE_CEILING < 20 * 1000 * 1000
    assert parse_attachment_byte_bounds("GEMINI") == (
        DEFAULT_ATTACHMENT_BYTE_CEILING, DEFAULT_ATTACHMENT_BYTE_FLOOR,
    )

    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_CEILING", "1000")
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_FLOOR", "400")
    monkeypatch.setenv("SAIVERSE_GEMINI_ATTACHMENT_BYTE_CEILING", "2000")
    assert parse_attachment_byte_bounds("GEMINI") == (2000, 400)
    assert parse_attachment_byte_bounds("OPENAI") == (1000, 400)

    # 床が天井を超える設定は天井に揃える。読めない値は既定。
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_FLOOR", "5000")
    assert parse_attachment_byte_bounds("OPENAI") == (1000, 1000)
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_CEILING", "lots")
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_FLOOR", "-1")
    assert parse_attachment_byte_bounds("OPENAI") == (
        DEFAULT_ATTACHMENT_BYTE_CEILING, DEFAULT_ATTACHMENT_BYTE_FLOOR,
    )


# --- 見積もり ------------------------------------------------------------------


def test_inline_estimate_is_base64_four_thirds(tmp_path):
    path = _write_images(tmp_path, 1)[0]
    raw = path.read_bytes()
    estimated = estimate_inline_image_bytes(path, "image/png")
    assert estimated == math.ceil(len(raw) * 4 / 3)
    # base64 の実際の長さ (4 の倍数に切り上げ) との差は 3 バイト未満
    assert 0 <= len(base64.b64encode(raw)) - estimated < 3
    assert estimate_inline_image_bytes(tmp_path / "missing.png", "image/png") is None


# --- 非常弁の決め方 ------------------------------------------------------------


def test_valve_does_nothing_under_the_ceiling():
    assert attachment_positions_to_replace([10, 10, 10], ceiling=30, floor=10) == []
    assert attachment_positions_to_replace([], ceiling=30, floor=10) == []


def test_valve_replaces_oldest_down_to_the_floor():
    # 4 枚目で 40 > 天井 35 → 古い側から 15 以下になるまで (3 枚) 置き換える。
    # 5 枚目は 20 で天井以下なので、そのまま。
    assert attachment_positions_to_replace([10] * 5, ceiling=35, floor=15) == [0, 1, 2]
    # 見積もりの無い画像 (読めない) は 0 として数える: 3 枚目で 20 > 15 →
    # 0 と 10 を置き換え、最後の 1 枚 (10 > 床 5 だが天井以下) は残す。
    assert attachment_positions_to_replace([None, 10, 10], ceiling=15, floor=5) == [0, 1]
    assert attachment_positions_to_replace([None, 10], ceiling=15, floor=5) == []


def test_valve_keeps_the_newest_unless_it_alone_exceeds_the_ceiling():
    # 新しい 1 枚が床 (5) より大きいだけなら残す。
    assert attachment_positions_to_replace([10, 12], ceiling=15, floor=5) == [0]
    # 1 枚で天井を超える画像は、その 1 枚だけを置き換える。
    assert attachment_positions_to_replace([10, 40], ceiling=15, floor=5) == [1]


def test_an_image_over_the_ceiling_alone_does_not_take_older_images_with_it():
    """一枚で天井を超える画像は弁の手前で文字に振り分け、他の画像の合計に入れない。
    以前は先に古い側を床まで畳んでからその一枚も外し、古い画像が道連れになった。"""
    assert attachment_positions_to_replace([1, 12], ceiling=10, floor=5) == [1]
    # 列の途中にあっても同じ。前後の画像の合計は 3 + 3 + 3 = 9 ≤ 天井。
    assert attachment_positions_to_replace([3, 30, 3, 3], ceiling=10, floor=5) == [1]
    # 天井ちょうどは一枚で超えていないので、ふつうに数える。
    assert attachment_positions_to_replace([10], ceiling=10, floor=5) == []
    assert attachment_positions_to_replace([4, 10], ceiling=10, floor=5) == [0]


def test_valve_never_shrinks_when_images_are_appended():
    """後ろに画像が足されるだけなら、置き換える画像の集まりは広がるだけで縮まない
    (単調性)。一枚で天井を超える画像が混ざっていても成り立つ。"""
    import random

    rng = random.Random(1234)
    for _ in range(300):
        sizes = [rng.choice((rng.randint(1, 40), rng.randint(90, 150))) for _ in range(30)]
        previous: set = set()
        for n in range(len(sizes) + 1):
            replaced = set(attachment_positions_to_replace(sizes[:n], ceiling=100, floor=50))
            assert replaced >= previous
            kept = sum(size for pos, size in enumerate(sizes[:n]) if pos not in replaced)
            assert kept <= 100
            # 天井以下の画像を置き換えるのは古い側の連続した並びだけ
            fitting_replaced = sorted(pos for pos in replaced if sizes[pos] <= 100)
            fitting = [pos for pos in range(n) if sizes[pos] <= 100]
            assert fitting_replaced == fitting[:len(fitting_replaced)]
            previous = replaced


def test_estimate_remembers_unreadable_images(tmp_path, monkeypatch):
    """読めなかった画像の見積もり (None) も版ごとに覚え、毎回読み直さない。"""
    from saiverse import media_utils

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    calls = []

    def fake_load(path, mime_type, max_bytes=None):
        calls.append(path)
        raise ValueError("broken")

    monkeypatch.setattr(media_utils, "load_image_bytes_for_llm", fake_load)
    assert estimate_inline_image_bytes(broken, "image/png") is None
    assert estimate_inline_image_bytes(broken, "image/png") is None
    assert len(calls) == 1
    # ファイルが書き換われば測り直す。
    broken.write_bytes(b"still not an image, but longer")
    assert estimate_inline_image_bytes(broken, "image/png") is None
    assert len(calls) == 2


def test_select_applies_count_limit_then_valve(monkeypatch):
    cache = {i: [{"path": f"p{i}", "mime_type": "image/png"}] for i in range(6)}
    monkeypatch.setattr(attachment_utils, "estimate_inline_image_bytes", lambda *a, **k: 10)
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_CEILING", "35")
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_FLOOR", "15")

    # 上限なし: 6 枚。4 枚目で開いて 0〜2 を置き換え、5〜6 枚目は天井以下。
    assert select_embedded_attachment_keys(cache, None, provider="OPENAI") == {
        (3, 0), (4, 0), (5, 0),
    }
    # 枚数の上限 3: 新しい 3 枚 (30 ≤ 35) で弁は開かない。
    assert select_embedded_attachment_keys(cache, 3, provider="OPENAI") == {
        (3, 0), (4, 0), (5, 0),
    }
    # 天井を超えない列では、上限なしのとき None (= 全部埋め込む) を返す。
    small = {i: cache[i] for i in range(3)}
    assert select_embedded_attachment_keys(small, None, provider="OPENAI") is None


def test_select_never_counts_visual_or_realtime_context_images(monkeypatch):
    cache = {i: [{"path": f"p{i}", "mime_type": "image/png"}] for i in range(5)}
    monkeypatch.setattr(attachment_utils, "estimate_inline_image_bytes", lambda *a, **k: 10)
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_CEILING", "25")
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_FLOOR", "10")
    messages = [{"role": "user", "metadata": {}} for _ in range(5)]
    messages[4] = {"role": "user", "metadata": {"__realtime_context__": True}}

    allowed = select_embedded_attachment_keys(
        cache, None, exempt_indices={0}, provider="OPENAI", messages=messages,
    )
    # 数えるのは 1〜3 の 3 枚: 3 枚目で 30 > 25 → 1, 2 を置き換えて 10。
    assert allowed == {(0, 0), (3, 0), (4, 0)}


def test_select_counts_only_embeddable_messages(monkeypatch):
    cache = {i: [{"path": f"p{i}", "mime_type": "image/png"}] for i in range(4)}
    monkeypatch.setattr(attachment_utils, "estimate_inline_image_bytes", lambda *a, **k: 10)
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_CEILING", "25")
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_FLOOR", "10")

    # 埋め込まないメッセージ (1 と 3) の画像は合計に入らないので、弁は開かない。
    assert select_embedded_attachment_keys(
        cache, None, provider="OPENAI", embeddable_indices={0, 2},
    ) is None
    # 埋め込むメッセージが無ければ測りもしない。
    assert select_embedded_attachment_keys(
        cache, None, provider="OPENAI", embeddable_indices=set(),
    ) is None


def test_select_never_counts_image_summary_requests(monkeypatch):
    """要約を作るための送信 (``__skip_image_summary__``) の画像は、枚数の上限にも
    非常弁にも数えず、常に埋め込む。外すと要約のモデルが画像を見ないまま答える。"""
    cache = {0: [{"path": "p0", "mime_type": "image/png"}]}
    monkeypatch.setattr(attachment_utils, "estimate_inline_image_bytes", lambda *a, **k: 10)
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_CEILING", "1")
    monkeypatch.setenv("SAIVERSE_ATTACHMENT_BYTE_FLOOR", "0")
    messages = [{"role": "user", "metadata": {"__skip_image_summary__": True}}]

    assert select_embedded_attachment_keys(
        cache, None, provider="GEMINI", messages=messages,
    ) is None
    assert select_embedded_attachment_keys(
        cache, 0, provider="GEMINI", messages=messages,
    ) == {(0, 0)}
    # 印が無ければ、同じ画像は一枚で天井を超えるので文字になる。
    assert select_embedded_attachment_keys(
        cache, None, provider="GEMINI", messages=[{"role": "user", "metadata": {}}],
    ) == set()


def _captured_summary_requests(tmp_path, monkeypatch):
    """saiverse/media_summary.py が要約・説明を作るために送るメッセージ列を、実物の
    関数から取り出す (LLM は呼ばない)。"""
    from saiverse import media_summary

    captured = []
    fake_client = MagicMock()
    fake_client.supports_images = True

    def fake_generate(messages, **_kwargs):
        captured.append(messages)
        return "説明"

    fake_client.generate.side_effect = fake_generate
    monkeypatch.setattr(media_summary, "_get_summary_client", lambda: fake_client)
    path = _write_images(tmp_path, 1)[0]
    media_summary._generate_image_summary(path, "image/png")
    media_summary.generate_contextual_image_description(path, "image/png", "見て", "")
    assert len(captured) == 2
    return captured


def test_summary_requests_embed_the_image_even_when_the_valve_would_open(
    tmp_path, monkeypatch, summary_mock,
):
    """要約・説明を作る送信は、天井 1 バイト・枚数の上限 0 でも画像を埋め込む
    (Gemini と OpenAI 互換の実物の組み立てで確かめる)。"""
    requests = _captured_summary_requests(tmp_path, monkeypatch)
    for provider in ("GEMINI", "OPENAI"):
        monkeypatch.setenv(f"SAIVERSE_{provider}_ATTACHMENT_BYTE_CEILING", "1")
        monkeypatch.setenv(f"SAIVERSE_{provider}_ATTACHMENT_BYTE_FLOOR", "0")
        monkeypatch.setenv(f"SAIVERSE_{provider}_ATTACHMENT_LIMIT", "0")
    for messages in requests:
        assert _gemini_embedded(_gemini_contents(messages)) == [0]
        prepared = prepare_openai_messages(messages, supports_images=True)
        assert _openai_embedded(prepared) == [0]
    summary_mock.assert_not_called()


def test_saved_summary_alone_does_not_replace_an_image(tmp_path, monkeypatch):
    """要約のサイドカー (``.summary.txt``) はアップロード時の文脈つきの説明などでも
    作られるので、それがあるだけでは画像を文字にしない。置き換えは列の画像の
    並びと大きさだけで決まる。"""
    from saiverse.media_utils import save_media_summary

    paths = _write_images(tmp_path, 3)
    save_media_summary(paths[0], "アップロード時に作った説明")
    cache = {
        i: [{"path": path, "mime_type": "image/png"}] for i, path in enumerate(paths)
    }
    assert select_embedded_attachment_keys(cache, None, provider="OPENAI") is None


# --- 組み立て側: OpenAI 互換 ---------------------------------------------------


def _openai_embedded(prepared):
    return [
        i for i, message in enumerate(prepared)
        if isinstance(message["content"], list)
        and any(part["type"] == "image_url" for part in message["content"])
    ]


def test_openai_embeds_every_image_without_an_explicit_limit(tmp_path, summary_mock):
    prepared = prepare_openai_messages(
        _image_messages(_write_images(tmp_path, 8)), supports_images=True,
    )
    assert _openai_embedded(prepared) == list(range(8))
    summary_mock.assert_not_called()


def test_openai_explicit_env_limit_still_applies(tmp_path, monkeypatch, summary_mock):
    monkeypatch.setenv("SAIVERSE_OPENAI_ATTACHMENT_LIMIT", "2")
    prepared = prepare_openai_messages(
        _image_messages(_write_images(tmp_path, 5)), supports_images=True,
    )
    assert _openai_embedded(prepared) == [3, 4]
    assert summary_mock.call_count == 3


def test_openai_valve_replaces_oldest_and_stays_a_prefix(tmp_path, monkeypatch, summary_mock):
    paths = _write_images(tmp_path, 6)
    unit = estimate_inline_image_bytes(paths[0], "image/png")
    _set_valve_for_unit(monkeypatch, "OPENAI", unit, 3.5, 1.5)

    five = prepare_openai_messages(_image_messages(paths[:5]), supports_images=True)
    # 4 枚目で天井を超え、古い 3 枚を床まで置き換える。新しい 2 枚は画像のまま。
    assert _openai_embedded(five) == [3, 4]
    for message in five[:3]:
        assert any(SUMMARY in part.get("text", "") for part in message["content"])

    # 6 枚目を足しても、置き換えた画像は画像に戻らず、前回の送信が先頭と一致する。
    six = prepare_openai_messages(_image_messages(paths), supports_images=True)
    assert _openai_embedded(six) == [3, 4, 5]
    assert six[:5] == five


# --- 組み立て側: Anthropic -----------------------------------------------------


def _anthropic_embedded(prepared):
    return [
        i for i, message in enumerate(prepared)
        if any(block["type"] == "image" for block in message["content"])
    ]


def test_anthropic_embeds_every_image_without_an_explicit_limit(tmp_path, summary_mock):
    prepared = _prepare_anthropic_messages(
        _image_messages(_write_images(tmp_path, 8)),
        supports_images=True, enable_cache=False,
    )
    assert _anthropic_embedded(prepared) == list(range(8))
    summary_mock.assert_not_called()


def test_anthropic_explicit_env_limit_still_applies(tmp_path, monkeypatch, summary_mock):
    monkeypatch.setenv("SAIVERSE_ANTHROPIC_ATTACHMENT_LIMIT", "2")
    prepared = _prepare_anthropic_messages(
        _image_messages(_write_images(tmp_path, 5)),
        supports_images=True, enable_cache=False,
    )
    assert _anthropic_embedded(prepared) == [3, 4]


def test_anthropic_valve_replaces_oldest_images(tmp_path, monkeypatch, summary_mock):
    paths = _write_images(tmp_path, 5)
    unit = estimate_inline_image_bytes(paths[0], "image/png", max_bytes=5 * 1024 * 1024)
    _set_valve_for_unit(monkeypatch, "ANTHROPIC", unit, 3.5, 1.5)
    prepared = _prepare_anthropic_messages(
        _image_messages(paths), supports_images=True, enable_cache=False,
        max_image_bytes=5 * 1024 * 1024,
    )
    assert _anthropic_embedded(prepared) == [3, 4]
    for message in prepared[:3]:
        assert any(SUMMARY in block.get("text", "") for block in message["content"])


# --- 組み立て側: xAI -----------------------------------------------------------


def _xai_embedded(built):
    return [
        i for i, message in enumerate(built)
        if any(part.WhichOneof("content") == "image_url" for part in message.content)
    ]


def test_xai_embeds_every_image_without_an_explicit_limit(tmp_path, summary_mock):
    built = _build_xai_messages(_image_messages(_write_images(tmp_path, 8)), supports_images=True)
    assert _xai_embedded(built) == list(range(8))
    summary_mock.assert_not_called()


def test_xai_explicit_env_limit_still_applies(tmp_path, monkeypatch, summary_mock):
    monkeypatch.setenv("SAIVERSE_XAI_ATTACHMENT_LIMIT", "2")
    built = _build_xai_messages(_image_messages(_write_images(tmp_path, 5)), supports_images=True)
    assert _xai_embedded(built) == [3, 4]


def test_xai_valve_replaces_oldest_images(tmp_path, monkeypatch, summary_mock):
    paths = _write_images(tmp_path, 5)
    unit = estimate_inline_image_bytes(paths[0], "image/png")
    _set_valve_for_unit(monkeypatch, "XAI", unit, 3.5, 1.5)
    built = _build_xai_messages(_image_messages(paths), supports_images=True)
    assert _xai_embedded(built) == [3, 4]


# --- 組み立て側: Gemini --------------------------------------------------------


def _gemini_contents(messages):
    from llm_clients.gemini import GeminiClient
    from saiverse.model_configs import get_model_config

    with patch(
        "llm_clients.gemini.build_gemini_clients",
        return_value=(MagicMock(), MagicMock(), MagicMock()),
    ):
        client = GeminiClient(
            "gemini-4-argon", config=get_model_config("gemini-4-argon"),
            supports_images=True,
        )
    _system, contents = client._convert_messages(messages)
    return contents


def _gemini_embedded(contents):
    return [
        i for i, content in enumerate(contents)
        if any(part.inline_data for part in content.parts)
    ]


def test_gemini_embeds_every_image_without_an_explicit_limit(tmp_path, summary_mock):
    contents = _gemini_contents(_image_messages(_write_images(tmp_path, 8)))
    assert _gemini_embedded(contents) == list(range(8))
    # 埋め込む画像には要約を作らない (以前は全部の画像で作っていた)
    summary_mock.assert_not_called()


def test_gemini_explicit_env_limit_still_applies(tmp_path, monkeypatch, summary_mock):
    monkeypatch.setenv("SAIVERSE_GEMINI_ATTACHMENT_LIMIT", "2")
    contents = _gemini_contents(_image_messages(_write_images(tmp_path, 5)))
    assert _gemini_embedded(contents) == [3, 4]
    assert summary_mock.call_count == 3
    for content in contents[:3]:
        assert any("[画像参照のみ:" in (part.text or "") for part in content.parts)


def test_gemini_valve_replaces_oldest_images(tmp_path, monkeypatch, summary_mock):
    paths = _write_images(tmp_path, 5)
    unit = estimate_inline_image_bytes(paths[0], "image/png")
    _set_valve_for_unit(monkeypatch, "GEMINI", unit, 3.5, 1.5)
    contents = _gemini_contents(_image_messages(paths))
    assert _gemini_embedded(contents) == [3, 4]
    for content in contents[:3]:
        assert any(SUMMARY in (part.text or "") for part in content.parts)
    assert summary_mock.call_count == 3


def test_gemini_valve_applies_even_when_the_count_limit_is_off(tmp_path, monkeypatch, summary_mock):
    """persist_realtime_and_recall のモデルの列 (枚数の上限を使わない印) でも、
    プロバイダの要求サイズ上限を避ける非常弁は効く (§4.2 の例外)。"""
    monkeypatch.setenv("SAIVERSE_GEMINI_ATTACHMENT_LIMIT", "1")  # 印があれば使わない
    paths = _write_images(tmp_path, 5)
    unit = estimate_inline_image_bytes(paths[0], "image/png")
    _set_valve_for_unit(monkeypatch, "GEMINI", unit, 3.5, 1.5)
    messages = _image_messages(paths)
    mark_attachment_limit_off(messages)
    contents = _gemini_contents(messages)
    assert _gemini_embedded(contents) == [3, 4]
