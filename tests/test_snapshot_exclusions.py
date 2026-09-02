"""スナップショットの除外ルールと、中断で残る書きかけアーカイブの回帰テスト。

2026-09-02 の実機障害: ``start.bat`` → ``update_engine --manual`` →
``snapshot.py save`` の経路で、世界のスナップショット作成が 900 秒のタイムアウト
で落ちて SAIVerse が起動不能になった。原因は 2 つ。

1. 除外対象が backups / snapshots などに限られており、再生成できるだけの
   ``llama_cache/`` (16GB) まで sha256 計算と ZIP 圧縮の対象に入っていた。
2. 親プロセスの subprocess timeout で子が kill されるため ``cmd_save`` の
   except 節が走らず、書きかけの 20.8GB の ``.zip.tmp`` が残り続けた。

除外を一本の集合へまとめた際に、意味の違う 2 つの用途（save の収集除外 /
restore のアーカイブメンバー拒否）が同じ集合を見てしまい、``llama_cache/`` を
正当に含む過去のアーカイブを復元できなくなる欠陥も生まれた。その 2 集合の
分離もここで固定する。

``SAIVERSE_HOME`` は必ず tmp_path に向け、本番の ``~/.saiverse/`` には一切
触れない。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import snapshot
from scripts import update_engine


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _populate_world(home: Path) -> None:
    """除外されるものと、絶対に除外してはいけないものを両方置いた世界を作る。"""
    # 再生成できるキャッシュ（除外される側）
    _write(home / "llama_cache" / "mira__local-model.bin", "kv cache")
    _write(home / "llama_cache" / "nested" / "slot.ckpt", "kv checkpoint")
    # 運用ファイル（従来から除外される側）
    _write(home / "backups" / "saiverse.db_backup_startup_1.bak", "backup")
    _write(home / "user_data" / "logs" / "20260902_000000" / "backend.log", "log line")
    _write(home / "log.txt", "log line")
    # 世界そのもの（必ず含まれる側）
    _write(home / "image" / "20260101_photo.png", "photo bytes")
    _write(home / "image" / "20260101_photo.png.summary.txt", "photo summary")
    # サムネイルは再生成できるが、除外すると image がまるごとの入れ替え単位で
    # なくなるため、あえて除外していない（実測 3MB に対し os.rename が 1 回から
    # 約 960 回に増える）。
    _write(home / "image" / ".thumbnails" / "0123abcd.webp", "thumbnail")
    _write(home / "personas" / "mira" / "memory.db", "persona memory")
    _write(home / "user_data" / "database" / "saiverse.db", "world db")
    _write(home / "user_data" / "addon_data" / "stackchan" / "avatar_sets" / "mira.png", "avatar")


# ---- save 側: 何を集めて、何を集めないか ----

def test_llama_cache_is_not_snapshot_payload(tmp_path: Path, monkeypatch) -> None:
    """16GB の KV キャッシュがアーカイブに入らないこと。"""
    home = tmp_path / "home"
    _populate_world(home)
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    names = {entry.archive_path for entry in snapshot.collect_files_to_snapshot()}

    assert not [n for n in names if n.startswith("llama_cache/")]


def test_world_state_stays_in_snapshot_payload(tmp_path: Path, monkeypatch) -> None:
    """除外が広がりすぎていないことの確認。ここに挙げたものは再生成できない。

    ``image/.thumbnails/`` だけは再生成できるが、入れ替え単位を割らないために
    あえて含めている。その判断もここで固定する。
    """
    home = tmp_path / "home"
    _populate_world(home)
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    names = {entry.archive_path for entry in snapshot.collect_files_to_snapshot()}

    assert names == {
        "image/20260101_photo.png",
        "image/20260101_photo.png.summary.txt",
        "image/.thumbnails/0123abcd.webp",
        "personas/mira/memory.db",
        "user_data/database/saiverse.db",
        "user_data/addon_data/stackchan/avatar_sets/mira.png",
    }


# ---- restore 側 1: アーカイブメンバーの受け入れと拒否 ----

def test_old_archives_with_llama_cache_still_restore() -> None:
    """``llama_cache/`` を含む format_version 2 の旧アーカイブを拒否しないこと。

    ``llama_cache`` を収集から外したのは今回が初めてで、それ以前に作られた
    スナップショットには ``llama_cache/...`` が正当なメンバーとして入っている。
    ここで ValueError を投げると ``validate_and_extract_snapshot`` のループごと
    落ち、復元が丸ごと失敗する。
    """
    assert snapshot._safe_archive_member("llama_cache/air_city_a__gemma4-e4b.bin") == Path(
        "llama_cache/air_city_a__gemma4-e4b.bin"
    )
    assert snapshot._safe_archive_member("llama_cache/nested/slot.ckpt") == Path(
        "llama_cache/nested/slot.ckpt"
    )


@pytest.mark.parametrize(
    "member",
    [
        "backups/saiverse.db_backup_startup_1.bak",
        "snapshots/before_v0_3_0.zip",
        ".runtime/city_a.json",
        "log.txt",
        ".runtime.json",
        "user_data/logs/20260902_000000/backend.log",
    ],
)
def test_archive_cannot_write_into_preserved_operational_paths(member: str) -> None:
    """home 側で保全している運用領域を、アーカイブから書き換えられないこと。"""
    with pytest.raises(ValueError):
        snapshot._safe_archive_member(member)


@pytest.mark.parametrize(
    "member",
    [
        "../outside.txt",
        "personas/../../outside.txt",
        "C:\\Windows\\system32\\evil.dll",
        "C:/Windows/system32/evil.dll",
        "",
    ],
)
def test_archive_cannot_escape_the_staging_tree(member: str) -> None:
    """path traversal とドライブ付き絶対パスを拒否し続けること。"""
    with pytest.raises(ValueError):
        snapshot._safe_archive_member(member)


def test_root_anchored_member_cannot_land_inside_the_stage(tmp_path: Path) -> None:
    """``/etc/passwd`` 形式のメンバーが展開先の中を指さないこと。

    Windows の ``Path`` はドライブの無い ``/etc/passwd`` を ``is_absolute()`` で
    True にしないので ``_safe_archive_member`` は通す。stage に繋いだ結果が stage の
    外を指すことを ``validate_and_extract_snapshot`` の側が捕まえる、という二段構え。
    その二段目をここで固定する。
    """
    stage = tmp_path / "stage"
    stage.mkdir()

    rel = snapshot._safe_archive_member("/etc/passwd")

    assert not (stage / rel).resolve().is_relative_to(stage.resolve())


# ---- restore 側 2: 入れ替え単位 ----

def test_image_is_swapped_as_a_single_unit(tmp_path: Path) -> None:
    """``image`` がまるごと 1 単位で入れ替わること。

    ``image/.thumbnails`` を除外すると ``image`` は子ごとに分割され、実機では
    ``os.rename`` が 1 回から約 960 回に増える。``swap_staged_world`` は失敗時に
    移動済みを巻き戻す作りなので、分割数がそのまま部分失敗の窓の広さになる。
    """
    home = tmp_path / "home"
    _populate_world(home)
    # 実機の image/ 直下は 961 エントリ。分割されれば数がそのまま出る。
    for index in range(20):
        _write(home / "image" / f"2026010{index % 10}_extra_{index}.png", "bytes")

    roots = {path.as_posix() for path in snapshot._managed_swap_roots(home)}

    assert "image" in roots
    assert not [r for r in roots if r.startswith("image/")]


def test_swap_roots_descend_only_where_an_excluded_path_lives(tmp_path: Path) -> None:
    """入れ替え単位が、除外パスを抱えるところだけ子へ降りること。

    ``user_data`` は配下に ``user_data/logs`` を抱えるのでまるごとにできない。
    ``image`` と ``personas`` は抱えていないのでまるごと 1 単位。
    """
    home = tmp_path / "home"
    _populate_world(home)

    roots = {path.as_posix() for path in snapshot._managed_swap_roots(home)}

    assert roots == {
        "personas",
        "image",
        "user_data/database",
        "user_data/addon_data",
    }


def test_llama_cache_is_never_a_swap_root(tmp_path: Path) -> None:
    """``llama_cache`` が入れ替え・削除の対象にならないこと。

    世界を過去へ戻してもキャッシュは現在のもので構わない。消すと 16GB の
    再生成コストが掛かるので、home 側の現物をそのまま残す。
    """
    home = tmp_path / "home"
    _populate_world(home)

    roots = {path.as_posix() for path in snapshot._managed_swap_roots(home)}

    assert "llama_cache" not in roots
    assert not [r for r in roots if r.startswith("llama_cache/")]


def test_clear_for_restore_keeps_the_preserved_paths(tmp_path: Path) -> None:
    """復元前の掃除が、除外パスだけを残して世界を消すこと。"""
    home = tmp_path / "home"
    _populate_world(home)

    snapshot.clear_for_restore(home)

    assert (home / "llama_cache" / "mira__local-model.bin").is_file()
    assert (home / "backups" / "saiverse.db_backup_startup_1.bak").is_file()
    assert (home / "user_data" / "logs" / "20260902_000000" / "backend.log").is_file()
    assert (home / "log.txt").is_file()
    assert not (home / "personas").exists()
    assert not (home / "image").exists()
    assert not (home / "user_data" / "database").exists()
    assert not (home / "user_data" / "addon_data").exists()


# ---- 中断で残る書きかけアーカイブ ----

def test_cmd_save_removes_leftover_tmp_before_writing(tmp_path: Path, monkeypatch) -> None:
    """前回の中断で残った同名の .zip.tmp を、ZIP を開く前に消していること。"""
    home = tmp_path / "home"
    _write(home / "user_data" / "database" / "saiverse.db", "world db")
    (home / "snapshots").mkdir(parents=True)
    leftover = home / "snapshots" / "world.zip.tmp"
    leftover.write_bytes(b"partially written archive from an interrupted run")

    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    existed_when_writing_started: list[bool] = []

    def _record_then_fail(*args, **kwargs):
        existed_when_writing_started.append(leftover.exists())
        raise RuntimeError("stop before writing")

    with patch.object(snapshot, "is_saiverse_running", return_value=(False, "")), patch.object(
        snapshot.zipfile, "ZipFile", side_effect=_record_then_fail
    ):
        rc = snapshot.cmd_save(argparse.Namespace(name="world", note="", force=False))

    assert rc == 1
    assert existed_when_writing_started == [False]
    assert not leftover.exists()


def test_pre_update_snapshot_failure_removes_partial_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """子プロセスが失敗（タイムアウト kill 含む）したら書きかけ .zip.tmp を消すこと。

    kill された子の except 節は走らないので、掃除できるのは親のこちらだけ。
    """
    home = tmp_path / "home"
    (home / "snapshots").mkdir(parents=True)
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    written: list[Path] = []

    def _fake_run(command, *, cwd, label, timeout=900):
        name = command[3]
        partial = home / "snapshots" / f"{name}.zip.tmp"
        partial.write_bytes(b"20GB of half-written archive, in spirit")
        written.append(partial)
        raise update_engine.UpdateError(f"{label} failed with exit 1: timed out")

    with patch.object(update_engine, "_run", side_effect=_fake_run):
        with pytest.raises(update_engine.UpdateError):
            update_engine.create_pre_update_snapshot(tmp_path / "repo", "python")

    assert len(written) == 1
    assert not written[0].exists()


def test_pre_update_snapshot_cleanup_failure_does_not_hide_the_real_error(
    tmp_path: Path, monkeypatch
) -> None:
    """掃除が失敗しても、元の UpdateError がそのまま上がること。"""
    home = tmp_path / "home"
    (home / "snapshots").mkdir(parents=True)
    monkeypatch.setenv("SAIVERSE_HOME", str(home))

    def _fake_run(command, *, cwd, label, timeout=900):
        raise update_engine.UpdateError("snapshot phase failed with exit 1: timed out")

    with patch.object(update_engine, "_run", side_effect=_fake_run), patch.object(
        update_engine.Path, "unlink", side_effect=OSError("locked by another process")
    ):
        with pytest.raises(update_engine.UpdateError, match="timed out"):
            update_engine.create_pre_update_snapshot(tmp_path / "repo", "python")
