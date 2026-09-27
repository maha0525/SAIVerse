"""環境設定の一覧 (GET /api/admin/env) が、後の版で足された変数も出すこと。

.env は setup のときに .env.example を写したもので、アップデートでは書き足されない。
一覧を .env だけから作ると、後の版で増えた変数 (TYPESAFE_API_KEY など) の入力欄が
既存ユーザーの画面に出ない (2026-09-22、v0.3.14 の Jev でユーザーが報告)。
"""
from __future__ import annotations

import pytest

from api.routes import admin


@pytest.fixture
def env_files(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    monkeypatch.setattr(admin, "ENV_FILE_PATH", env)
    monkeypatch.setattr(admin, "ENV_EXAMPLE_PATH", example)
    return env, example


def test_keys_only_in_the_example_are_listed_empty(env_files):
    env, example = env_files
    env.write_text("GEMINI_API_KEY=abc\nSAIVERSE_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    example.write_text(
        "# comment\n"
        "GEMINI_API_KEY=\n"
        "SAIVERSE_LOG_LEVEL=INFO\n"
        "TYPESAFE_API_KEY=\n"
        "# COMMENTED_OUT=1\n",
        encoding="utf-8",
    )

    listed = {v.key: v for v in admin.get_env_vars()}

    assert set(listed) == {"GEMINI_API_KEY", "SAIVERSE_LOG_LEVEL", "TYPESAFE_API_KEY"}
    # .env にある値が勝ち、雛形の既定値で上書きしない
    assert listed["SAIVERSE_LOG_LEVEL"].value == "DEBUG"
    assert listed["GEMINI_API_KEY"].value == "********"
    # 雛形にしか無い変数は未設定として空欄で出す (伏せ字にしない)
    assert listed["TYPESAFE_API_KEY"].value == ""
    assert listed["TYPESAFE_API_KEY"].is_sensitive is True


def test_listing_works_without_an_example_file(env_files):
    env, _example = env_files
    env.write_text("FOO=bar\n", encoding="utf-8")

    assert [v.key for v in admin.get_env_vars()] == ["FOO"]


def test_the_real_example_offers_the_typesafe_key():
    assert "TYPESAFE_API_KEY" in admin.read_env_example_keys()
