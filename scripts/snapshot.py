"""SAIVerse snapshot/restore tool.

SAIVerse の状態（~/.saiverse/ 配下）を ZIP アーカイブに保存・復元するための
独立スクリプト。バージョン認識基盤（フェーズ0）の前提として、不可逆操作の
テスト・デバッグを安全に行えるようにするのが目的。

SAIVerse 起動状態では DB ロックが取れないため、起動中検出を備える。

設計詳細: docs/intent/version_aware_world_and_persona.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

LOGGER = logging.getLogger("saiverse.snapshot")

# ---- パス解決 ----

REPO_ROOT = Path(__file__).resolve().parent.parent

# ~/.saiverse/ 直下で除外するディレクトリ
EXCLUDED_TOP_DIRS = {"backups", "snapshots"}

# ~/.saiverse/ 直下で除外するファイル
EXCLUDED_TOP_FILES = {"log.txt"}

# ~/.saiverse/user_data/ 配下で除外するディレクトリ
EXCLUDED_USER_DATA_DIRS = {"logs"}


def saiverse_home() -> Path:
    """SAIVerse ホームディレクトリ。SAIVERSE_HOME 環境変数で上書き可能。"""
    env = os.environ.get("SAIVERSE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".saiverse"


def snapshots_dir() -> Path:
    """スナップショット保存先。なければ作成。"""
    d = saiverse_home() / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_saiverse_version() -> str:
    """リポジトリの VERSION ファイルから現在のバージョン文字列を取得。"""
    version_file = REPO_ROOT / "VERSION"
    if version_file.exists():
        try:
            return version_file.read_text(encoding="utf-8").strip() or "unknown"
        except Exception as exc:
            LOGGER.warning("Failed to read VERSION file: %s", exc, exc_info=True)
    return "unknown"


# ---- 起動中検出 ----

# 過去に SQLite の `BEGIN EXCLUSIVE` 試行で起動中検出を試みたが、SAIVerse は
# `SessionLocal()` を都度作って close するパターンのため、リクエストの合間に
# DB はロックされていない瞬間があり、誤って「停止中」と判定される問題があった。
# 代わりに saiverse.db の city.API_PORT を読み出し、各ポートへの TCP 接続を
# 試みる方式に改めた。SQLite 読み込みは並行可能なので起動中でも問題なく取れる。

def is_saiverse_running() -> Tuple[bool, str]:
    """SAIVerse が起動中かを city.API_PORT への TCP 接続試行で判定。

    Returns:
        (True, reason) なら起動中、(False, reason_or_empty) なら停止中
    """
    db_path = saiverse_home() / "user_data" / "database" / "saiverse.db"
    if not db_path.exists():
        LOGGER.debug("saiverse.db not found at %s, treating as not running", db_path)
        return False, ""

    ports = _read_city_api_ports(db_path)
    if not ports:
        LOGGER.debug("No API_PORT entries found in city table")
        return False, ""

    for port in ports:
        if _is_port_open("127.0.0.1", port):
            return True, f"port {port} is open (SAIVerse backend listening)"

    return False, ""


def _read_city_api_ports(db_path: Path) -> List[int]:
    """saiverse.db を読み取り専用で開き、city.API_PORT 列を取得する。

    起動中の SAIVerse がDBを使っていても、SQLite の読み込みは並行可能なので
    ロックでブロックされない。
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError as exc:
        LOGGER.warning("Failed to open saiverse.db read-only: %s", exc, exc_info=True)
        return []

    try:
        try:
            cur = conn.execute("SELECT API_PORT FROM city WHERE API_PORT IS NOT NULL")
        except sqlite3.OperationalError as exc:
            # テーブル/カラムがない（古いスキーマ等）。安全側に倒して空を返す
            LOGGER.warning("Could not query city.API_PORT: %s", exc, exc_info=True)
            return []
        ports: List[int] = []
        for row in cur.fetchall():
            try:
                if row[0] is not None:
                    ports.append(int(row[0]))
            except (TypeError, ValueError) as exc:
                LOGGER.warning("Skipping non-integer API_PORT %r: %s", row[0], exc)
        return ports
    finally:
        conn.close()


def _is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """指定ホスト:ポートに TCP 接続できるかを判定する。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        result = s.connect_ex((host, port))
        is_open = result == 0
        LOGGER.debug("connect_ex(%s, %d) = %d (open=%s)", host, port, result, is_open)
        return is_open
    except OSError as exc:
        LOGGER.warning("Socket error connecting to %s:%d: %s", host, port, exc)
        return False
    finally:
        s.close()


# ---- ファイル収集 ----

@dataclass
class SnapshotEntry:
    abs_path: Path
    archive_path: str  # zip 内のパス（POSIX 形式）


def collect_files_to_snapshot() -> List[SnapshotEntry]:
    """スナップショットに含めるファイルを列挙。

    除外ルール:
    - ~/.saiverse/{backups,snapshots}/ 配下
    - ~/.saiverse/log.txt
    - ~/.saiverse/user_data/logs/ 配下
    """
    home = saiverse_home()
    if not home.exists():
        return []

    entries: List[SnapshotEntry] = []

    for path in home.rglob("*"):
        if not path.is_file():
            continue

        # 直下の除外チェック
        try:
            rel = path.relative_to(home)
        except ValueError:
            continue

        parts = rel.parts
        if not parts:
            continue

        top = parts[0]
        if top in EXCLUDED_TOP_DIRS:
            continue
        if len(parts) == 1 and top in EXCLUDED_TOP_FILES:
            continue

        # user_data/ 配下の除外チェック
        if top == "user_data" and len(parts) >= 2 and parts[1] in EXCLUDED_USER_DATA_DIRS:
            continue

        entries.append(SnapshotEntry(
            abs_path=path,
            archive_path=rel.as_posix(),
        ))

    return entries


# ---- メタ情報 ----

def build_metadata(name: str, note: str, file_count: int, total_bytes: int) -> dict:
    """snapshot.json に書き込むメタ情報を組み立てる。"""
    return {
        "name": name,
        "created_at": datetime.now().astimezone().isoformat(),
        "saiverse_version": get_saiverse_version(),
        "note": note,
        "file_count": file_count,
        "total_bytes_uncompressed": total_bytes,
        # 将来 LAST_KNOWN_VERSION カラムが追加されたらここに city_versions / persona_versions を載せる
    }


def read_snapshot_metadata(snap_path: Path) -> Optional[dict]:
    """ZIP からメタ情報を読み込む。失敗時 None。"""
    try:
        with zipfile.ZipFile(snap_path, "r") as zf:
            return json.loads(zf.read("snapshot.json").decode("utf-8"))
    except KeyError:
        LOGGER.warning("snapshot.json not found in %s", snap_path)
        return None
    except Exception as exc:
        LOGGER.warning("Failed to read metadata from %s: %s", snap_path, exc, exc_info=True)
        return None


# ---- ファイルシステム操作 ----

def remove_path(path: Path) -> None:
    """ファイル/ディレクトリを再帰削除。"""
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def clear_for_restore(home: Path) -> None:
    """復元前に ~/.saiverse/ の中身を削除（除外ディレクトリは残す）。"""
    if not home.exists():
        return

    for child in list(home.iterdir()):
        name = child.name
        if name in EXCLUDED_TOP_DIRS:
            LOGGER.debug("Preserving excluded top dir: %s", child)
            continue
        if name in EXCLUDED_TOP_FILES:
            LOGGER.debug("Preserving excluded top file: %s", child)
            continue

        if name == "user_data" and child.is_dir():
            # user_data 配下は logs だけ残して他は削除
            for sub in list(child.iterdir()):
                if sub.name in EXCLUDED_USER_DATA_DIRS:
                    LOGGER.debug("Preserving excluded user_data subdir: %s", sub)
                    continue
                LOGGER.debug("Removing %s", sub)
                remove_path(sub)
        else:
            LOGGER.debug("Removing %s", child)
            remove_path(child)


# ---- コマンド本体 ----

def cmd_save(args: argparse.Namespace) -> int:
    name = args.name
    if not _is_valid_snapshot_name(name):
        print(f"ERROR: Invalid snapshot name '{name}' (use letters/digits/_-./).", file=sys.stderr)
        return 2

    note = args.note or ""
    snap_path = snapshots_dir() / f"{name}.zip"

    if snap_path.exists() and not args.force:
        print(f"ERROR: Snapshot '{name}' already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    running, reason = is_saiverse_running()
    if running:
        print(f"WARNING: SAIVerse appears to be running ({reason}).", file=sys.stderr)
        if not args.force:
            print("        Stop SAIVerse before taking a snapshot, or pass --force.", file=sys.stderr)
            return 1
        print("        Proceeding due to --force. The snapshot may be inconsistent.", file=sys.stderr)

    home = saiverse_home()
    if not home.exists():
        print(f"ERROR: SAIVerse home not found: {home}", file=sys.stderr)
        return 1

    print(f"Collecting files from {home} ...")
    entries = collect_files_to_snapshot()
    total_bytes = sum(e.abs_path.stat().st_size for e in entries if e.abs_path.exists())
    print(f"  -> {len(entries)} files, {total_bytes / (1024 * 1024):.1f} MB uncompressed")

    metadata = build_metadata(name=name, note=note, file_count=len(entries), total_bytes=total_bytes)

    tmp_path = snap_path.with_suffix(".zip.tmp")
    failed: List[Tuple[Path, str]] = []
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for entry in entries:
                try:
                    zf.write(entry.abs_path, entry.archive_path)
                except Exception as exc:
                    LOGGER.warning("Failed to add %s: %s", entry.abs_path, exc, exc_info=True)
                    failed.append((entry.abs_path, str(exc)))
            zf.writestr("snapshot.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        # アトミックに置き換え
        if snap_path.exists():
            snap_path.unlink()
        tmp_path.rename(snap_path)
    except Exception as exc:
        LOGGER.error("Failed to write snapshot: %s", exc, exc_info=True)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        print(f"ERROR: Snapshot creation failed: {exc}", file=sys.stderr)
        return 1

    size_mb = snap_path.stat().st_size / (1024 * 1024)
    print(f"OK: Snapshot saved: {snap_path}")
    print(f"    SAIVerse version: {metadata['saiverse_version']}")
    print(f"    Compressed size:  {size_mb:.1f} MB")
    if failed:
        print(f"    WARNING: {len(failed)} files failed to add (see logs)", file=sys.stderr)
        for path, reason in failed[:10]:
            print(f"      - {path}: {reason}", file=sys.stderr)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    snaps = sorted(snapshots_dir().glob("*.zip"))
    if not snaps:
        print("No snapshots found in", snapshots_dir())
        return 0

    print(f"{'Name':<35} {'Created':<22} {'Version':<10} {'Size':>10}")
    print("-" * 80)
    for snap in snaps:
        name = snap.stem
        size_mb = snap.stat().st_size / (1024 * 1024)
        meta = read_snapshot_metadata(snap)
        if meta:
            created = meta.get("created_at", "?")[:19]
            version = meta.get("saiverse_version", "?")
        else:
            created = "?"
            version = "?"
        print(f"{name:<35} {created:<22} {version:<10} {size_mb:>7.1f} MB")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    snap = snapshots_dir() / f"{args.name}.zip"
    if not snap.exists():
        print(f"ERROR: Snapshot '{args.name}' not found.", file=sys.stderr)
        return 1

    meta = read_snapshot_metadata(snap)
    if meta is None:
        print("ERROR: Failed to read snapshot metadata.", file=sys.stderr)
        return 1

    try:
        with zipfile.ZipFile(snap, "r") as zf:
            namelist = [n for n in zf.namelist() if n != "snapshot.json"]
    except Exception as exc:
        print(f"ERROR: Failed to read archive: {exc}", file=sys.stderr)
        return 1

    print("=== Snapshot Metadata ===")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print()
    print(f"=== Archive contents ({len(namelist)} files) ===")
    if args.files:
        for n in namelist:
            print(f"  {n}")
    else:
        # トップレベルのディレクトリ別件数だけ表示
        top_counts: dict = {}
        for n in namelist:
            top = n.split("/", 1)[0]
            top_counts[top] = top_counts.get(top, 0) + 1
        for top, count in sorted(top_counts.items()):
            print(f"  {top}/  ({count} files)")
        print("  (use --files to list all)")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    snap = snapshots_dir() / f"{args.name}.zip"
    if not snap.exists():
        print(f"ERROR: Snapshot '{args.name}' not found.", file=sys.stderr)
        return 1

    running, reason = is_saiverse_running()
    if running:
        print(f"ERROR: SAIVerse appears to be running ({reason}).", file=sys.stderr)
        if not args.force:
            print("       Stop SAIVerse before restoring, or pass --force.", file=sys.stderr)
            return 1
        print("       WARNING: Proceeding due to --force. This may corrupt data.", file=sys.stderr)

    # メタ情報を先に読んで confirm
    meta = read_snapshot_metadata(snap)
    if meta is None:
        print("ERROR: Snapshot is unreadable, refusing to restore.", file=sys.stderr)
        return 1

    print("About to restore from snapshot:")
    print(f"  Name:     {meta.get('name')}")
    print(f"  Created:  {meta.get('created_at')}")
    print(f"  Version:  {meta.get('saiverse_version')}")
    print(f"  Files:    {meta.get('file_count')}")
    print()

    if not args.yes:
        print("Type 'RESTORE' to confirm: ", end="", flush=True)
        try:
            ans = input().strip()
        except EOFError:
            ans = ""
        if ans != "RESTORE":
            print("Cancelled.")
            return 0

    # 復元前自動スナップショット
    if not args.no_auto_snapshot:
        auto_name = f"auto_before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print(f"Creating auto-snapshot of current state: {auto_name}")
        auto_args = argparse.Namespace(
            name=auto_name,
            note=f"Auto-saved before restoring '{args.name}'",
            force=False,
        )
        rc = cmd_save(auto_args)
        if rc != 0:
            print("ERROR: Failed to auto-snapshot. Use --no-auto-snapshot to skip.", file=sys.stderr)
            return rc
        print()

    home = saiverse_home()
    home.mkdir(parents=True, exist_ok=True)

    print(f"Clearing target ({home}) ...")
    try:
        clear_for_restore(home)
    except Exception as exc:
        LOGGER.error("Failed during clear phase: %s", exc, exc_info=True)
        print(f"ERROR: Failed to clear target: {exc}", file=sys.stderr)
        return 1

    print(f"Extracting {snap} ...")
    try:
        with zipfile.ZipFile(snap, "r") as zf:
            for member in zf.namelist():
                if member == "snapshot.json":
                    continue
                zf.extract(member, home)
    except Exception as exc:
        LOGGER.error("Extraction failed: %s", exc, exc_info=True)
        print(f"ERROR: Extraction failed: {exc}", file=sys.stderr)
        print("       The auto-snapshot 'auto_before_restore_*' can be used to recover.", file=sys.stderr)
        return 1

    print(f"OK: Restored from snapshot '{args.name}'.")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    snap = snapshots_dir() / f"{args.name}.zip"
    if not snap.exists():
        print(f"ERROR: Snapshot '{args.name}' not found.", file=sys.stderr)
        return 1

    if not args.yes:
        print(f"Delete snapshot '{args.name}'? (y/N): ", end="", flush=True)
        try:
            ans = input().strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("Cancelled.")
            return 0

    snap.unlink()
    print(f"OK: Deleted snapshot '{args.name}'.")
    return 0


# ---- ヘルパ ----

_VALID_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


def _is_valid_snapshot_name(name: str) -> bool:
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    return all(c in _VALID_NAME_CHARS for c in name)


# ---- エントリポイント ----

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="SAIVerse snapshot/restore tool. Operates on ~/.saiverse/ "
                    "(or $SAIVERSE_HOME). SAIVerse should be stopped before use.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("save", help="Create a new snapshot")
    p.add_argument("name", help="Snapshot name (letters/digits/_-./)")
    p.add_argument("--note", help="Free-form note stored in metadata")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing snapshot, or proceed despite running SAIVerse")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("list", help="List all snapshots")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("inspect", help="Show snapshot metadata and content summary")
    p.add_argument("name", help="Snapshot name")
    p.add_argument("--files", action="store_true", help="List all files in archive")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("restore", help="Restore from a snapshot (clears current state first)")
    p.add_argument("name", help="Snapshot name")
    p.add_argument("--force", action="store_true", help="Proceed despite running SAIVerse")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p.add_argument("--no-auto-snapshot", action="store_true",
                   help="Skip auto-snapshot of current state before restore")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("delete", help="Delete a snapshot")
    p.add_argument("name", help="Snapshot name")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p.set_defaults(func=cmd_delete)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
