"""Tests for saiverse/i18n_utils.py."""
from saiverse.i18n_utils import normalize_i18n_dict, resolve_i18n_text


def test_resolve_exact_match():
    data = {"ja": "こんにちは", "en": "Hello", "zh": "你好"}
    assert resolve_i18n_text(data, target_lang="ja") == "こんにちは"
    assert resolve_i18n_text(data, target_lang="en") == "Hello"
    assert resolve_i18n_text(data, target_lang="zh") == "你好"


def test_resolve_fallback_chain_lingua_franca():
    # When target is 'zh' but no 'zh' translation, fall back to 'en' (lingua franca)
    data = {"ja": "こんにちは", "en": "Hello"}
    assert resolve_i18n_text(data, target_lang="zh") == "Hello"

    # When target is 'zh' and neither 'zh' nor 'en' exists, fall back to 'ja'
    data_ja_only = {"ja": "こんにちは"}
    assert resolve_i18n_text(data_ja_only, target_lang="zh") == "こんにちは"


def test_resolve_fallback_en_to_ja():
    # When target is 'en' but only 'ja' exists, fall back to 'ja'
    data_ja_only = {"ja": "こんにちは"}
    assert resolve_i18n_text(data_ja_only, target_lang="en") == "こんにちは"


def test_resolve_fallback_ja_to_en():
    # When target is 'ja' but only 'en' exists, fall back to 'en'
    data_en_only = {"en": "Hello"}
    assert resolve_i18n_text(data_en_only, target_lang="ja") == "Hello"


def test_resolve_with_alt_fields():
    # Single string with alt_en
    assert resolve_i18n_text("テスト", target_lang="ja", alt_en="Test") == "テスト"
    assert resolve_i18n_text("テスト", target_lang="en", alt_en="Test") == "Test"
    assert resolve_i18n_text("テスト", target_lang="zh", alt_en="Test") == "Test"

    # None with alt_en and alt_ja
    assert resolve_i18n_text(None, target_lang="ja", alt_ja="日本語", alt_en="English") == "日本語"
    assert resolve_i18n_text(None, target_lang="en", alt_ja="日本語", alt_en="English") == "English"
    assert resolve_i18n_text(None, target_lang="zh", alt_ja="日本語", alt_en="English") == "English"


def test_normalize_i18n_dict():
    norm = normalize_i18n_dict({"ja": "和", "en": "En"}, alt_en="Ignored")
    assert norm == {"ja": "和", "en": "En"}

    norm2 = normalize_i18n_dict("テスト", alt_en="Test")
    assert norm2 == {"ja": "テスト", "en": "Test"}

    norm3 = normalize_i18n_dict(None, alt_ja="和", alt_en="En")
    assert norm3 == {"ja": "和", "en": "En"}
