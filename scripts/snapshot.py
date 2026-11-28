"""SAIVerse snapshot/restore tool.

SAIVerse の状態（~/.saiverse/ 配下）をコンテンツアドレッサブル倉庫 (CAS: format_version 3)
へ保存・復元するための独立スクリプト。既存の ZIP アーカイブ (format_version 2) も
そのまま読み取り・復元・削除できる。

SAIVerse 起動状態では DB ロックが取れないため、起動中検出を備える。

設計詳細: docs/intent/world_snapshot.md
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
import uuid
import zipfile
import zlib

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

LOGGER = logging.getLogger("saiverse.snapshot")

# ---- パス解決 ----

REPO_ROOT = Path(__file__).resolve().parent.parent

# `python scripts/snapshot.py` として起動されると sys.path[0] は scripts/ になり、
# saiverse パッケージ (is_saiverse_running が使う runtime_marker) が import できない。
# update_engine がサブプロセスとして呼ぶ経路がまさにこれで、venv に editable install が
# 無い素のユーザー環境では起動中検出が ModuleNotFoundError で必ず落ちる。
# 他の scripts/*.py と同じくリポジトリルートを自分で通す。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# --- 除外の集合は 2 つある。用途が違うので、混ぜてはいけない ---
#
# どちらも ~/.saiverse/ からの相対パスで書く。ディレクトリとファイルを区別せず、
# 「相対パスの先頭がこの集合のいずれかに一致するか」だけで判定するので、階層が
# 深くなっても判定規則は増えない。

# save でアーカイブに入れず、restore で home 側にそのまま残すもの。
# collect_files_to_snapshot / clear_for_restore / _managed_swap_roots が使う。
#
# ここに足してよいのは「再生成できるキャッシュ」と「世界の状態ではない運用
# ファイル」だけ。personas / cities / buildings / image 本体 /
# user_data/database (ライブ DB 本体) / user_data/addon_data / qdrant /
# documents / video / audio は世界そのものなので、決して足さないこと。
#
# 足すときは容量だけで決めない。除外は復元時の入れ替え単位も分割する
# （親ディレクトリが「まるごと」で扱えなくなり、os.rename の回数がその
# ディレクトリの子の数まで増えて、部分失敗の窓が広がる）。実測 3MB の
# image/.thumbnails は、この代償に見合わないので入れていない。
EXCLUDED_FROM_SNAPSHOT = frozenset({
    "backups",            # DB バックアップ（スナップショットとは別系統の保全）
    "snapshots",          # スナップショット自身
    ".runtime",           # 起動中マーカー
    "log.txt",
    ".runtime.json",
    "user_data/logs",     # セッションログ
    "llama_cache",        # llama.cpp の KV スロットキャッシュ（実測 15.8GB）。復元に
                          # 失敗しても warning を出して fresh に続行する設計で、無くても
                          # 動く。復元時も home 側の現物を残す（世界を過去へ戻しても
                          # キャッシュは現在のものでよく、消すと再生成コストが大きい）
    "addon_install",      # アドオン専用の Python 環境と導入時の答え。expansion_data/ の
                          # アドオンと同じく世界の状態ではなく再生成できる導入物で、
                          # 環境は数 GB になる。答えも同じ場所に置き、戻しても実際の
                          # 環境と食い違わないようにしている（addon_data/ とは別物）
})

# 復元時、アーカイブのメンバーとして入っていたら危険として拒否するもの。
# _safe_archive_member だけが使う。
#
# EXCLUDED_FROM_SNAPSHOT から llama_cache を引いただけ。llama_cache を含めないのは、
# それを除外していなかった頃の format_version 2 アーカイブに llama_cache/... が正当な
# メンバーとして入っており、拒否すると過去のスナップショットの復元が丸ごと失敗する
# ため。展開先は一時的な stage で、_managed_swap_roots が llama_cache を入れ替え単位に
# 含めないので、stage に展開されても home の llama_cache は入れ替わらない（stage ごと
# 捨てられる）。
#
# user_data/database/*.bak* も同じく、移行前の過去アーカイブに入っている可能性があるため
# PROTECTED_FROM_ARCHIVE には含めない（_is_excluded_db_backup で save 除外・restore 保全）。
PROTECTED_FROM_ARCHIVE = EXCLUDED_FROM_SNAPSHOT - {"llama_cache"}


def _as_parts(paths: frozenset[str]) -> frozenset[Tuple[str, ...]]:
    """判定用に、相対パス文字列の集合を parts のタプルの集合へ落とす。"""
    return frozenset(tuple(p.split("/")) for p in paths)


_EXCLUDED_PARTS = _as_parts(EXCLUDED_FROM_SNAPSHOT)
_PROTECTED_PARTS = _as_parts(PROTECTED_FROM_ARCHIVE)

# ZIP 形式 (従来) の format_version と、CAS 形式 (現行) の format_version。
# 既存テスト互換のため SNAPSHOT_FORMAT_VERSION は ZIP 形式 (2) のまま維持する。
SNAPSHOT_FORMAT_VERSION = 2
CAS_SNAPSHOT_FORMAT_VERSION = 3

STORE_DIR_NAME = ".store"
OBJECTS_DIR_NAME = "objects"
MANIFESTS_DIR_NAME = "manifests"
README_FILE_NAME = "README.txt"

ZLIB_COMPRESS_LEVEL = 1
STREAM_CHUNK_SIZE = 1024 * 1024
DEFAULT_SAVE_WORKERS = min(8, max(1, os.cpu_count() or 4))
PROGRESS_INTERVAL_SECONDS = 2.0

# 既に圧縮済みのファイル形式。zlib で再圧縮しても 97〜99% しか縮まず CPU 時間だけ
# 大量に消費するため、無圧縮 (.raw / ZIP_STORED) で保存する。
INCOMPRESSIBLE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif",
    ".mp4", ".mov", ".webm", ".mkv", ".avi",
    ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".flac",
    ".zip", ".gz", ".bz2", ".xz", ".7z", ".zst", ".br",
    ".vrm", ".glb", ".woff2",
})

_STORE_README_TEXT = """SAIVerse World Snapshots
========================

このフォルダには SAIVerse の世界スナップショットが保存されています。

- 新しいスナップショットの作成・復元・削除は、SAIVerse の
  「グローバル設定 → バックアップ」タブ、または snapshot.bat / snapshot.sh から
  行ってください。
- `.store/` フォルダの中身は複数世代で共有される実体データです。
  直接削除・変更すると、参照しているスナップショットが復元できなくなります。
- `manifests/` の JSON ファイルを誤って手動削除しても他の世代は壊れませんが、
  不要データの回収には画面の「不要データを掃除」または `snapshot.bat gc` を
  実行してください。
- 従来の `.zip` ファイル (format_version 2) はそのまま復元・削除できます。
"""

_DATABASE_REL_PARTS = ("user_data", "database")
_LIVE_MAIN_DB_FILES = frozenset({
    "saiverse.db",
    "saiverse.db-wal",
    "saiverse.db-shm",
    "saiverse.db-journal",
})


def _is_excluded_db_backup(rel: Path) -> bool:
    """``user_data/database/`` 直下の ``saiverse.db`` バックアップ副産物かどうか。

    ``database/backup.py`` / ``database/migrate.py`` が過去のバージョンで
    ``user_data/database/`` へ直接書き出していた ``*.bak`` / ``*.bak.json`` /
    ``*.bak-wal`` / ``*.bak-shm`` をスナップショットへ二重に吸い込まないための境界。
    """
    parts = rel.parts
    if len(parts) != 3 or parts[:2] != _DATABASE_REL_PARTS:
        return False
    name = parts[2]
    if not name.startswith("saiverse.db") or name in _LIVE_MAIN_DB_FILES:
        return False
    return ".bak" in name[len("saiverse.db"):]


def _matches_any(rel: Path, parts_set: frozenset[Tuple[str, ...]]) -> bool:
    """``rel`` の先頭からの部分列が ``parts_set`` のいずれかに一致するか。

    一致したディレクトリの配下は、深さに関係なくすべて該当する。
    """
    parts = rel.parts
    return any(parts[:i] in parts_set for i in range(1, len(parts) + 1))


def is_excluded(rel: Path) -> bool:
    """home からの相対パス ``rel`` がスナップショット除外対象に該当するか。"""
    return _matches_any(rel, _EXCLUDED_PARTS) or _is_excluded_db_backup(rel)


def is_protected_from_archive(rel: Path) -> bool:
    """アーカイブメンバー ``rel`` が ``PROTECTED_FROM_ARCHIVE`` に該当するか。"""
    return _matches_any(rel, _PROTECTED_PARTS)


def _contains_excluded(rel_parts: Tuple[str, ...]) -> bool:
    """``rel_parts`` の *配下* に除外対象ディレクトリがあるか（``rel_parts`` 自身は数えない）。

    「まるごと入れ替えてよいディレクトリか、子まで降りて選り分ける必要があるか」
    の判定に使う。見るのは ``EXCLUDED_FROM_SNAPSHOT`` 側だけ。
    """
    depth = len(rel_parts)
    return any(
        len(excluded) > depth and excluded[:depth] == rel_parts
        for excluded in _EXCLUDED_PARTS
    )


def _dir_has_excluded_db_backups(directory: Path) -> bool:
    """``user_data/database/`` に未移行の DB バックアップファイルが残っているか。"""
    if directory.is_symlink() or not directory.is_dir():
        return False
    try:
        for child in directory.iterdir():
            if _is_excluded_db_backup(Path(*_DATABASE_REL_PARTS, child.name)):
                return True
    except OSError:
        return False
    return False


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


def store_dir() -> Path:
    """CAS 内部ストアディレクトリ (.store/)。"""
    return snapshots_dir() / STORE_DIR_NAME


def objects_dir() -> Path:
    """CAS オブジェクト保存ディレクトリ (.store/objects/)。"""
    return store_dir() / OBJECTS_DIR_NAME


def manifests_dir() -> Path:
    """CAS マニフェスト保存ディレクトリ (manifests/)。"""
    return snapshots_dir() / MANIFESTS_DIR_NAME


def _ensure_store_readme() -> None:
    readme = snapshots_dir() / README_FILE_NAME
    if not readme.exists():
        try:
            readme.write_text(_STORE_README_TEXT, encoding="utf-8")
        except OSError:
            LOGGER.debug("Could not write snapshot README.txt", exc_info=True)


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

def is_saiverse_running() -> Tuple[bool, str]:
    """SAIVerse が起動中かを runtime_marker と city.API_PORT への TCP 接続試行で判定。

    Returns:
        (True, reason) なら起動中、(False, reason_or_empty) なら停止中
    """
    from saiverse.runtime_marker import marker_status

    marker_state, marker_reason = marker_status()
    if marker_state == "running":
        return True, marker_reason
    if marker_state == "unknown":
        return True, marker_reason

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
    """saiverse.db を読み取り専用で開き、city.API_PORT 列を取得する。"""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError as exc:
        LOGGER.warning("Failed to open saiverse.db read-only: %s", exc, exc_info=True)
        return []

    try:
        try:
            cur = conn.execute("SELECT API_PORT FROM city WHERE API_PORT IS NOT NULL")
        except sqlite3.OperationalError as exc:
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
    archive_path: str  # POSIX 形式の相対パス


def collect_files_to_snapshot() -> List[SnapshotEntry]:
    """スナップショットに含めるファイルを枝刈り走査で列挙する。

    ``os.walk`` で除外ディレクトリに降りないことで、``addon_install/`` (10 万超
    ファイル) や ``backups/`` などの無駄な走査を避ける。
    """
    home = saiverse_home()
    if not home.exists():
        return []

    entries: List[SnapshotEntry] = []

    for root, dirs, files in os.walk(home, followlinks=False):
        root_path = Path(root)
        try:
            rel_root = root_path.relative_to(home)
        except ValueError:
            dirs[:] = []
            continue

        # 除外対象ディレクトリとシンボリックリンクディレクトリを枝刈り
        kept_dirs: List[str] = []
        for d in dirs:
            rel_dir = (rel_root / d) if rel_root.parts else Path(d)
            if is_excluded(rel_dir):
                continue
            if (root_path / d).is_symlink():
                continue
            kept_dirs.append(d)
        kept_dirs.sort()
        dirs[:] = kept_dirs

        for fname in sorted(files):
            rel = (rel_root / fname) if rel_root.parts else Path(fname)
            if not rel.parts or is_excluded(rel):
                continue
            abs_path = root_path / fname
            if not abs_path.is_file():
                continue
            entries.append(SnapshotEntry(
                abs_path=abs_path,
                archive_path=rel.as_posix(),
            ))

    return entries


# ---- メタ情報・ハッシュ ----

def build_metadata(
    name: str,
    note: str,
    file_count: int,
    total_bytes: int,
    files: dict[str, dict[str, object]],
    *,
    format_version: int = CAS_SNAPSHOT_FORMAT_VERSION,
) -> dict:
    """マニフェスト / snapshot.json に書き込むメタ情報を組み立てる。"""
    return {
        "format_version": format_version,
        "name": name,
        "created_at": datetime.now().astimezone().isoformat(),
        "saiverse_version": get_saiverse_version(),
        "note": note,
        "file_count": file_count,
        "total_bytes_uncompressed": total_bytes,
        "files": files,
    }


def _sha256_and_size(
    path: Path,
    *,
    on_bytes: Optional[Callable[[int], None]] = None,
) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(STREAM_CHUNK_SIZE), b""):
            digest.update(chunk)
            chunk_len = len(chunk)
            size += chunk_len
            if on_bytes is not None:
                on_bytes(chunk_len)
    return digest.hexdigest(), size


def _sha256_file(path: Path) -> str:
    digest, _ = _sha256_and_size(path)
    return digest


def read_snapshot_metadata(snap_path: Path) -> Optional[dict]:
    """ZIP または CAS マニフェスト JSON からメタ情報を読み込む。失敗時 None。"""
    if snap_path.suffix.lower() == ".json":
        try:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as exc:
            LOGGER.warning("Failed to read manifest metadata from %s: %s", snap_path, exc, exc_info=True)
            return None
    try:
        with zipfile.ZipFile(snap_path, "r") as zf:
            data = json.loads(zf.read("snapshot.json").decode("utf-8"))
            return data if isinstance(data, dict) else None
    except KeyError:
        LOGGER.warning("snapshot.json not found in %s", snap_path)
        return None
    except Exception as exc:
        LOGGER.warning("Failed to read metadata from %s: %s", snap_path, exc, exc_info=True)
        return None


# ---- CAS オブジェクト倉庫 ----

def _is_valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _object_paths(sha256: str) -> Tuple[Path, Path]:
    prefix_dir = objects_dir() / sha256[:2]
    return prefix_dir / f"{sha256}.raw", prefix_dir / f"{sha256}.z"


def _find_existing_object(sha256: str, expected_size: int) -> Optional[Tuple[Path, str]]:
    raw_path, z_path = _object_paths(sha256)
    if raw_path.is_file():
        try:
            if raw_path.stat().st_size == expected_size:
                return raw_path, "raw"
        except OSError:
            pass
    if z_path.is_file():
        try:
            if z_path.stat().st_size > 0:
                return z_path, "zlib"
        except OSError:
            pass
    return None


def _preferred_encoding(path: Path) -> str:
    if path.suffix.lower() in INCOMPRESSIBLE_EXTENSIONS:
        return "raw"
    return "zlib"


def _write_object_atomically(
    src_path: Path,
    sha256: str,
    expected_size: int,
    encoding: str,
    *,
    on_bytes: Optional[Callable[[int], None]] = None,
) -> Path:
    raw_path, z_path = _object_paths(sha256)
    target = raw_path if encoding == "raw" else z_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f"{target.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    digest = hashlib.sha256()
    actual_size = 0
    try:
        with src_path.open("rb") as src, tmp_path.open("wb") as dst:
            if encoding == "raw":
                for chunk in iter(lambda: src.read(STREAM_CHUNK_SIZE), b""):
                    digest.update(chunk)
                    chunk_len = len(chunk)
                    actual_size += chunk_len
                    dst.write(chunk)
                    if on_bytes is not None:
                        on_bytes(chunk_len)
            else:
                compressor = zlib.compressobj(level=ZLIB_COMPRESS_LEVEL)
                for chunk in iter(lambda: src.read(STREAM_CHUNK_SIZE), b""):
                    digest.update(chunk)
                    chunk_len = len(chunk)
                    actual_size += chunk_len
                    compressed = compressor.compress(chunk)
                    if compressed:
                        dst.write(compressed)
                    if on_bytes is not None:
                        on_bytes(chunk_len)
                tail = compressor.flush()
                if tail:
                    dst.write(tail)
        if actual_size != expected_size or digest.hexdigest() != sha256:
            raise RuntimeError(f"File changed while snapshotting: {src_path}")
        os.replace(tmp_path, target)
        return target
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _process_entry_for_cas(
    entry: SnapshotEntry,
    *,
    hash_locks: Dict[str, threading.Lock],
    locks_guard: threading.Lock,
    on_bytes: Optional[Callable[[int], None]] = None,
) -> Tuple[str, str, int, str, bool]:
    """1 ファイルをハッシュ計算し、未保存なら CAS 倉庫へ書き込む。

    Returns:
        (archive_path, sha256, size, encoding, was_new)
    """
    sha256, size = _sha256_and_size(entry.abs_path, on_bytes=on_bytes)
    existing = _find_existing_object(sha256, size)
    if existing is not None:
        _, encoding = existing
        return entry.archive_path, sha256, size, encoding, False

    with locks_guard:
        lock = hash_locks.setdefault(sha256, threading.Lock())

    with lock:
        existing = _find_existing_object(sha256, size)
        if existing is not None:
            _, encoding = existing
            return entry.archive_path, sha256, size, encoding, False
        encoding = _preferred_encoding(entry.abs_path)
        _write_object_atomically(entry.abs_path, sha256, size, encoding, on_bytes=on_bytes)
        return entry.archive_path, sha256, size, encoding, True


def resolve_snapshot(name: str) -> Tuple[str, Path]:
    """名前からスナップショット実体を解決する。

    Returns:
        ("cas", manifest_path) または ("zip", zip_path)
    """
    if not _is_valid_snapshot_name(name):
        raise ValueError(f"Invalid snapshot name: {name!r}")
    manifest = manifests_dir() / f"{name}.json"
    archive = snapshots_dir() / f"{name}.zip"
    has_manifest = manifest.is_file()
    has_archive = archive.is_file()
    if has_manifest and has_archive:
        raise ValueError(
            f"Ambiguous snapshot '{name}': both CAS manifest and ZIP archive exist"
        )
    if has_manifest:
        return "cas", manifest
    if has_archive:
        return "zip", archive
    raise FileNotFoundError(f"Snapshot '{name}' not found")


_MANIFEST_TEMP_RE = re.compile(r"\.json\.tmp\.(\d+)$")
_OBJECT_TEMP_RE = re.compile(r"\.(?:raw|z)\.tmp\.(\d+)(?:\.\d+)?$")


def _is_manifest_temp_file(filename: str) -> bool:
    """``<name>.json.tmp.<pid>`` 形式の一時マニフェストかどうか。

    確定済みマニフェストは必ず ``.json`` で終わるため、名前に ``.json.tmp`` を
    含む正当なスナップショット (例: ``world.json.tmp_old.json``) とは衝突しない。
    """
    return _MANIFEST_TEMP_RE.search(filename) is not None


def _is_object_temp_file(filename: str) -> bool:
    """``<sha256>.(raw|z).tmp.<pid>.<tid>`` 形式の一時オブジェクトかどうか。"""
    return _OBJECT_TEMP_RE.search(filename) is not None


def _temp_file_pid(filename: str) -> Optional[int]:
    match = _MANIFEST_TEMP_RE.search(filename) or _OBJECT_TEMP_RE.search(filename)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def list_snapshots() -> List[Dict[str, Any]]:
    """全スナップショット (CAS + 旧 ZIP) の一覧を返す (新しい順)。"""
    items: List[Dict[str, Any]] = []
    mdir = manifests_dir()
    if mdir.is_dir():
        for path in sorted(mdir.glob("*.json")):
            if not path.is_file():
                continue
            meta = read_snapshot_metadata(path)
            items.append({
                "name": path.stem,
                "format": "cas",
                "path": str(path),
                "created_at": meta.get("created_at") if meta else None,
                "saiverse_version": meta.get("saiverse_version") if meta else None,
                "note": (meta.get("note") or "") if meta else "",
                "file_count": meta.get("file_count") if meta else None,
                "total_bytes_uncompressed": meta.get("total_bytes_uncompressed") if meta else None,
                "archive_size_bytes": None,
                "readable": meta is not None,
            })

    sdir = snapshots_dir()
    for path in sorted(sdir.glob("*.zip")):
        if not path.is_file():
            continue
        meta = read_snapshot_metadata(path)
        try:
            archive_size = path.stat().st_size
        except OSError:
            archive_size = None
        items.append({
            "name": path.stem,
            "format": "zip",
            "path": str(path),
            "created_at": meta.get("created_at") if meta else None,
            "saiverse_version": meta.get("saiverse_version") if meta else None,
            "note": (meta.get("note") or "") if meta else "",
            "file_count": meta.get("file_count") if meta else None,
            "total_bytes_uncompressed": meta.get("total_bytes_uncompressed") if meta else None,
            "archive_size_bytes": archive_size,
            "readable": meta is not None,
        })

    items.sort(key=lambda x: (x.get("created_at") or "", x["name"]), reverse=True)
    return items


def _collect_live_object_hashes() -> Tuple[set[str], List[str]]:
    """全 CAS マニフェストから参照されている SHA-256 集合と、読めないマニフェスト一覧を返す。"""
    referenced: set[str] = set()
    corrupted: List[str] = []
    mdir = manifests_dir()
    if not mdir.is_dir():
        return referenced, corrupted

    for path in sorted(mdir.glob("*.json")):
        if not path.is_file():
            continue
        meta = read_snapshot_metadata(path)
        if not meta or meta.get("format_version") != CAS_SNAPSHOT_FORMAT_VERSION:
            corrupted.append(path.name)
            continue
        files = meta.get("files")
        if not isinstance(files, dict):
            corrupted.append(path.name)
            continue
        valid_manifest = True
        manifest_hashes: set[str] = set()
        for entry in files.values():
            if not isinstance(entry, dict) or not _is_valid_sha256(entry.get("sha256")):
                valid_manifest = False
                break
            manifest_hashes.add(entry["sha256"])
        if not valid_manifest:
            corrupted.append(path.name)
            continue
        referenced.update(manifest_hashes)
    return referenced, corrupted


def _parse_object_filename(filename: str) -> Optional[str]:
    """``<sha256>.raw`` / ``<sha256>.z`` から sha256 を取り出す。該当外は None。"""
    if filename.endswith(".raw"):
        stem = filename[:-4]
    elif filename.endswith(".z"):
        stem = filename[:-2]
    else:
        return None
    return stem if _is_valid_sha256(stem) else None


def _is_temp_file_stale(filename: str) -> bool:
    """一時ファイルの持ち主プロセスが既に終了しているか。"""
    pid = _temp_file_pid(filename)
    if pid is None:
        return False
    if pid == os.getpid():
        return False
    try:
        from saiverse.runtime_marker import probe_process_alive

        return probe_process_alive(pid) == "dead"
    except Exception:
        return False


def get_store_stats() -> Dict[str, Any]:
    """スナップショット倉庫全体の容量・孤立オブジェクト統計を返す。"""
    referenced, corrupted = _collect_live_object_hashes()
    cas_store_bytes = 0
    cas_object_count = 0
    orphan_object_count = 0
    orphan_bytes = 0
    temp_file_count = 0
    temp_bytes = 0

    odir = objects_dir()
    if odir.is_dir():
        for root, _dirs, files in os.walk(odir):
            root_path = Path(root)
            for fname in files:
                fpath = root_path / fname
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                if _is_object_temp_file(fname):
                    temp_file_count += 1
                    temp_bytes += size
                    continue
                sha = _parse_object_filename(fname)
                cas_store_bytes += size
                cas_object_count += 1
                if sha is not None and not corrupted and sha not in referenced:
                    orphan_object_count += 1
                    orphan_bytes += size

    legacy_zip_bytes = 0
    legacy_zip_count = 0
    for zpath in snapshots_dir().glob("*.zip"):
        if zpath.is_file():
            legacy_zip_count += 1
            try:
                legacy_zip_bytes += zpath.stat().st_size
            except OSError:
                pass

    return {
        "cas_store_bytes": cas_store_bytes,
        "cas_object_count": cas_object_count,
        "legacy_zip_bytes": legacy_zip_bytes,
        "legacy_zip_count": legacy_zip_count,
        "total_disk_bytes": cas_store_bytes + legacy_zip_bytes + temp_bytes,
        "orphan_object_count": orphan_object_count,
        "orphan_bytes": orphan_bytes,
        "temp_file_count": temp_file_count,
        "temp_bytes": temp_bytes,
        "corrupted_manifests": corrupted,
    }


STORE_LOCK_FILE_NAME = ".store.lock"
DEFAULT_STORE_LOCK_TIMEOUT_SECONDS = 5.0

_STORE_LOCK_THREAD_LOCK = threading.RLock()
_STORE_LOCK_STATE = threading.local()


class SnapshotStoreBusyError(RuntimeError):
    """Raised when another process or thread holds the snapshot store lock."""


@contextlib.contextmanager
def snapshot_store_lock(timeout: float = DEFAULT_STORE_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Cross-process + re-entrant thread lock protecting the snapshot store.

    Serializes ``save_snapshot`` (from before object writes through manifest
    publish), ``delete_snapshot``, ``gc_store``, ``export_snapshot_to_zip``,
    and ``cmd_restore`` so GC/delete in another process can never remove
    in-flight or referenced CAS objects.
    """
    sdir = store_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    lock_path = (sdir / STORE_LOCK_FILE_NAME).resolve()
    lock_key = str(lock_path)

    depths: dict[str, int] = getattr(_STORE_LOCK_STATE, "depths", None)
    if depths is None:
        depths = {}
        _STORE_LOCK_STATE.depths = depths

    if depths.get(lock_key, 0) > 0:
        depths[lock_key] += 1
        try:
            yield
        finally:
            depths[lock_key] -= 1
        return

    deadline = time.monotonic() + max(0.0, timeout)
    acquired_thread = _STORE_LOCK_THREAD_LOCK.acquire(timeout=max(0.0, timeout))
    if not acquired_thread:
        raise SnapshotStoreBusyError(
            "Another snapshot operation is currently in progress; try again after it finishes."
        )

    fd: Optional[int] = None
    locked_os = False
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        if sys.platform == "win32":
            if os.fstat(fd).st_size == 0:
                os.write(fd, b" ")
        while True:
            try:
                if sys.platform == "win32":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked_os = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SnapshotStoreBusyError(
                        "Another snapshot operation is currently in progress; try again after it finishes."
                    )
                time.sleep(0.05)

        depths[lock_key] = 1
        try:
            yield
        finally:
            depths[lock_key] = 0
    finally:
        if fd is not None:
            if locked_os:
                try:
                    if sys.platform == "win32":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
        _STORE_LOCK_THREAD_LOCK.release()


def gc_store(*, dry_run: bool = False, lock_timeout: float = DEFAULT_STORE_LOCK_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """参照されていない CAS オブジェクトと死んだプロセスの一時ファイルを回収する。

    Fail-closed: 1 枚でも壊れて読めないマニフェストがある場合は、生きている世代の
    オブジェクトを誤削除しないよう一切削除せず RuntimeError を送出する。
    """
    with snapshot_store_lock(timeout=lock_timeout):
        return _gc_store_locked(dry_run=dry_run)


def _gc_store_locked(*, dry_run: bool = False) -> Dict[str, Any]:
    referenced, corrupted = _collect_live_object_hashes()
    if corrupted:
        raise RuntimeError(
            "GC aborted (fail-closed): unreadable or invalid manifest(s) detected: "
            + ", ".join(corrupted)
        )

    removed_objects = 0
    reclaimed_bytes = 0
    removed_temp_files = 0
    live_objects = 0
    live_bytes = 0

    odir = objects_dir()
    if odir.is_dir():
        for root, _dirs, files in os.walk(odir, topdown=False):
            root_path = Path(root)
            for fname in files:
                fpath = root_path / fname
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                if _is_object_temp_file(fname):
                    if _is_temp_file_stale(fname):
                        if not dry_run:
                            try:
                                fpath.unlink(missing_ok=True)
                                removed_temp_files += 1
                                reclaimed_bytes += size
                            except OSError as exc:
                                LOGGER.warning("Could not remove stale temp %s: %s", fpath, exc)
                        else:
                            removed_temp_files += 1
                            reclaimed_bytes += size
                    continue
                sha = _parse_object_filename(fname)
                if sha is None:
                    continue
                if sha in referenced:
                    live_objects += 1
                    live_bytes += size
                    continue
                if not dry_run:
                    try:
                        fpath.unlink(missing_ok=True)
                        removed_objects += 1
                        reclaimed_bytes += size
                    except OSError as exc:
                        LOGGER.warning("Could not remove unreferenced object %s: %s", fpath, exc)
                else:
                    removed_objects += 1
                    reclaimed_bytes += size
            # 空になった 2 文字プレフィックスディレクトリを掃除
            if not dry_run and root_path != odir:
                try:
                    if not any(root_path.iterdir()):
                        root_path.rmdir()
                except OSError:
                    pass

    # manifests/ 内の死んだプロセスの一時マニフェストも掃除
    mdir = manifests_dir()
    if mdir.is_dir():
        for path in list(mdir.iterdir()):
            if path.is_file() and _is_manifest_temp_file(path.name) and _is_temp_file_stale(path.name):
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                if not dry_run:
                    try:
                        path.unlink(missing_ok=True)
                        removed_temp_files += 1
                        reclaimed_bytes += size
                    except OSError:
                        pass
                else:
                    removed_temp_files += 1
                    reclaimed_bytes += size

    return {
        "removed_objects": removed_objects,
        "reclaimed_bytes": reclaimed_bytes,
        "removed_temp_files": removed_temp_files,
        "live_objects": live_objects,
        "live_bytes": live_bytes,
    }


def cleanup_temp_files_for_pid(pid: int) -> int:
    """強制終了された子プロセス (``pid``) が残した一時ファイルを掃除する。"""
    removed = 0
    mdir = manifests_dir()
    if mdir.is_dir():
        for path in list(mdir.iterdir()):
            if path.is_file() and _is_manifest_temp_file(path.name) and _temp_file_pid(path.name) == pid:
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    LOGGER.warning("Could not remove leftover temp %s: %s", path, exc)

    odir = objects_dir()
    if odir.is_dir():
        for root, _dirs, files in os.walk(odir):
            root_path = Path(root)
            for fname in files:
                if _is_object_temp_file(fname) and _temp_file_pid(fname) == pid:
                    try:
                        (root_path / fname).unlink(missing_ok=True)
                        removed += 1
                    except OSError as exc:
                        LOGGER.warning("Could not remove leftover temp %s: %s", root_path / fname, exc)
    return removed


def delete_snapshot(
    name: str,
    *,
    run_gc: bool = True,
    lock_timeout: float = DEFAULT_STORE_LOCK_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """指定スナップショットを削除する。CAS の場合は続けて fail-closed GC を実行する。"""
    with snapshot_store_lock(timeout=lock_timeout):
        fmt, path = resolve_snapshot(name)
        size_removed = 0
        if fmt == "zip":
            try:
                size_removed = path.stat().st_size
            except OSError:
                size_removed = 0
            path.unlink()
            return {
                "name": name,
                "format": "zip",
                "reclaimed_bytes": size_removed,
                "gc": None,
                "gc_error": None,
            }

        path.unlink()
        gc_result: Optional[Dict[str, Any]] = None
        gc_error: Optional[str] = None
        if run_gc:
            try:
                gc_result = _gc_store_locked()
                size_removed = int(gc_result.get("reclaimed_bytes", 0))
            except Exception as exc:
                gc_error = str(exc)
                LOGGER.warning("Post-delete GC did not run: %s", exc)
        return {
            "name": name,
            "format": "cas",
            "reclaimed_bytes": size_removed,
            "gc": gc_result,
            "gc_error": gc_error,
        }


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
    """復元前に ~/.saiverse/ の中身を削除（除外パスは残す）。"""
    for rel in sorted(_managed_swap_roots(home), key=str):
        LOGGER.debug("Removing %s", home / rel)
        remove_path(home / rel)


def _safe_archive_member(name: str) -> Path:
    """アーカイブ/マニフェストのメンバー名を検証して相対パスへ落とす。

    拒否の基準は ``PROTECTED_FROM_ARCHIVE``（``EXCLUDED_FROM_SNAPSHOT`` ではない）。
    収集の除外に入っただけのもの（llama_cache や旧アーカイブ内の DB バックアップ）は、
    それ以前に作られたアーカイブに正当なメンバーとして入っているので拒否しない。
    """
    if not name or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe snapshot member: {name!r}")
    rel = Path(name)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise ValueError(f"Unsafe snapshot member: {name!r}")
    if is_protected_from_archive(rel):
        raise ValueError(f"Snapshot member targets a protected path: {name!r}")
    return rel


def _validate_sqlite_files(
    stage: Path,
    *,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> None:
    main_db = stage / "user_data" / "database" / "saiverse.db"
    if not main_db.is_file():
        raise ValueError("Snapshot is missing user_data/database/saiverse.db")
    db_paths = sorted(stage.rglob("*.db"))
    total = len(db_paths)
    for idx, db_path in enumerate(db_paths, start=1):
        rel_str = db_path.relative_to(stage).as_posix()
        _emit_progress(
            f"[snapshot] Verifying SQLite integrity ({idx}/{total}): {rel_str}",
            progress_callback,
        )
        last_report = [time.monotonic()]

        def _sqlite_progress() -> int:
            now = time.monotonic()
            if now - last_report[0] >= PROGRESS_INTERVAL_SECONDS:
                last_report[0] = now
                _emit_progress(
                    f"[snapshot] Verifying SQLite integrity ({idx}/{total}): {rel_str} ...",
                    progress_callback,
                )
            return 0

        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                connection.set_progress_handler(_sqlite_progress, 100_000)
                result = connection.execute("PRAGMA integrity_check").fetchone()
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()
        except sqlite3.Error as exc:
            raise ValueError(f"SQLite validation failed for {db_path.relative_to(stage)}") from exc
        if not result or result[0] != "ok":
            raise ValueError(f"SQLite integrity_check failed for {db_path.relative_to(stage)}")


def validate_and_extract_snapshot(
    snap: Path,
    stage: Path,
    *,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Validate a format v2 ZIP archive and extract it into an isolated staging tree.

    CRC-32 is verified by Python's ``zipfile`` stream reader as every member is
    extracted to EOF alongside SHA-256 verification, avoiding a separate silent
    ``testzip()`` full-archive pass while emitting periodic progress.
    """
    try:
        with zipfile.ZipFile(snap, "r") as zf:
            infos = zf.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or names.count("snapshot.json") != 1:
                raise ValueError("Snapshot contains duplicate members or invalid metadata count")
            metadata = json.loads(zf.read("snapshot.json").decode("utf-8"))
            if metadata.get("format_version") != SNAPSHOT_FORMAT_VERSION:
                raise ValueError(
                    f"Unsupported snapshot format {metadata.get('format_version')!r}; "
                    f"expected {SNAPSHOT_FORMAT_VERSION}"
                )
            manifest = metadata.get("files")
            if not isinstance(manifest, dict):
                raise ValueError("Snapshot file manifest is missing")
            payload_names = {
                name for name in names if name != "snapshot.json" and not name.endswith("/")
            }
            if payload_names != set(manifest) or metadata.get("file_count") != len(payload_names):
                raise ValueError("Snapshot manifest does not match archive members")

            total_files = len(payload_names)
            total_bytes = sum(
                info.file_size
                for info in infos
                if info.filename != "snapshot.json" and not info.is_dir()
            )
            total_mb = total_bytes / (1024 * 1024)
            done_files = 0
            done_bytes = 0
            last_report = time.monotonic()

            for info in infos:
                if info.filename == "snapshot.json" or info.is_dir():
                    continue
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError(f"Snapshot contains a symbolic link: {info.filename}")
                rel = _safe_archive_member(info.filename)
                target = (stage / rel).resolve()
                if not target.is_relative_to(stage.resolve()):
                    raise ValueError(f"Snapshot member escapes staging: {info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with zf.open(info, "r") as source, target.open("xb") as destination:
                    for chunk in iter(lambda: source.read(STREAM_CHUNK_SIZE), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        done_bytes += len(chunk)
                        destination.write(chunk)
                        now = time.monotonic()
                        if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                            last_report = now
                            _emit_progress(
                                f"[snapshot] Extracting ZIP: {done_files}/{total_files} files "
                                f"({done_bytes / (1024 * 1024):.1f} / {total_mb:.1f} MB)",
                                progress_callback,
                            )
                expected = manifest[info.filename]
                if not isinstance(expected, dict):
                    raise ValueError(f"Invalid manifest entry for {info.filename}")
                if expected.get("sha256") != digest.hexdigest() or expected.get("size") != size:
                    raise ValueError(f"Hash or size mismatch for {info.filename}")
                done_files += 1
    except (zipfile.BadZipFile, zlib.error) as exc:
        raise ValueError(f"Snapshot CRC validation failed: {exc}") from exc

    _emit_progress(
        f"[snapshot] Extracted {done_files}/{total_files} files ({done_bytes / (1024 * 1024):.1f} MB)",
        progress_callback,
    )
    _validate_sqlite_files(stage, progress_callback=progress_callback)
    return metadata


def _resolve_object_for_restore(sha256: str, encoding: object) -> Tuple[Path, str]:
    raw_path, z_path = _object_paths(sha256)
    if encoding == "raw" and raw_path.is_file():
        return raw_path, "raw"
    if encoding == "zlib" and z_path.is_file():
        return z_path, "zlib"
    if raw_path.is_file():
        return raw_path, "raw"
    if z_path.is_file():
        return z_path, "zlib"
    raise ValueError(f"Missing CAS object for sha256 {sha256}")


def validate_and_extract_cas_snapshot(
    manifest_path: Path,
    stage: Path,
    *,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Validate a format v3 CAS manifest + objects and extract into an isolated staging tree."""
    metadata = read_snapshot_metadata(manifest_path)
    if metadata is None:
        raise ValueError(f"Snapshot manifest is unreadable: {manifest_path}")
    if metadata.get("format_version") != CAS_SNAPSHOT_FORMAT_VERSION:
        raise ValueError(
            f"Unsupported CAS snapshot format {metadata.get('format_version')!r}; "
            f"expected {CAS_SNAPSHOT_FORMAT_VERSION}"
        )
    manifest = metadata.get("files")
    if not isinstance(manifest, dict) or metadata.get("file_count") != len(manifest):
        raise ValueError("Snapshot manifest file table is missing or count mismatched")

    stage_resolved = stage.resolve()
    total_files = len(manifest)
    total_bytes = sum(
        int(entry.get("size", 0))
        for entry in manifest.values()
        if isinstance(entry, dict) and isinstance(entry.get("size"), int)
    )
    total_mb = total_bytes / (1024 * 1024)
    done_files = 0
    done_bytes = 0
    last_report = time.monotonic()

    for archive_path, expected in manifest.items():
        if not isinstance(expected, dict):
            raise ValueError(f"Invalid manifest entry for {archive_path}")
        expected_sha = expected.get("sha256")
        expected_size = expected.get("size")
        if not _is_valid_sha256(expected_sha) or not isinstance(expected_size, int) or expected_size < 0:
            raise ValueError(f"Invalid hash/size metadata for {archive_path}")

        rel = _safe_archive_member(archive_path)
        target = (stage / rel).resolve()
        if not target.is_relative_to(stage_resolved):
            raise ValueError(f"Snapshot member escapes staging: {archive_path}")
        target.parent.mkdir(parents=True, exist_ok=True)

        obj_path, encoding = _resolve_object_for_restore(expected_sha, expected.get("encoding"))
        digest = hashlib.sha256()
        size = 0
        with obj_path.open("rb") as source, target.open("xb") as destination:
            if encoding == "raw":
                for chunk in iter(lambda: source.read(STREAM_CHUNK_SIZE), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    done_bytes += len(chunk)
                    destination.write(chunk)
                    now = time.monotonic()
                    if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                        last_report = now
                        _emit_progress(
                            f"[snapshot] Staging CAS: {done_files}/{total_files} files "
                            f"({done_bytes / (1024 * 1024):.1f} / {total_mb:.1f} MB)",
                            progress_callback,
                        )
            else:
                decompressor = zlib.decompressobj()
                try:
                    for chunk in iter(lambda: source.read(STREAM_CHUNK_SIZE), b""):
                        plain = decompressor.decompress(chunk)
                        if plain:
                            digest.update(plain)
                            size += len(plain)
                            done_bytes += len(plain)
                            destination.write(plain)
                        now = time.monotonic()
                        if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                            last_report = now
                            _emit_progress(
                                f"[snapshot] Staging CAS: {done_files}/{total_files} files "
                                f"({done_bytes / (1024 * 1024):.1f} / {total_mb:.1f} MB)",
                                progress_callback,
                            )
                    tail = decompressor.flush()
                    if tail:
                        digest.update(tail)
                        size += len(tail)
                        done_bytes += len(tail)
                        destination.write(tail)
                    if not decompressor.eof or decompressor.unused_data:
                        raise ValueError(f"Corrupted zlib stream for {archive_path}")
                except zlib.error as exc:
                    raise ValueError(f"Decompression failed for {archive_path}: {exc}") from exc

        if digest.hexdigest() != expected_sha or size != expected_size:
            raise ValueError(f"Hash or size mismatch for {archive_path}")
        done_files += 1

    _emit_progress(
        f"[snapshot] Staged {done_files}/{total_files} files ({done_bytes / (1024 * 1024):.1f} MB)",
        progress_callback,
    )
    _validate_sqlite_files(stage, progress_callback=progress_callback)
    return metadata


def export_snapshot_to_zip(
    name: str,
    output_zip: Path,
    *,
    lock_timeout: float = DEFAULT_STORE_LOCK_TIMEOUT_SECONDS,
) -> Path:
    """スナップショットを単一の format_version 2 互換 ZIP ファイルへ書き出す。"""
    output_zip = output_zip.resolve()
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = output_zip.with_name(f"{output_zip.name}.tmp.{os.getpid()}")

    try:
        with snapshot_store_lock(timeout=lock_timeout):
            fmt, path = resolve_snapshot(name)
            if fmt == "zip":
                shutil.copy2(path, tmp_out)
                os.replace(tmp_out, output_zip)
                return output_zip

            metadata = read_snapshot_metadata(path)
            if metadata is None or metadata.get("format_version") != CAS_SNAPSHOT_FORMAT_VERSION:
                raise ValueError(f"Snapshot '{name}' manifest is invalid")
            files = metadata.get("files")
            if not isinstance(files, dict):
                raise ValueError(f"Snapshot '{name}' has no file manifest")

            v2_files: Dict[str, Dict[str, object]] = {}
            with zipfile.ZipFile(
                tmp_out,
                "w",
                zipfile.ZIP_DEFLATED,
                allowZip64=True,
                compresslevel=ZLIB_COMPRESS_LEVEL,
            ) as zf:
                for archive_path, entry in sorted(files.items()):
                    expected_sha = entry["sha256"]
                    expected_size = int(entry["size"])
                    obj_path, encoding = _resolve_object_for_restore(expected_sha, entry.get("encoding"))
                    compress_type = (
                        zipfile.ZIP_STORED
                        if Path(archive_path).suffix.lower() in INCOMPRESSIBLE_EXTENSIONS
                        else zipfile.ZIP_DEFLATED
                    )
                    info = zipfile.ZipInfo(archive_path)
                    info.compress_type = compress_type
                    info.file_size = expected_size
                    digest = hashlib.sha256()
                    size = 0
                    with obj_path.open("rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                        if encoding == "raw":
                            for chunk in iter(lambda: src.read(STREAM_CHUNK_SIZE), b""):
                                digest.update(chunk)
                                size += len(chunk)
                                dst.write(chunk)
                        else:
                            decompressor = zlib.decompressobj()
                            for chunk in iter(lambda: src.read(STREAM_CHUNK_SIZE), b""):
                                plain = decompressor.decompress(chunk)
                                if plain:
                                    digest.update(plain)
                                    size += len(plain)
                                    dst.write(plain)
                            tail = decompressor.flush()
                            if tail:
                                digest.update(tail)
                                size += len(tail)
                                dst.write(tail)
                    if digest.hexdigest() != expected_sha or size != expected_size:
                        raise ValueError(f"Integrity check failed while exporting {archive_path}")
                    v2_files[archive_path] = {"sha256": expected_sha, "size": expected_size}

                v2_meta = {
                    "format_version": SNAPSHOT_FORMAT_VERSION,
                    "name": metadata.get("name", name),
                    "created_at": metadata.get("created_at"),
                    "saiverse_version": metadata.get("saiverse_version", "unknown"),
                    "note": metadata.get("note", ""),
                    "file_count": len(v2_files),
                    "total_bytes_uncompressed": metadata.get("total_bytes_uncompressed", 0),
                    "files": v2_files,
                }
                zf.writestr("snapshot.json", json.dumps(v2_meta, ensure_ascii=False, indent=2))
            os.replace(tmp_out, output_zip)
            return output_zip
    except Exception:
        try:
            tmp_out.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _managed_swap_roots(
    root: Path,
    *,
    extra_descend: frozenset[Tuple[str, ...]] = frozenset(),
) -> set[Path]:
    """まるごと入れ替えてよい相対パスの集合を、除外パスを避けながら列挙する。

    除外パスを配下に持つディレクトリ（例: ``user_data`` は ``user_data/logs``
    を持つ）はそれ自体を単位にできないので、子まで降りて選り分ける。
    ``user_data/database`` は配下に未移行の DB バックアップファイルがある場合だけ
    子まで降りる（移行済みの通常環境ではまるごと 1 単位）。
    """
    paths: set[Path] = set()
    _collect_swap_roots(root, (), paths, extra_descend=extra_descend)
    return paths


def _collect_swap_roots(
    current: Path,
    prefix: Tuple[str, ...],
    out: set[Path],
    *,
    extra_descend: frozenset[Tuple[str, ...]] = frozenset(),
) -> None:
    if current.is_symlink() or not current.is_dir():
        return
    for child in current.iterdir():
        rel_parts = prefix + (child.name,)
        if is_excluded(Path(*rel_parts)):
            continue
        must_descend = (
            _contains_excluded(rel_parts)
            or rel_parts in extra_descend
            or (rel_parts == _DATABASE_REL_PARTS and _dir_has_excluded_db_backups(child))
        )
        if must_descend and child.is_dir() and not child.is_symlink():
            _collect_swap_roots(child, rel_parts, out, extra_descend=extra_descend)
        else:
            out.add(Path(*rel_parts))


def swap_staged_world(home: Path, stage: Path) -> None:
    """Replace managed world roots and restore every moved old root on failure."""
    rollback = home.parent / f".{home.name}.restore-rollback-{uuid.uuid4().hex}"
    rollback.mkdir(parents=True)

    # home と stage のどちらか一方でも user_data/database に除外対象 DB バックアップを
    # 持つなら、両方とも user_data/database の子単位で入れ替える（親と子が同時に
    # roots へ混在して os.rename が衝突するのを防ぐ）。
    extra_descend: set[Tuple[str, ...]] = set()
    if _dir_has_excluded_db_backups(home / "user_data" / "database") or _dir_has_excluded_db_backups(
        stage / "user_data" / "database"
    ):
        extra_descend.add(_DATABASE_REL_PARTS)
    frozen_extra = frozenset(extra_descend)

    roots = sorted(
        _managed_swap_roots(home, extra_descend=frozen_extra)
        | _managed_swap_roots(stage, extra_descend=frozen_extra),
        key=str,
    )
    moved_old: list[Path] = []
    moved_new: list[Path] = []
    try:
        for rel in roots:
            current = home / rel
            if current.exists() or current.is_symlink():
                destination = rollback / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.rename(current, destination)
                moved_old.append(rel)
        for rel in roots:
            incoming = stage / rel
            if incoming.exists() or incoming.is_symlink():
                destination = home / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.rename(incoming, destination)
                moved_new.append(rel)
    except Exception:
        LOGGER.exception("World swap failed; rolling back old world")
        for rel in reversed(moved_new):
            remove_path(home / rel)
        for rel in reversed(moved_old):
            source = rollback / rel
            destination = home / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, destination)
        raise
    finally:
        if rollback.exists():
            shutil.rmtree(rollback)


# ---- コマンド本体 ----

def _emit_progress(message: str, callback: Optional[Callable[[str], None]] = None) -> None:
    if callback is not None:
        callback(message)
    else:
        print(message, flush=True)


def save_snapshot(
    name: str,
    *,
    note: str = "",
    force: bool = False,
    workers: int = DEFAULT_SAVE_WORKERS,
    progress_callback: Optional[Callable[[str], None]] = None,
    lock_timeout: float = DEFAULT_STORE_LOCK_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """CAS (format_version 3) スナップショットを作成する本体関数。"""
    if not _is_valid_snapshot_name(name):
        raise ValueError(f"Invalid snapshot name '{name}' (use letters/digits/_-./).")

    with snapshot_store_lock(timeout=lock_timeout):
        return _save_snapshot_locked(
            name,
            note=note,
            force=force,
            workers=workers,
            progress_callback=progress_callback,
        )


def _save_snapshot_locked(
    name: str,
    *,
    note: str = "",
    force: bool = False,
    workers: int = DEFAULT_SAVE_WORKERS,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:

    mdir = manifests_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    objects_dir().mkdir(parents=True, exist_ok=True)
    _ensure_store_readme()

    manifest_path = mdir / f"{name}.json"
    legacy_zip = snapshots_dir() / f"{name}.zip"
    if (manifest_path.exists() or legacy_zip.exists()) and not force:
        raise FileExistsError(f"Snapshot '{name}' already exists. Use --force to overwrite.")

    running, reason = is_saiverse_running()
    if running:
        raise RuntimeError(
            f"SAIVerse appears to be running ({reason}). Stop SAIVerse before taking a world snapshot."
        )

    home = saiverse_home()
    if not home.exists():
        raise FileNotFoundError(f"SAIVerse home not found: {home}")

    # 前回の中断で残った同名の未確定ファイルだけを片付ける (確定済み *.json は触らない)
    leftover_manifest_re = re.compile(rf"^{re.escape(name)}\.json\.tmp\.\d+$")
    for leftover in list(mdir.iterdir()):
        if leftover.is_file() and leftover_manifest_re.match(leftover.name):
            try:
                leftover.unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.warning("Could not remove leftover %s: %s", leftover, exc)
    legacy_tmp = snapshots_dir() / f"{name}.zip.tmp"
    if legacy_tmp.exists():
        try:
            legacy_tmp.unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("Could not remove leftover %s: %s", legacy_tmp, exc)

    started_at = time.monotonic()
    _emit_progress(f"[snapshot] Collecting files from {home} ...", progress_callback)
    entries = collect_files_to_snapshot()
    total_bytes = sum(e.abs_path.stat().st_size for e in entries if e.abs_path.exists())
    total_mb = total_bytes / (1024 * 1024)
    _emit_progress(
        f"[snapshot] Found {len(entries)} files ({total_mb:.1f} MB uncompressed)",
        progress_callback,
    )

    file_manifest: Dict[str, Dict[str, object]] = {}
    failed: List[Tuple[Path, str]] = []
    hash_locks: Dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()

    progress_lock = threading.Lock()
    done_files = 0
    done_bytes = 0
    processed_io_bytes = 0
    new_objects = 0
    reused_objects = 0
    last_report = time.monotonic()
    worker_count = max(1, min(workers, len(entries) or 1))

    def _on_worker_bytes(chunk_bytes: int) -> None:
        nonlocal processed_io_bytes, last_report
        msg: Optional[str] = None
        with progress_lock:
            processed_io_bytes += chunk_bytes
            now = time.monotonic()
            if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                last_report = now
                msg = (
                    f"[snapshot] {done_files}/{len(entries)} files "
                    f"(done: {done_bytes / (1024 * 1024):.1f} / {total_mb:.1f} MB, "
                    f"processed: {processed_io_bytes / (1024 * 1024):.1f} MB, "
                    f"new: {new_objects}, reused: {reused_objects})"
                )
        if msg is not None:
            _emit_progress(msg, progress_callback)

    if entries:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {
                pool.submit(
                    _process_entry_for_cas,
                    entry,
                    hash_locks=hash_locks,
                    locks_guard=locks_guard,
                    on_bytes=_on_worker_bytes,
                ): entry
                for entry in entries
            }
            for future in as_completed(future_map):
                entry = future_map[future]
                try:
                    archive_path, sha256, size, encoding, was_new = future.result()
                    file_manifest[archive_path] = {
                        "sha256": sha256,
                        "size": size,
                        "encoding": encoding,
                    }
                    with progress_lock:
                        done_files += 1
                        done_bytes += size
                        if was_new:
                            new_objects += 1
                        else:
                            reused_objects += 1
                except Exception as exc:
                    LOGGER.warning("Failed to snapshot %s: %s", entry.abs_path, exc, exc_info=True)
                    failed.append((entry.abs_path, str(exc)))
                    with progress_lock:
                        done_files += 1

                msg_after: Optional[str] = None
                with progress_lock:
                    now = time.monotonic()
                    if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                        last_report = now
                        msg_after = (
                            f"[snapshot] {done_files}/{len(entries)} files "
                            f"(done: {done_bytes / (1024 * 1024):.1f} / {total_mb:.1f} MB, "
                            f"processed: {processed_io_bytes / (1024 * 1024):.1f} MB, "
                            f"new: {new_objects}, reused: {reused_objects})"
                        )
                if msg_after is not None:
                    _emit_progress(msg_after, progress_callback)

    if failed:
        sample = "; ".join(f"{p}: {r}" for p, r in failed[:5])
        raise RuntimeError(f"{len(failed)} file(s) could not be snapshotted ({sample})")

    sorted_manifest = dict(sorted(file_manifest.items()))
    metadata = build_metadata(
        name=name,
        note=note,
        file_count=len(sorted_manifest),
        total_bytes=done_bytes,
        files=sorted_manifest,
        format_version=CAS_SNAPSHOT_FORMAT_VERSION,
    )

    tmp_manifest = mdir / f"{name}.json.tmp.{os.getpid()}"
    try:
        _emit_progress("[snapshot] Writing manifest ...", progress_callback)
        tmp_manifest.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_manifest, manifest_path)
    except Exception:
        try:
            tmp_manifest.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # --force で同名の旧 .zip を上書きした場合は、新マニフェスト確定後に旧 .zip を片付ける
    if force and legacy_zip.exists():
        try:
            legacy_zip.unlink()
        except OSError as exc:
            LOGGER.warning("Could not remove replaced legacy zip %s: %s", legacy_zip, exc)

    elapsed = time.monotonic() - started_at
    _emit_progress(
        f"OK: Snapshot saved: {manifest_path} "
        f"({len(sorted_manifest)} files, {new_objects} new, {reused_objects} reused, {elapsed:.1f}s)",
        progress_callback,
    )
    return {
        "name": name,
        "manifest_path": str(manifest_path),
        "saiverse_version": metadata["saiverse_version"],
        "file_count": len(sorted_manifest),
        "total_bytes_uncompressed": done_bytes,
        "new_objects": new_objects,
        "reused_objects": reused_objects,
        "elapsed_seconds": round(elapsed, 2),
    }


def cmd_save(args: argparse.Namespace) -> int:
    name = args.name
    if not _is_valid_snapshot_name(name):
        print(f"ERROR: Invalid snapshot name '{name}' (use letters/digits/_-./).", file=sys.stderr)
        return 2

    try:
        result = save_snapshot(
            name,
            note=args.note or "",
            force=bool(getattr(args, "force", False)),
        )
    except Exception as exc:
        LOGGER.error("Snapshot save failed: %s", exc, exc_info=True)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"    SAIVerse version: {result['saiverse_version']}", flush=True)
    print(
        f"    Uncompressed:     {result['total_bytes_uncompressed'] / (1024 * 1024):.1f} MB "
        f"(new: {result['new_objects']}, reused: {result['reused_objects']})",
        flush=True,
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    items = list_snapshots()
    if not items:
        print("No snapshots found in", snapshots_dir())
        return 0

    print(f"{'Name':<38} {'Fmt':<4} {'Created':<20} {'Version':<10} {'Size':>10}")
    print("-" * 88)
    for item in items:
        created = (item.get("created_at") or "?")[:19]
        version = item.get("saiverse_version") or "?"
        size_bytes = item.get("archive_size_bytes")
        if size_bytes is None:
            size_bytes = item.get("total_bytes_uncompressed") or 0
        size_mb = size_bytes / (1024 * 1024)
        print(f"{item['name']:<38} {item['format']:<4} {created:<20} {version:<10} {size_mb:>7.1f} MB")

    stats = get_store_stats()
    print("-" * 88)
    print(
        f"Disk usage: CAS store {stats['cas_store_bytes'] / (1024 * 1024):.1f} MB "
        f"({stats['cas_object_count']} objects), "
        f"Legacy ZIPs {stats['legacy_zip_bytes'] / (1024 * 1024):.1f} MB "
        f"({stats['legacy_zip_count']} files)"
    )
    if stats["orphan_object_count"]:
        print(
            f"Reclaimable via 'gc': {stats['orphan_bytes'] / (1024 * 1024):.1f} MB "
            f"({stats['orphan_object_count']} unreferenced objects)"
        )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        fmt, path = resolve_snapshot(args.name)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    meta = read_snapshot_metadata(path)
    if meta is None:
        print("ERROR: Failed to read snapshot metadata.", file=sys.stderr)
        return 1

    if fmt == "zip":
        try:
            with zipfile.ZipFile(path, "r") as zf:
                namelist = [n for n in zf.namelist() if n != "snapshot.json"]
        except Exception as exc:
            print(f"ERROR: Failed to read archive: {exc}", file=sys.stderr)
            return 1
    else:
        namelist = sorted((meta.get("files") or {}).keys())

    summary_meta = {k: v for k, v in meta.items() if k != "files"}
    summary_meta["format"] = fmt
    print("=== Snapshot Metadata ===")
    print(json.dumps(summary_meta, ensure_ascii=False, indent=2))
    print()
    print(f"=== Contents ({len(namelist)} files) ===")
    if args.files:
        for n in namelist:
            print(f"  {n}")
    else:
        top_counts: dict[str, int] = {}
        for n in namelist:
            top = n.split("/", 1)[0]
            top_counts[top] = top_counts.get(top, 0) + 1
        for top, count in sorted(top_counts.items()):
            print(f"  {top}/  ({count} files)")
        print("  (use --files to list all)")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    try:
        fmt, snap = resolve_snapshot(args.name)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    running, reason = is_saiverse_running()
    if running:
        print(f"ERROR: SAIVerse appears to be running ({reason}).", file=sys.stderr)
        print("       Stop SAIVerse before restoring. This check cannot be bypassed.", file=sys.stderr)
        return 1

    meta = read_snapshot_metadata(snap)
    if meta is None:
        print("ERROR: Snapshot is unreadable, refusing to restore.", file=sys.stderr)
        return 1

    print("About to restore from snapshot:")
    print(f"  Name:     {meta.get('name')}")
    print(f"  Format:   {fmt}")
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

    home = saiverse_home()
    home.mkdir(parents=True, exist_ok=True)

    with snapshot_store_lock(), tempfile.TemporaryDirectory(
        prefix=f".{home.name}.restore-stage-",
        dir=home.parent,
    ) as stage_raw:
        stage = Path(stage_raw)
        print(f"[snapshot] Validating and staging {snap} ...", flush=True)
        try:
            if fmt == "cas":
                validate_and_extract_cas_snapshot(snap, stage)
            else:
                validate_and_extract_snapshot(snap, stage)
        except Exception as exc:
            LOGGER.error("Snapshot validation failed: %s", exc, exc_info=True)
            print(f"ERROR: Snapshot validation failed: {exc}", file=sys.stderr)
            return 1

        if not args.no_auto_snapshot:
            auto_name = f"auto_before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
            print(f"[snapshot] Creating auto-snapshot of current state: {auto_name}", flush=True)
            auto_args = argparse.Namespace(
                name=auto_name,
                note=f"Auto-saved before restoring '{args.name}'",
                force=False,
            )
            rc = cmd_save(auto_args)
            if rc != 0:
                print("ERROR: Failed to auto-snapshot; restore was not started.", file=sys.stderr)
                return rc

        print(f"[snapshot] Swapping staged world into {home} ...", flush=True)
        try:
            swap_staged_world(home, stage)
        except Exception as exc:
            LOGGER.error("World swap failed: %s", exc, exc_info=True)
            print(f"ERROR: World swap failed and old world was restored: {exc}", file=sys.stderr)
            return 1

    print(f"OK: Restored from snapshot '{args.name}'.", flush=True)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    try:
        resolve_snapshot(args.name)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
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

    try:
        result = delete_snapshot(args.name, run_gc=True)
    except Exception as exc:
        print(f"ERROR: Failed to delete snapshot '{args.name}': {exc}", file=sys.stderr)
        return 1

    reclaimed_mb = result["reclaimed_bytes"] / (1024 * 1024)
    print(f"OK: Deleted snapshot '{args.name}' (reclaimed {reclaimed_mb:.1f} MB).")
    if result.get("gc_error"):
        print(f"WARNING: Post-delete GC skipped: {result['gc_error']}", file=sys.stderr)
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    try:
        result = gc_store(dry_run=bool(args.dry_run))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    prefix = "[dry-run] Would reclaim" if args.dry_run else "OK: Reclaimed"
    print(
        f"{prefix} {result['reclaimed_bytes'] / (1024 * 1024):.1f} MB "
        f"({result['removed_objects']} objects, {result['removed_temp_files']} temp files). "
        f"Live store: {result['live_bytes'] / (1024 * 1024):.1f} MB ({result['live_objects']} objects)."
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    output_path = Path(args.output)
    try:
        exported = export_snapshot_to_zip(args.name, output_path)
    except Exception as exc:
        LOGGER.error("Snapshot export failed: %s", exc, exc_info=True)
        print(f"ERROR: Snapshot export failed: {exc}", file=sys.stderr)
        return 1
    size_mb = exported.stat().st_size / (1024 * 1024)
    print(f"OK: Exported '{args.name}' to {exported} ({size_mb:.1f} MB).")
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
                    "(or $SAIVERSE_HOME). SAIVerse should be stopped before save/restore.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("save", help="Create a new CAS snapshot")
    p.add_argument("name", help="Snapshot name (letters/digits/_-./)")
    p.add_argument("--note", help="Free-form note stored in metadata")
    p.add_argument("--force", action="store_true", help="Overwrite existing snapshot")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("list", help="List all snapshots")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("inspect", help="Show snapshot metadata and content summary")
    p.add_argument("name", help="Snapshot name")
    p.add_argument("--files", action="store_true", help="List all files in snapshot")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("restore", help="Restore from a snapshot (auto-saves current state first)")
    p.add_argument("name", help="Snapshot name")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p.add_argument("--no-auto-snapshot", action="store_true",
                   help="Skip auto-snapshot of current state before restore")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("delete", help="Delete a snapshot and reclaim unreferenced objects")
    p.add_argument("name", help="Snapshot name")
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("gc", help="Reclaim unreferenced CAS objects and stale temp files")
    p.add_argument("--dry-run", action="store_true", help="Report reclaimable space without deleting")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("export", help="Export a snapshot to a portable format v2 ZIP archive")
    p.add_argument("name", help="Snapshot name")
    p.add_argument("output", help="Output .zip file path")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
