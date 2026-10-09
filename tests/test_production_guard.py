"""本番ホーム (~/.saiverse) への書き込みを拒否する番人のテスト。

対象は ``scripts/_shared/production_guard.py``。会話ランナーと複製スクリプトの
番人は、それぞれのテストファイル (test_run_conversation / test_clone_world /
test_clone_persona) が確かめる。一日シム (scripts/run_day_sim.py) の番人の
テストは、一日シムごと撤去した v0.4 段 1-4 で消した。

``Path.home()`` を一時ディレクトリへ差し替えるので、本物の ~/.saiverse には触れない。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

from scripts._shared.production_guard import (
    ProductionPathError,
    effective_home,
    effective_user_data_dir,
    is_under_production,
    production_home,
    refuse_production_paths,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Path.home() と ~ の展開先を一時ディレクトリにし、SAIVERSE_* env を未設定にする。"""
    home = tmp_path / "user_home"
    (home / ".saiverse").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SAIVERSE_HOME", raising=False)
    monkeypatch.delenv("SAIVERSE_USER_DATA_DIR", raising=False)
    return home


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "sandbox"
    (root / ".saiverse").mkdir(parents=True)
    (root / "user_data" / "database").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# 共通の番人
# ---------------------------------------------------------------------------


def test_production_home_does_not_follow_saiverse_home(fake_home, sandbox, monkeypatch):
    monkeypatch.setenv("SAIVERSE_HOME", str(sandbox / ".saiverse"))
    assert production_home() == (fake_home / ".saiverse").resolve()
    assert effective_home() == sandbox / ".saiverse"


def test_db_under_production_is_refused_while_saiverse_home_points_to_sandbox(
    fake_home, sandbox, monkeypatch,
):
    # 2026-09-17 に見つかった穴そのもの: SAIVERSE_HOME をテスト環境に向けたまま、
    # 本番の DB を渡す。旧実装は SAIVERSE_HOME を本番とみなしたので通していた。
    monkeypatch.setenv("SAIVERSE_HOME", str(sandbox / ".saiverse"))
    prod_db = fake_home / ".saiverse" / "user_data" / "database" / "saiverse.db"
    with pytest.raises(ProductionPathError) as excinfo:
        refuse_production_paths({"--db-file": prod_db}, reason="理由の文")
    message = str(excinfo.value)
    assert "--db-file" in message
    assert str(prod_db) in message
    assert "理由の文" in message


def test_production_home_itself_is_refused(fake_home):
    assert is_under_production(fake_home / ".saiverse")


@pytest.mark.parametrize("name", [".saiverse_test", ".saiverse-backup", "saiverse"])
def test_siblings_of_production_home_are_allowed(fake_home, name):
    # 名前が前方一致するだけの隣のフォルダは本番ではない
    assert not is_under_production(fake_home / name / "user_data" / "database" / "saiverse.db")


def test_sandbox_and_none_targets_pass(fake_home, sandbox):
    refuse_production_paths(
        {
            "--db-file": sandbox / "user_data" / "database" / "saiverse.db",
            "SAIVERSE_HOME": sandbox / ".saiverse",
            "--out": None,  # この実行では書かない
        },
        reason="x",
    )


def test_error_cls_is_raised(fake_home):
    class ScriptError(RuntimeError):
        pass

    with pytest.raises(ScriptError):
        refuse_production_paths(
            {"--dest-home": fake_home / ".saiverse"}, reason="x", error_cls=ScriptError,
        )


def test_empty_env_means_production(fake_home, monkeypatch):
    # saiverse.data_paths は空文字の SAIVERSE_HOME を未設定 (= ~/.saiverse) として扱う
    monkeypatch.setenv("SAIVERSE_HOME", "")
    monkeypatch.setenv("SAIVERSE_USER_DATA_DIR", "")
    assert effective_home() == fake_home / ".saiverse"
    assert effective_user_data_dir() == fake_home / ".saiverse" / "user_data"
    assert is_under_production(effective_home())
    assert is_under_production(effective_user_data_dir())


def test_tilde_path_is_refused(fake_home):
    assert is_under_production("~/.saiverse/personas/quon_city_a")


def test_relative_dotdot_into_production_is_refused(fake_home, sandbox):
    sneaky = sandbox / ".." / "user_home" / ".saiverse" / "personas"
    assert is_under_production(sneaky)


def test_link_into_production_is_refused(fake_home, sandbox):
    target = fake_home / ".saiverse" / "personas"
    target.mkdir()
    link = sandbox / "linked_home"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("symlink を作れない環境")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    assert is_under_production(link / "quon_city_a" / "memory.db")


@pytest.mark.skipif(sys.platform != "win32", reason="大文字小文字を区別しないのは Windows だけ")
def test_case_difference_is_refused_on_windows(fake_home):
    upper = Path(str(fake_home / ".saiverse" / "user_data").upper())
    assert is_under_production(upper)
