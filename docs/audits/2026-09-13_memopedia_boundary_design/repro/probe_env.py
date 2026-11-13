"""隔離環境の共通セットアップ (このディレクトリの実験 exp1〜exp6 が共用する)。

このファイルがしていること:

- ``SAIVERSE_HOME`` / ``SAIVERSE_USER_DATA_DIR`` を一時ディレクトリへ向ける
  (``saiverse.data_paths`` を import するより前に呼ぶこと — ``USER_DATA_DIR`` は
  モジュール読み込み時に env を読んで確定する)。
- 本番 (``~/.saiverse``) を指していないことを実行時に検査する
  (``guard_not_production`` / ``guard_db_path``)。検査に落ちたら AssertionError で止まる。
- 埋め込みモデルの読み込みを止める (``use_dummy_embedder``)。
  実験は意味検索を見ないので、埋め込みは 0 ベクトルの偽物で足りる。
  本物を読むと ONNX のダウンロードが起きうるので、必ずこちらを使う。

LLM は呼ばない。バックエンドは起動しない。本番ペルソナには触れない。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# このファイルは <repo>/docs/audits/<調査>/repro/ に置かれる想定なので、
# リポジトリの根は 4 つ上 (repro → 調査 → audits → docs → 根)。
# 調査セッション固有のパスを埋め込まない (誰の環境でも再実行できるように)。
REPO = Path(__file__).resolve().parents[4]

# 実験用の隔離ホームの置き場。既定は OS の一時領域だが、SAIVERSE_PROBE_ROOT で
# 上書きできる。**本番 (~/.saiverse) を指していないことは guard_not_production が検査する。**
SCRATCH = Path(
    os.environ.get("SAIVERSE_PROBE_ROOT")
    or (Path(tempfile.gettempdir()) / "saiverse_probe")
)


def _guard_scratch_root() -> None:
    """隔離領域そのものが本番を指していないことを、何かを作るより前に確かめる。

    ``SAIVERSE_PROBE_ROOT`` で上書きできる以上、ディレクトリを作る前に見る必要がある。
    この検査はモジュールの読み込み時に 1 回走る。
    """
    root = SCRATCH.resolve()
    real = (Path.home() / ".saiverse").resolve()
    assert root != real, f"REFUSED: probe root is production {root}"
    assert real not in root.parents, f"REFUSED: probe root is under production {root}"


_guard_scratch_root()


def setup_home(name: str, *, fresh: bool = True) -> Path:
    """``SAIVERSE_HOME`` を <隔離ホームの置き場>/<name>_home へ固定し、そのパスを返す。

    ``fresh=True`` (既定) は、そのディレクトリを毎回まっさらにしてから使う。
    前回の実行で残った DB を読んでしまうと、観測が前回の結果に汚染される。
    """
    home = SCRATCH / f"{name}_home"
    if fresh and home.exists():
        _guard_path_is_scratch(home)
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    os.environ["SAIVERSE_HOME"] = str(home)
    os.environ["SAIVERSE_USER_DATA_DIR"] = str(home / "user_data")
    # 記憶機能を明示的に有効化 (既定も True だが、既存テストの fixture に合わせる)
    os.environ["SAIMEMORY_MEMORY"] = "1"
    # 起動時バックアップスレッドを確実に止める (隔離環境でも余計な I/O をしない)
    os.environ["SAIMEMORY_BACKUP_ON_START"] = "0"
    os.environ["SAIVERSE_DB_BACKUP_ON_START"] = "0"
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return home


def _guard_path_is_scratch(path: Path) -> None:
    resolved = path.resolve()
    real = (Path.home() / ".saiverse").resolve()
    assert resolved != real, f"REFUSED: path is production {resolved}"
    assert real not in resolved.parents, f"REFUSED: path is under production {resolved}"
    assert resolved.is_relative_to(SCRATCH.resolve()), (
        f"REFUSED: path is outside the probe area {resolved}"
    )


def guard_not_production() -> None:
    """``SAIVERSE_HOME`` が本番 (``~/.saiverse``) を指していないことを確かめる。"""
    _guard_path_is_scratch(Path(os.environ["SAIVERSE_HOME"]))


def guard_db_path(db_path: "str | Path") -> str:
    """実際に開かれた DB ファイルが隔離領域の中にあることを確かめ、そのパスを返す。

    ``SAIVERSE_HOME`` を立てただけで安心しない — アダプタが別の場所へ
    fallback していないかを、開いた後の実パスで見る。
    """
    resolved = Path(db_path).resolve()
    _guard_path_is_scratch(resolved)
    return str(resolved)


class DummyEmbedder:
    """埋め込みを 0 ベクトルで返す偽物 (本物のモデルを読み込ませないため)。

    ``tests/test_perception_rendering.py`` の ``_DummyEmbedder`` と同じ形。
    """

    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def use_dummy_embedder():
    """``saiverse_memory.adapter`` が使う Embedder を偽物へ差し替える。

    ``setup_home`` の後、``SAIMemoryAdapter`` を作るより前に呼ぶこと。
    差し替えを止める必要はない (実験プロセスはここで終わる) ので、
    呼び出し側は戻り値を無視してよい。
    """
    from unittest.mock import patch

    patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
    patcher.start()
    return patcher


def table_counts(db_path: Path) -> dict:
    """DB の全テーブル (VIEW を除く) の行数を返す。"""
    import sqlite3

    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        out = {}
        for n in names:
            try:
                out[n] = conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
            except Exception as exc:  # noqa: BLE001
                out[n] = f"ERROR: {exc}"
        return out
    finally:
        conn.close()
