"""旧アドオン永続データを v2 規約 (~/.saiverse/user_data/addon_data/<id>/) に移行。

設計は ``docs/intent/addon_catalog_management.md`` の「アドオン永続データ規約」を参照。

旧配置の散らかり:
    - ``~/.saiverse/addons/<addon_id>/``                 (stackchan / x-addon の独自ストレージ)
    - ``~/.saiverse/user_data/addon_files/<addon_id>/``  (voice-tts の参照音声等)
    - ``~/.saiverse/user_data/voice/out/``               (voice-tts の合成出力)

新規約: すべて ``~/.saiverse/user_data/addon_data/<addon_id>/`` 配下に統一。
アップロードされたファイル (旧 addon_files の中身) は ``inputs/`` へ、voice-tts の
合成出力 (旧 voice/out) は ``outputs/`` へサブディレクトリを切る。

移行は 2 種類ある:
    - アドオンのコードが自分で場所を決めているもの (``~/.saiverse/addons/`` と
      ``voice/out``)。アドオンのコードが新しい場所を読むようになってから動かす
      必要があるので、``addon_ids`` (起動時は ``ENABLED_ADDONS_FOR_STARTUP``) で絞る。
    - 本体のアップロード処理 (``api/routes/addon.py``) が全アドオン共通で書いて
      いたもの (``addon_files/<id>/``)。場所を決めているのは本体なので、本体の
      アップロード先が ``addon_data/<id>/inputs/`` に変わった時点で、アドオンを
      問わず全部動かす (``addon_ids`` では絞らない)。DB に記録された絶対パスは
      ``rewrite_addon_file_paths_in_db`` が同じ規則で書き換える。

冪等性:
    - 旧パスがなければ no-op
    - 新パスに既に同名要素があれば衝突警告でスキップ (上書きしない)
    - 1 度移行完了後に再起動しても同じ migrator が走るが、何もしない
    - 衝突でスキップされた状態は自動では収束しない (新しいアップロードは新パスへ
      書き続けるので、次の起動でも衝突のまま)。WARN を見た人が手で片づける

呼び出し: ``main.py`` の起動初期 (DB 初期化前後どちらでも) で
``migrate_addon_data_dirs`` を 1 回呼ぶ。``rewrite_addon_file_paths_in_db`` は
DB の場所が決まった後 (起動前バックアップの後) に呼ぶ。
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional

from saiverse.data_paths import USER_DATA_DIR, get_saiverse_home

LOGGER = logging.getLogger(__name__)

# 起動時 migration の対象アドオン (= コード側が新規約パスを参照済みのもの)。
# ここで絞るのは「アドオンのコードが自分で場所を決めている」移行だけ。
# 本体のアップロード処理が書いた addon_files/ の移行はここに関係なく全アドオンで走る。
# voice-tts は 2026-10-05 に v2 化 (合成出力を addon_data/saiverse-voice-tts/outputs/
# へ書く) したので含める。
ENABLED_ADDONS_FOR_STARTUP: tuple[str, ...] = (
    "saiverse-elyth-addon",
    "saiverse-x-addon",
    "saiverse-stackchan-addon",
    "saiverse-voice-tts",
)


@dataclass(frozen=True)
class _MigrationEntry:
    """1 件の (old_path → new_path) 移行。"""
    addon_id: str          # フィルタ用、どの addon に属する migration か
    label: str             # ログ用の人間可読ラベル
    old_path: Path
    new_path: Path


def _build_migrations() -> List[_MigrationEntry]:
    home = get_saiverse_home()
    addon_data = USER_DATA_DIR / "addon_data"

    return [
        # stackchan: addons/saiverse-stackchan-addon/ を丸ごと移動
        _MigrationEntry(
            addon_id="saiverse-stackchan-addon",
            label="stackchan-addon: ~/.saiverse/addons/ → user_data/addon_data/",
            old_path=home / "addons" / "saiverse-stackchan-addon",
            new_path=addon_data / "saiverse-stackchan-addon",
        ),
        # x-addon: addons/saiverse-x-addon/ を丸ごと移動
        _MigrationEntry(
            addon_id="saiverse-x-addon",
            label="x-addon: ~/.saiverse/addons/ → user_data/addon_data/",
            old_path=home / "addons" / "saiverse-x-addon",
            new_path=addon_data / "saiverse-x-addon",
        ),
        # voice-tts outputs: user_data/voice/out/ → addon_data/saiverse-voice-tts/outputs/
        _MigrationEntry(
            addon_id="saiverse-voice-tts",
            label="voice-tts outputs: user_data/voice/out/ → addon_data/saiverse-voice-tts/outputs/",
            old_path=USER_DATA_DIR / "voice" / "out",
            new_path=addon_data / "saiverse-voice-tts" / "outputs",
        ),
    ]


def _legacy_upload_base() -> Path:
    """本体のアップロード処理が旧規約で書いていた場所 (``user_data/addon_files/``)。"""
    return USER_DATA_DIR / "addon_files"


def _upload_inputs_dir(addon_id: str) -> Path:
    """アップロードされたファイルの新しい置き場所 (``addon_data/<id>/inputs/``)。

    ``api/routes/addon.py`` のアップロード先と同じ規則。あちらは
    ``get_addon_data_dir`` を使うが、こちらは移行の判定でディレクトリを作らない
    ために同じ形を直接組み立てる。
    """
    return USER_DATA_DIR / "addon_data" / addon_id / "inputs"


def _build_upload_migrations() -> List[_MigrationEntry]:
    """``addon_files/`` 直下のアドオンごとのフォルダを、全部 ``inputs/`` へ動かす移行。

    本体のアップロード処理が全アドオン共通で書いていた場所なので、アドオンを
    問わず同じ一本の規則で動かす (voice-tts の参照音声もこの規則で動く)。
    """
    base = _legacy_upload_base()
    if not base.is_dir():
        return []
    entries: List[_MigrationEntry] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        entries.append(
            _MigrationEntry(
                addon_id=child.name,
                label=f"{child.name} uploads: user_data/addon_files/ → addon_data/{child.name}/inputs/",
                old_path=child,
                new_path=_upload_inputs_dir(child.name),
            )
        )
    return entries


def _try_move(entry: _MigrationEntry) -> str:
    """1 件の migration を試行。結果ステータス (人間可読) を返す。

    - 旧パス不存在: "skipped (no source)"
    - 旧パス空ディレクトリ: 旧パスを削除して "removed (empty source)"
    - 新パス既存 + 何か入ってる: 衝突 → 旧パスを残して警告
    - 新パス不存在 or 空: atomic rename (失敗時 copy + remove フォールバック)
    """
    old = entry.old_path
    new = entry.new_path
    if not old.exists():
        LOGGER.debug("addon_migrations: %s — old path not found, skip", entry.label)
        return "skipped (no source)"

    # 旧パスが空ディレクトリなら掃除だけして終わり (voice-tts の addons/ 空ケース等)
    if old.is_dir() and not any(old.iterdir()):
        try:
            old.rmdir()
            LOGGER.info("addon_migrations: %s — removed empty source %s", entry.label, old)
            return "removed (empty source)"
        except OSError as e:
            LOGGER.warning(
                "addon_migrations: %s — failed to remove empty source %s: %s",
                entry.label, old, e,
            )
            return f"failed to remove empty source: {e}"

    # 新パスが既に存在し、かつ非空なら衝突
    if new.exists():
        is_dir = new.is_dir()
        non_empty = is_dir and any(new.iterdir())
        if non_empty or not is_dir:
            LOGGER.warning(
                "addon_migrations: %s — destination %s already exists and is non-empty, "
                "skipping (manual merge required)",
                entry.label, new,
            )
            return "skipped (destination not empty)"
        # 空ディレクトリの new は削除して rename 用に空ける
        try:
            new.rmdir()
        except OSError as e:
            LOGGER.warning(
                "addon_migrations: %s — failed to clear empty destination %s: %s",
                entry.label, new, e,
            )
            return f"failed to clear empty destination: {e}"

    # ここまできたら new は不存在、または空 dir を削除済み
    new.parent.mkdir(parents=True, exist_ok=True)

    try:
        # 同一 volume なら atomic rename。違う volume だと OSError EXDEV
        old.rename(new)
        LOGGER.info("addon_migrations: %s — moved %s → %s", entry.label, old, new)
        return "moved"
    except OSError:
        # フォールバック: copytree (file の場合は copy2) + rmtree
        LOGGER.info(
            "addon_migrations: %s — rename failed, falling back to copy+remove",
            entry.label,
        )
        try:
            if old.is_dir():
                shutil.copytree(str(old), str(new))
                shutil.rmtree(str(old))
            else:
                shutil.copy2(str(old), str(new))
                old.unlink()
            LOGGER.info(
                "addon_migrations: %s — copy+remove fallback succeeded", entry.label
            )
            return "moved (via copy)"
        except (OSError, shutil.Error) as e:
            LOGGER.error(
                "addon_migrations: %s — copy fallback also failed: %s",
                entry.label, e,
            )
            return f"failed: {e}"


def migrate_addon_data_dirs(
    addon_ids: Optional[Iterable[str]] = None,
) -> dict[str, str]:
    """旧 → 新パスの移行を実行。

    Args:
        addon_ids: アドオンのコードが自分で場所を決めている移行の対象 addon の
                   id 集合 (省略時は全 addon)。起動時は
                   ``ENABLED_ADDONS_FOR_STARTUP`` を渡すこと。コードが新規約に
                   揃っていないアドオンを含めると、データ移動後にコードから
                   見えなくなる。
                   本体のアップロード処理が書いた ``addon_files/`` の移行は
                   この絞り込みを受けず、常に全アドオン分を動かす (モジュールの
                   docstring 参照)。

    Returns:
        各 migration エントリの結果ステータス dict (label → status)。
        例: {"x-addon: ...": "moved", "voice-tts outputs: ...": "skipped (no source)"}
    """
    targets = set(addon_ids) if addon_ids is not None else None
    results: dict[str, str] = {}
    for entry in _build_migrations():
        if targets is not None and entry.addon_id not in targets:
            continue
        results[entry.label] = _try_move(entry)
    for entry in _build_upload_migrations():
        results[entry.label] = _try_move(entry)
    # 中身を全部動かし終えた addon_files/ 自体は空になるので掃除する
    base = _legacy_upload_base()
    if base.is_dir() and not any(base.iterdir()):
        try:
            base.rmdir()
            LOGGER.info("addon_migrations: removed empty legacy upload dir %s", base)
        except OSError as e:
            LOGGER.warning(
                "addon_migrations: failed to remove empty legacy upload dir %s: %s", base, e
            )
    moved_count = sum(1 for s in results.values() if s.startswith("moved"))
    skipped_count = sum(1 for s in results.values() if s.startswith("skipped"))
    failed_count = sum(1 for s in results.values() if s.startswith("failed"))
    LOGGER.info(
        "addon_migrations: done — %d moved, %d skipped, %d failed",
        moved_count, skipped_count, failed_count,
    )
    return results


# ---------------------------------------------------------------------------
# DB に記録されたアップロード済みファイルの絶対パスの書き換え
# ---------------------------------------------------------------------------

# params_json を持つアドオン設定のテーブルと、その行を指す主キー列
_PARAMS_TABLES: tuple[tuple[str, str], ...] = (
    ("addon_config", "addon_name"),
    ("addon_persona_config", "id"),
)


def _path_parts(value: str) -> List[str]:
    """パス文字列を、区切り (``\\`` と ``/`` のどちらでも) で分けた部品の列にする。

    ``..`` や重複した区切りは先に畳む。先頭の区切り (POSIX の ``/``、UNC の
    ``\\\\``) は空の部品として残るので、相対パスが絶対パスの前方に一致することは
    ない。Windows では大文字小文字を区別しないファイルシステムなので、比較用に
    小文字へ揃えるのは呼び出し側 (``_fold``) で行う。
    """
    return posixpath.normpath(value.replace("\\", "/")).split("/")


def _fold(parts: List[str]) -> List[str]:
    # Windows と macOS の既定のファイルシステムは大文字小文字を区別しない。
    # 記録時と今とで表記 (SAIVERSE_HOME の指定など) が違っても一致させる。
    if os.name == "nt" or sys.platform == "darwin":
        return [p.casefold() for p in parts]
    return parts


def _map_legacy_upload_path(value: str) -> Optional[Path]:
    """旧 ``addon_files/<id>/...`` を指す絶対パスなら、対応する新しいパスを返す。

    当てはまらない文字列 (別の場所のパス・パスでない値) には None を返す。
    """
    if not value or not isinstance(value, str):
        return None
    base_parts = _path_parts(str(_legacy_upload_base()))
    parts = _path_parts(value)
    n = len(base_parts)
    # <base>/<addon_id> 以下を対象にする (<base>/<addon_id> そのもの = フォルダを
    # 指す記録も、フォルダ側の移行と揃えて inputs/ へ書き換える)
    if len(parts) < n + 1:
        return None
    if _fold(parts[:n]) != _fold(base_parts):
        return None
    addon_id = parts[n]
    if not re.match(r"^[A-Za-z0-9_.-]+$", addon_id) or addon_id in (".", ".."):
        return None
    return _upload_inputs_dir(addon_id).joinpath(*parts[n + 1:])


def _rewrite_value(value: Any, stats: dict[str, int]) -> Any:
    """params の値を (ネストも含めて) 辿り、旧パスを新パスへ置き換えた値を返す。"""
    if isinstance(value, dict):
        return {k: _rewrite_value(v, stats) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_value(v, stats) for v in value]
    if isinstance(value, str):
        new_path = _map_legacy_upload_path(value)
        if new_path is None:
            return value
        # フォルダの移行が衝突などで行われず、ファイルがまだ旧い場所にしか無いなら、
        # 書き換えると今あるファイルを見失う。旧い場所のまま残して次の起動で再判定する。
        if Path(value).exists() and not new_path.exists():
            LOGGER.warning(
                "addon_migrations: %s still exists at the legacy location and is not at %s; "
                "keeping the recorded path",
                value, new_path,
            )
            stats["kept"] += 1
            return value
        stats["rewritten"] += 1
        return str(new_path)
    return value


def rewrite_addon_file_paths_in_db(db_path: str) -> dict[str, int]:
    """アドオン設定の params_json に記録された旧 ``addon_files/`` のパスを書き換える。

    本体のアップロード処理は、保存したファイルの絶対パスを params_json に記録し、
    アドオンはその絶対パスをそのまま使う (voice-tts の参照音声など)。
    ``migrate_addon_data_dirs`` がフォルダを ``addon_data/<id>/inputs/`` へ動かした
    後に、記録の側も同じ規則 (``<user_data>/addon_files/<id>/<rest>`` →
    ``<user_data>/addon_data/<id>/inputs/<rest>``) で揃える。

    冪等: 書き換え後の値は ``addon_files/`` を指さないので、二度目以降は何もしない。
    旧い場所にファイルが残っていて新しい場所に無い値 (フォルダの移行が衝突で
    止まったもの) は書き換えない。

    Returns:
        {"rows": 書き換えた行数, "rewritten": 書き換えた値の数, "kept": 残した値の数}
    """
    stats = {"rows": 0, "rewritten": 0, "kept": 0}
    if not Path(db_path).exists():
        # create_engine は存在しない DB ファイルを空で作ってしまうので触らない
        return stats

    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        existing = set(inspect(engine).get_table_names())
        with engine.begin() as conn:
            for table, key_col in _PARAMS_TABLES:
                if table not in existing:
                    continue
                rows = conn.execute(
                    text(f"SELECT {key_col}, params_json FROM {table} WHERE params_json IS NOT NULL")
                ).fetchall()
                for key, params_json in rows:
                    try:
                        params = json.loads(params_json)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    new_params = _rewrite_value(params, stats)
                    if new_params == params:
                        continue
                    conn.execute(
                        text(f"UPDATE {table} SET params_json = :p WHERE {key_col} = :k"),
                        {"p": json.dumps(new_params, ensure_ascii=False), "k": key},
                    )
                    stats["rows"] += 1
    finally:
        engine.dispose()

    LOGGER.info(
        "addon_migrations: rewrote legacy addon_files paths in DB — %d rows, %d values "
        "(%d kept at legacy location)",
        stats["rows"], stats["rewritten"], stats["kept"],
    )
    return stats


__all__ = [
    "migrate_addon_data_dirs",
    "rewrite_addon_file_paths_in_db",
    "ENABLED_ADDONS_FOR_STARTUP",
]
