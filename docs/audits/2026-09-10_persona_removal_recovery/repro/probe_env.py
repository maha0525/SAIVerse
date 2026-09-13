"""隔離環境の共通セットアップ (実験 2/3/1 共用)。

- SAIVERSE_HOME を一時ディレクトリへ向ける (saiverse.data_paths の import より前)。
- ~/.saiverse/ には一切触れない。
- LLM は呼ばない。埋め込みはローカル ONNX (sbert/multilingual-e5-small) のみ。
"""
from __future__ import annotations

import os
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


def setup_home(name: str) -> Path:
    """SAIVERSE_HOME を <隔離ホームの置き場>/<name>_home へ固定し、そのパスを返す。"""
    home = SCRATCH / f"{name}_home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["SAIVERSE_HOME"] = str(home)
    os.environ["SAIVERSE_USER_DATA_DIR"] = str(home / "user_data")
    # 起動時バックアップスレッドを確実に止める (隔離環境でも余計な I/O をしない)
    os.environ["SAIMEMORY_BACKUP_ON_START"] = "0"
    os.environ["SAIVERSE_DB_BACKUP_ON_START"] = "0"
    # 埋め込みはローカル snapshot のみ (オンラインダウンロードを起こさせない)
    os.environ.setdefault(
        "SAIMEMORY_EMBED_MODEL_PATH", str(REPO / "sbert" / "multilingual-e5-small")
    )
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return home


def guard_not_production() -> None:
    """SAIVERSE_HOME が本番 (~/.saiverse) を指していないことを確かめる。"""
    home = Path(os.environ["SAIVERSE_HOME"]).resolve()
    real = (Path.home() / ".saiverse").resolve()
    assert home != real, f"REFUSED: SAIVERSE_HOME points at production {home}"
    # 本番の下に潜り込んでいないことも見る (~/.saiverse/probe_home のような形)
    assert real not in home.parents, f"REFUSED: home is under production {home}"
    assert home.is_relative_to(SCRATCH.resolve()), f"REFUSED: unexpected home {home}"


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
