#!/usr/bin/env python
"""本番世界を丸ごとテスト環境へ複製するスクリプト (world clone)。

ペルソナ単体複製 (clone_persona_to_test_env.py) の「記憶は本物、世界は書き割り」
問題への回答。「何をコピーするか」の列挙をやめ、全部コピーして「何をリセット/
停止するか」だけを管理する。設計意図と住み分けは docs/intent/sandbox_world_clone.md。

Usage:
    python scripts/clone_world_to_test_env.py --persona <id> [--persona <id> ...]
        [--source-db <path>]   # 省略時: 本番の default_db_path()
        [--source-home <path>] # 省略時: 本番の SAIVERSE_HOME (~/.saiverse)
        [--dest-db <path>]     # 省略時: test_data/user_data/database/saiverse.db
        [--dest-home <path>]   # 省略時: test_data/.saiverse
        [--ui-port 18000] [--api-port 18001]
        [--keep-addons]        # addon_config を無効化しない
        [--no-media]           # home の documents/ image/ をコピーしない
        [--force]              # 既存複製の上書き確認をスキップ

安全性:
    - source (本番) は読み取り専用。DB スナップショットは sqlite backup API で
      取得する (本番稼働中でも WAL ごと整合する)
    - dest が source と同一パスなら拒否
    - 複製世界は起動しても外に触れない: アドオン全無効化 / オンラインモード解除 /
      他 City トランザクション消去 / ポート書き換え (本番と同時起動可能)
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOGGER = logging.getLogger("scripts.clone_world_to_test_env")


class CloneError(RuntimeError):
    """複製処理の前提が満たされていない場合のエラー (メッセージをそのまま表示)。"""


#: user_data からコピーする資源ディレクトリ (存在するもののみ)
USER_DATA_RESOURCE_DIRS = [
    "models", "providers", "playbooks", "tools", "prompts", "phenomena", "icons",
]

#: home 直下からコピーするメディアディレクトリ (Item.FILE_PATH が home 相対で指す)
HOME_MEDIA_DIRS = ["documents", "image"]

#: sqlite の WAL サイドカー (backup API がスナップショットへ取り込むため個別コピー不要)
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm")


# ---------------------------------------------------------------------------
# パス解決 (clone_persona_to_test_env.py と同じ流儀)
# ---------------------------------------------------------------------------


def _default_source_db() -> Path:
    from database.paths import default_db_path
    return default_db_path()


def _default_source_home() -> Path:
    from saiverse.data_paths import get_saiverse_home
    return get_saiverse_home()


def _user_data_dir_for(db_path: Path, home: Path) -> Path:
    if db_path.parent.name == "database":
        return db_path.parent.parent
    return home / "user_data"


# ---------------------------------------------------------------------------
# DB スナップショットとリセット
# ---------------------------------------------------------------------------


def _snapshot_sqlite(source: Path, dest: Path) -> None:
    """sqlite backup API で整合スナップショットを取る (source は ro で開く)。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    for suffix in _DB_SIDECAR_SUFFIXES:
        sidecar = dest.with_name(dest.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    src_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _reset_runtime_state(
    dest_db: Path,
    target_personas: List[str],
    *,
    ui_port: int,
    api_port: int,
    disable_addons: bool,
) -> Dict[str, Any]:
    """dest DB の実行時状態をリセットする (intent doc §4 の表が正典)。"""
    report: Dict[str, Any] = {}
    conn = sqlite3.connect(str(dest_db))
    try:
        # AI: 実行時カウンタのリセット
        conn.execute(
            "UPDATE ai SET IS_DISPATCHED = 0, AUTO_COUNT = 0, LAST_AUTO_PROMPT_TIMES = NULL"
        )
        # AI: 非対象ペルソナは停止 (コスト暴発 + memory.db 不在ペルソナの誤動作防止)
        placeholders = ",".join("?" for _ in target_personas) or "''"
        cur = conn.execute(
            f"UPDATE ai SET ACTIVITY_STATE = 'Stop' WHERE AIID NOT IN ({placeholders})",
            target_personas,
        )
        report["stopped_personas"] = cur.rowcount

        # City: ポート書き換え + オンラインモード解除 (複数 City は +10 ずつ)
        city_rows = conn.execute("SELECT CITYID, CITYNAME FROM city ORDER BY CITYID").fetchall()
        port_map = {}
        for i, (city_id, city_name) in enumerate(city_rows):
            new_ui, new_api = ui_port + i * 10, api_port + i * 10
            conn.execute(
                "UPDATE city SET UI_PORT = ?, API_PORT = ?, START_IN_ONLINE_MODE = 0"
                " WHERE CITYID = ?",
                (new_ui, new_api, city_id),
            )
            port_map[city_name] = (new_ui, new_api)
        report["city_ports"] = port_map

        # 他 City とのトランザクション残骸
        report["visiting_ai_deleted"] = conn.execute("DELETE FROM visiting_ai").rowcount
        report["thinking_request_deleted"] = conn.execute("DELETE FROM thinking_request").rowcount

        # アドオン無効化 (外部サービス・実デバイスへの作用を遮断)
        if disable_addons and _table_exists(conn, "addon_config"):
            cur = conn.execute("UPDATE addon_config SET is_enabled = 0")
            report["addons_disabled"] = cur.rowcount
        else:
            report["addons_disabled"] = 0 if disable_addons else "kept"

        conn.commit()
    finally:
        conn.close()
    return report


def _read_source_personas(source_db: Path) -> Dict[str, str]:
    """source の全ペルソナ {AIID: ACTIVITY_STATE} を読む (存在検証用)。"""
    conn = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    try:
        return {
            row[0]: row[1]
            for row in conn.execute("SELECT AIID, ACTIVITY_STATE FROM ai")
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ディレクトリコピー
# ---------------------------------------------------------------------------


def _copy_tree(source_dir: Path, dest_dir: Path, *, label: str) -> int:
    """ディレクトリを複製する。sqlite (*.db) は backup API、他は copy2。

    WAL サイドカー (*.db-wal / *.db-shm) はスキップ (backup が取り込むため)。
    Returns: コピーしたファイル数。
    """
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    files = sorted(p for p in source_dir.rglob("*") if p.is_file())
    count = 0
    for src_file in files:
        name = src_file.name
        if name.endswith(_DB_SIDECAR_SUFFIXES) and ".db" in name:
            continue
        target = dest_dir / src_file.relative_to(source_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        if src_file.suffix == ".db":
            _snapshot_sqlite(src_file, target)
        else:
            shutil.copy2(src_file, target)
        count += 1
    LOGGER.info("%s: %d ファイルをコピー (%s → %s)", label, count, source_dir, dest_dir)
    return count


def _sync_tree(source_dir: Path, dest_dir: Path, *, label: str) -> int:
    """メディア用の増分同期 (size + mtime 一致はスキップ、source に無い dest は削除)。

    image/ は GB 級になるため、再クローンのたびに全コピーしない。copy2 が
    mtime を保存するので (size, mtime) 比較は健全。
    Returns: 実際にコピーしたファイル数。
    """
    source_files = {p.relative_to(source_dir): p for p in source_dir.rglob("*") if p.is_file()}
    copied = 0
    skipped = 0
    for rel, src_file in sorted(source_files.items()):
        target = dest_dir / rel
        if target.is_file():
            src_stat, dst_stat = src_file.stat(), target.stat()
            if src_stat.st_size == dst_stat.st_size and int(src_stat.st_mtime) == int(dst_stat.st_mtime):
                skipped += 1
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, target)
        copied += 1
    # source に存在しない dest 側ファイルを削除 (前回クローンの残骸)
    if dest_dir.is_dir():
        for p in sorted(dest_dir.rglob("*"), reverse=True):
            if p.is_file() and p.relative_to(dest_dir) not in source_files:
                p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
    LOGGER.info("%s: %d コピー / %d スキップ (増分同期)", label, copied, skipped)
    return copied


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------


def _default_confirm(message: str) -> bool:
    answer = input(f"{message} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def clone_world(
    target_personas: List[str],
    source_db: Path,
    source_home: Path,
    dest_db: Path,
    dest_home: Path,
    *,
    ui_port: int = 18000,
    api_port: int = 18001,
    keep_addons: bool = False,
    include_media: bool = True,
    force: bool = False,
    confirm: Callable[[str], bool] = _default_confirm,
) -> Dict[str, Any]:
    """本番世界を丸ごとテスト環境へ複製する。

    Returns:
        サマリ情報の dict。
    Raises:
        CloneError: 前提未達 (source 不在 / dest==source / ペルソナ不在 / 中断)。
    """
    source_db = Path(source_db).resolve()
    dest_db = Path(dest_db).resolve()
    source_home = Path(source_home).resolve()
    dest_home = Path(dest_home).resolve()

    LOGGER.info("複製元 DB   : %s", source_db)
    LOGGER.info("複製元 home : %s", source_home)
    LOGGER.info("複製先 DB   : %s", dest_db)
    LOGGER.info("複製先 home : %s", dest_home)

    if source_db == dest_db or source_home == dest_home:
        raise CloneError("source と dest が同一です。本番を破壊するため拒否します。")
    if not source_db.is_file():
        raise CloneError(f"source DB が見つかりません: {source_db}")

    # 対象ペルソナの存在検証 (typo で全員 Stop の世界ができるのを防ぐ)
    source_personas = _read_source_personas(source_db)
    missing = [p for p in target_personas if p not in source_personas]
    if missing:
        raise CloneError(
            f"source DB に存在しないペルソナ: {missing}。"
            f"存在するのは: {sorted(source_personas)}"
        )

    # 上書き確認
    if (dest_db.exists() or dest_home.exists()) and not force:
        if not confirm(
            f"テスト環境が既に存在します ({dest_db.parent.parent})。全て置き換えますか?"
        ):
            raise CloneError("ユーザーにより中断されました (--force で確認をスキップできます)。")

    # --- 1. saiverse.db のスナップショット ---
    LOGGER.info("saiverse.db をスナップショット中 (%.1f MB)...",
                source_db.stat().st_size / (1024 * 1024))
    _snapshot_sqlite(source_db, dest_db)

    # --- 2. 実行時状態のリセット ---
    reset_report = _reset_runtime_state(
        dest_db, target_personas,
        ui_port=ui_port, api_port=api_port, disable_addons=not keep_addons,
    )

    # --- 3. user_data 資源ディレクトリ ---
    source_user_data = _user_data_dir_for(source_db, source_home)
    dest_user_data = _user_data_dir_for(dest_db, dest_home)
    copied_dirs: Dict[str, int] = {}
    for name in USER_DATA_RESOURCE_DIRS:
        src_dir = source_user_data / name
        if src_dir.is_dir():
            copied_dirs[f"user_data/{name}"] = _copy_tree(
                src_dir, dest_user_data / name, label=f"user_data/{name}",
            )

    # --- 4. メディアディレクトリ (Item.FILE_PATH は home 相対) ---
    if include_media:
        for name in HOME_MEDIA_DIRS:
            src_dir = source_home / name
            if src_dir.is_dir():
                copied_dirs[name] = _sync_tree(src_dir, dest_home / name, label=name)

    # --- 5. 対象ペルソナディレクトリ ---
    persona_reports: Dict[str, Any] = {}
    for persona_id in target_personas:
        src_dir = source_home / "personas" / persona_id
        if not src_dir.is_dir():
            LOGGER.warning(
                "ペルソナディレクトリがありません: %s (memory.db 無しで続行)", src_dir,
            )
            persona_reports[persona_id] = 0
            continue
        persona_reports[persona_id] = _copy_tree(
            src_dir, dest_home / "personas" / persona_id, label=f"personas/{persona_id}",
        )

    summary = {
        "targets": target_personas,
        "reset": reset_report,
        "copied_dirs": copied_dirs,
        "personas": persona_reports,
        "dest_db": str(dest_db),
        "dest_home": str(dest_home),
    }
    _print_summary(summary)
    return summary


def _print_summary(summary: Dict[str, Any]) -> None:
    reset = summary["reset"]
    LOGGER.info("=" * 64)
    LOGGER.info("世界複製完了 — 対象ペルソナ: %s", ", ".join(summary["targets"]) or "(なし)")
    LOGGER.info("-" * 64)
    LOGGER.info("City ポート: %s", reset["city_ports"])
    LOGGER.info("停止した非対象ペルソナ: %d 体", reset["stopped_personas"])
    LOGGER.info("アドオン無効化: %s / visiting_ai 消去: %d / thinking_request 消去: %d",
                reset["addons_disabled"], reset["visiting_ai_deleted"],
                reset["thinking_request_deleted"])
    for name, count in summary["copied_dirs"].items():
        LOGGER.info("コピー %-24s: %d ファイル", name, count)
    for pid, count in summary["personas"].items():
        LOGGER.info("ペルソナ %-22s: %d ファイル", pid, count)
    LOGGER.info("-" * 64)
    LOGGER.info("次の手順:")
    LOGGER.info("  - 検分: python scripts/inspect_world.py personas --env test")
    LOGGER.info("  - 一日シム: SAIVERSE_HOME=%s SAIVERSE_USER_DATA_DIR=%s \\",
                summary["dest_home"], str(Path(summary["dest_db"]).parent.parent))
    LOGGER.info("      python scripts/run_day_sim.py --scenario <file> --real"
                " --city <CITYNAME> --db-file %s", summary["dest_db"])
    LOGGER.info("  - 判断点 playbook が本番 DB 未 import の場合のみ import_playbook.py を dest へ")
    LOGGER.info("=" * 64)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="本番世界を丸ごとテスト環境へ複製する (本番側は読み取りのみ)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--persona", action="append", default=[],
                        help="動かす対象ペルソナ ID (複数指定可)。指定外は ACTIVITY_STATE=Stop")
    parser.add_argument("--source-db", type=Path, default=None)
    parser.add_argument("--source-home", type=Path, default=None)
    parser.add_argument("--dest-db", type=Path,
                        default=ROOT / "test_data" / "user_data" / "database" / "saiverse.db")
    parser.add_argument("--dest-home", type=Path,
                        default=ROOT / "test_data" / ".saiverse")
    parser.add_argument("--ui-port", type=int, default=18000)
    parser.add_argument("--api-port", type=int, default=18001)
    parser.add_argument("--keep-addons", action="store_true",
                        help="addon_config を無効化しない (アドオン自体のテスト用)")
    parser.add_argument("--no-media", action="store_true",
                        help="home の documents/ image/ をコピーしない")
    parser.add_argument("--force", action="store_true",
                        help="既存テスト環境の上書き確認をスキップ")
    args = parser.parse_args(argv)

    # Windows コンソール (cp932) 対策 (ログは stderr に出る)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    source_db = args.source_db if args.source_db is not None else _default_source_db()
    source_home = args.source_home if args.source_home is not None else _default_source_home()

    try:
        clone_world(
            args.persona,
            source_db=source_db,
            source_home=source_home,
            dest_db=args.dest_db,
            dest_home=args.dest_home,
            ui_port=args.ui_port,
            api_port=args.api_port,
            keep_addons=args.keep_addons,
            include_media=not args.no_media,
            force=args.force,
        )
    except CloneError as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
