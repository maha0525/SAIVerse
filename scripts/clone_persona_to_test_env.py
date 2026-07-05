#!/usr/bin/env python
"""本番ペルソナをテスト環境へ複製するスクリプト。

一日シミュレータの実 LLM モード (scripts/run_day_sim.py --real) を、本物の
記憶・人格を持つペルソナの複製で「本番を汚さずに」試すためのツール。

Usage:
    python scripts/clone_persona_to_test_env.py --persona <id> \\
        [--source-db <path>]   # 省略時: 本番の default_db_path()
        [--source-home <path>] # 省略時: 本番の SAIVERSE_HOME (~/.saiverse)
        [--dest-db <path>]     # 省略時: test_data/user_data/database/saiverse.db
        [--dest-home <path>]   # 省略時: test_data/.saiverse
        [--force]              # 既存複製の上書き確認をスキップ

処理内容:
    1. AI 行の複製 (HOME_CITYID / PRIVATE_ROOM_ID / 現在位置をテスト環境へ再マップ、
       実行時状態はリセット)。現在位置は AI テーブルのカラムではなく
       BuildingOccupancyLog の「開いている行 (EXIT_TIMESTAMP IS NULL)」なので、
       dest 側に再マップした occupancy 行を新規挿入する。
    2. ペルソナディレクトリ (memory.db / tasks.db 等) の複製
    3. AI が参照するモデル設定 JSON (と provider_ref 先の provider JSON) が
       builtin_data に無く source の user_data にだけある場合、dest へコピー
    4. 完了サマリと次の手順の案内

安全性:
    - **source (本番) 側は読み取り専用**。source DB は SQLite の mode=ro URI で
      開き、ファイルは copy2 で読むだけ。書き込みは dest (テスト環境) のみ。
    - dest に同じペルソナの複製が既にある場合、--force が無ければ上書き前に
      確認プロンプトを出す。
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# プロジェクトルートを追加
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Building, BuildingOccupancyLog, City

LOGGER = logging.getLogger("scripts.clone_persona_to_test_env")


class CloneError(RuntimeError):
    """複製処理の前提が満たされていない場合のエラー (メッセージをそのまま表示)。"""


# ---------------------------------------------------------------------------
# AI テーブルのカラム分類
# ---------------------------------------------------------------------------
# 「恒常設定 (人格・記憶・モデル・トグル類)」はそのまま複製し、
# 「実行時状態」はリセット、「環境依存 FK」は dest 環境へ再マップする。
# AI テーブルにカラムが増えたとき、ここに分類が無いと起動時に明示的に
# エラーになる (_verify_column_classification) — 黙って欠落させない。

#: そのまま複製する恒常設定カラム
PERSISTENT_COLUMNS = [
    "AIID",
    "AINAME",
    "SYSTEMPROMPT",
    "DESCRIPTION",
    "AVATAR_IMAGE",
    "APPEARANCE_IMAGE_PATH",
    # EMOTION は「今の感情状態」だが、人格の連続性の一部として複製する
    "EMOTION",
    "DEFAULT_MODEL",
    "LIGHTWEIGHT_MODEL",
    "LIGHTWEIGHT_VISION_MODEL",
    "VISION_MODEL",
    "AUDIO_MODEL",
    "VIDEO_MODEL",
    "MEMORY_WEAVE_MODEL",
    "CHRONICLE_ENABLED",
    "AUTONOMOUS_CHRONICLE_ENABLED",
    "AUTO_RECALL_ENABLED",
    "MEMORY_WEAVE_CONTEXT",
    "MEMOPEDIA_INDEX_LIMIT",
    "MEMOPEDIA_INDEX_ENABLED",
    "CORE_MEMORY_CHAR_BUDGET",
    "SPELL_ENABLED",
    "REALTIME_INFO_ENABLED",
    # METABOLISM_ANCHORS の anchor_id は memory.db 内を指す。memory.db を
    # バイト単位で複製するため anchor は複製先でも有効 — 対で複製する
    "METABOLISM_ANCHORS",
    # 活動モード (Stop/Sleep/Idle/Active) は運用設定。本番で Active なら
    # 複製も Active — 自律行動テストの目的に合致するため複製する
    "ACTIVITY_STATE",
    "SLEEP_ON_CACHE_EXPIRE",
    # 複製直後にバージョンアップグレード通知が走らないよう本番の値を引き継ぐ
    "LAST_KNOWN_VERSION",
    "META_JUDGMENT_CONFIG",
    "PERSONA_ROLE",
    "USER_CONV_TIMEOUT_MINUTES",
    "LIFE_PURPOSE",
]

#: リセットする実行時状態カラム (→ リセット値)
RUNTIME_RESET_COLUMNS: Dict[str, Any] = {
    "IS_DISPATCHED": False,       # 他 City への派遣中フラグ
    "AUTO_COUNT": 0,              # 自動発話カウンタ
    "LAST_AUTO_PROMPT_TIMES": None,  # 自動発話の時刻記録 (JSON)
}

#: dest 環境の City / Building へ再マップするカラム
REMAPPED_COLUMNS = [
    "HOME_CITYID",     # → dest の最初の City
    "PRIVATE_ROOM_ID",  # → dest の Building (同 ID > 同名 > 任意 > NULL)
]

#: AI が参照するモデル設定カラム (モデル JSON 依存解決の対象)
MODEL_COLUMNS = [
    "DEFAULT_MODEL",
    "LIGHTWEIGHT_MODEL",
    "LIGHTWEIGHT_VISION_MODEL",
    "VISION_MODEL",
    "AUDIO_MODEL",
    "VIDEO_MODEL",
    "MEMORY_WEAVE_MODEL",
]


def _verify_column_classification() -> None:
    """AI テーブルの全カラムが分類済みであることを確認する (増設カラムの検知)。"""
    all_columns = set(AI.__table__.columns.keys())
    classified = set(PERSISTENT_COLUMNS) | set(RUNTIME_RESET_COLUMNS) | set(REMAPPED_COLUMNS)
    unclassified = all_columns - classified
    unknown = classified - all_columns
    if unclassified:
        raise CloneError(
            "AI テーブルに未分類のカラムがあります: "
            f"{sorted(unclassified)}。scripts/clone_persona_to_test_env.py の "
            "PERSISTENT_COLUMNS / RUNTIME_RESET_COLUMNS / REMAPPED_COLUMNS に分類を追加してください。"
        )
    if unknown:
        raise CloneError(f"分類リストに存在しないカラム名があります: {sorted(unknown)}")


# ---------------------------------------------------------------------------
# パス解決
# ---------------------------------------------------------------------------


def _default_source_db() -> Path:
    from database.paths import default_db_path
    return default_db_path()


def _default_source_home() -> Path:
    from saiverse.data_paths import get_saiverse_home
    return get_saiverse_home()


def _user_data_dir_for(db_path: Path, home: Path) -> Path:
    """DB パスから user_data ディレクトリを導出する。

    標準レイアウト (<user_data>/database/saiverse.db) なら 2 つ上、
    それ以外は home/user_data にフォールバック。
    """
    if db_path.parent.name == "database":
        return db_path.parent.parent
    return home / "user_data"


# ---------------------------------------------------------------------------
# DB 操作
# ---------------------------------------------------------------------------


def _open_source_session(db_path: Path):
    """source DB を SQLite の読み取り専用モードで開く (本番保護)。

    Returns:
        (session, engine)。Windows でファイルハンドルが残らないよう、
        呼び出し側は session.close() 後に engine.dispose() すること。
    """
    uri = f"sqlite:///file:{db_path.as_posix()}?mode=ro&uri=true"
    engine = create_engine(uri)
    return sessionmaker(bind=engine)(), engine


def _open_dest_session(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path}")
    return sessionmaker(bind=engine)(), engine


def _read_source_persona(source_db: Path, persona_id: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Dict[str, str]]:
    """source DB からペルソナ行・現在位置・関連 Building 名を読み取る。

    Returns:
        (AI 行の {カラム名: 値}, 開いている occupancy 行の情報 or None,
         {BUILDINGID: BUILDINGNAME} 参照される source Building の名前)
    """
    session, engine = _open_source_session(source_db)
    try:
        row = session.query(AI).filter(AI.AIID == persona_id).first()
        if row is None:
            raise CloneError(
                f"source DB ({source_db}) にペルソナ '{persona_id}' が見つかりません。"
            )
        values = {name: getattr(row, name) for name in AI.__table__.columns.keys()}

        occupancy: Optional[Dict[str, Any]] = None
        occ_row = (
            session.query(BuildingOccupancyLog)
            .filter(BuildingOccupancyLog.AIID == persona_id)
            .filter(BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None))
            .order_by(BuildingOccupancyLog.ENTRY_TIMESTAMP.desc())
            .first()
        )
        if occ_row is not None:
            occupancy = {"BUILDINGID": occ_row.BUILDINGID}

        # 参照される source Building の名前 (同名マッチ用)
        ref_ids = [bid for bid in (values.get("PRIVATE_ROOM_ID"),
                                   occupancy["BUILDINGID"] if occupancy else None) if bid]
        building_names: Dict[str, str] = {}
        if ref_ids:
            for b in session.query(Building).filter(Building.BUILDINGID.in_(ref_ids)).all():
                building_names[b.BUILDINGID] = b.BUILDINGNAME
        return values, occupancy, building_names
    finally:
        session.close()
        engine.dispose()


def _remap_building(
    source_building_id: Optional[str],
    source_building_names: Dict[str, str],
    dest_buildings: List[Building],
    *,
    label: str,
) -> Optional[str]:
    """source の Building ID を dest に実在する Building へ再マップする。

    優先順位: 同 BUILDINGID > 同 BUILDINGNAME > dest の最初の Building > None
    """
    if not dest_buildings:
        LOGGER.warning("%s: dest に Building が 1 つも無いため NULL にします", label)
        return None
    if source_building_id is None:
        return None

    dest_by_id = {b.BUILDINGID: b for b in dest_buildings}
    if source_building_id in dest_by_id:
        LOGGER.info("%s: '%s' は dest にも存在 → そのまま使用", label, source_building_id)
        return source_building_id

    source_name = source_building_names.get(source_building_id)
    if source_name:
        for b in dest_buildings:
            if b.BUILDINGNAME == source_name:
                LOGGER.info(
                    "%s: 同名 Building '%s' にマップ ('%s' → '%s')",
                    label, source_name, source_building_id, b.BUILDINGID,
                )
                return b.BUILDINGID

    fallback = dest_buildings[0]
    LOGGER.info(
        "%s: 対応する Building が無いため dest の '%s' (%s) にマップ",
        label, fallback.BUILDINGID, fallback.BUILDINGNAME,
    )
    return fallback.BUILDINGID


def _clone_ai_row(
    source_values: Dict[str, Any],
    source_occupancy: Optional[Dict[str, Any]],
    source_building_names: Dict[str, str],
    dest_db: Path,
    persona_id: str,
) -> Dict[str, Any]:
    """AI 行を dest DB に複製 (upsert) し、再マップ結果を返す。"""
    session, engine = _open_dest_session(dest_db)
    try:
        dest_city = session.query(City).order_by(City.CITYID).first()
        if dest_city is None:
            raise CloneError(
                f"dest DB ({dest_db}) に City がありません。"
                "先に python test_fixtures/setup_test_env.py を実行してください。"
            )
        dest_buildings = (
            session.query(Building)
            .filter(Building.CITYID == dest_city.CITYID)
            .order_by(Building.BUILDINGID)
            .all()
        )

        new_values = dict(source_values)

        # 実行時状態のリセット
        for name, reset_value in RUNTIME_RESET_COLUMNS.items():
            new_values[name] = reset_value

        # 環境依存 FK の再マップ
        remap_report: Dict[str, Any] = {
            "HOME_CITYID": (source_values["HOME_CITYID"], dest_city.CITYID),
        }
        new_values["HOME_CITYID"] = dest_city.CITYID
        LOGGER.info(
            "HOME_CITYID: %s → %s (%s)",
            source_values["HOME_CITYID"], dest_city.CITYID, dest_city.CITYNAME,
        )

        new_private_room = _remap_building(
            source_values.get("PRIVATE_ROOM_ID"), source_building_names,
            dest_buildings, label="PRIVATE_ROOM_ID",
        )
        new_values["PRIVATE_ROOM_ID"] = new_private_room
        remap_report["PRIVATE_ROOM_ID"] = (source_values.get("PRIVATE_ROOM_ID"), new_private_room)

        # 既存の複製を削除 (呼び出し側で上書き確認済み)
        existing = session.query(AI).filter(AI.AIID == persona_id).first()
        if existing is not None:
            LOGGER.info("dest の既存 AI 行を削除して差し替えます: %s", persona_id)
            session.delete(existing)
        deleted_occ = (
            session.query(BuildingOccupancyLog)
            .filter(BuildingOccupancyLog.AIID == persona_id)
            .delete()
        )
        if deleted_occ:
            LOGGER.info("dest の既存 occupancy 行を %d 件削除しました", deleted_occ)
        session.flush()

        session.add(AI(**new_values))

        # 現在位置 (開いている occupancy 行) の再マップ。
        # source に開いている行が無ければ私室 → 先頭 Building の順でフォールバック。
        source_loc = source_occupancy["BUILDINGID"] if source_occupancy else None
        if source_loc is not None:
            new_location = _remap_building(
                source_loc, source_building_names, dest_buildings, label="現在位置",
            )
        else:
            new_location = new_private_room or (dest_buildings[0].BUILDINGID if dest_buildings else None)
            LOGGER.info(
                "source に開いている occupancy 行が無いため現在位置は '%s' にします", new_location,
            )
        remap_report["current_building"] = (source_loc, new_location)

        if new_location is not None:
            session.add(BuildingOccupancyLog(
                CITYID=dest_city.CITYID,
                BUILDINGID=new_location,
                AIID=persona_id,
                ENTRY_TIMESTAMP=datetime.now(),
                EXIT_TIMESTAMP=None,
            ))
        else:
            LOGGER.warning("現在位置を配置できません (dest に Building がありません)")

        session.commit()
        return remap_report
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# ペルソナディレクトリの複製
# ---------------------------------------------------------------------------


def _copy_persona_dir(source_dir: Path, dest_dir: Path) -> Tuple[int, int]:
    """ペルソナディレクトリを丸ごと複製する (進捗ログ付き)。

    Returns:
        (複製したファイル数, 総バイト数)
    """
    if (source_dir / "memory.db-wal").exists():
        LOGGER.warning(
            "memory.db-wal が存在します。本番 SAIVerse が起動中の可能性があります — "
            "整合性のため本番を停止してから複製することを推奨します。"
        )

    files = sorted(p for p in source_dir.rglob("*") if p.is_file())
    total_bytes = sum(p.stat().st_size for p in files)
    LOGGER.info(
        "ペルソナディレクトリ複製開始: %s → %s (%d ファイル / %.1f MB)",
        source_dir, dest_dir, len(files), total_bytes / (1024 * 1024),
    )

    if dest_dir.exists():
        LOGGER.info("dest の既存ディレクトリを削除します: %s", dest_dir)
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied_bytes = 0
    for i, src_file in enumerate(files, start=1):
        rel = src_file.relative_to(source_dir)
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        size = src_file.stat().st_size
        shutil.copy2(src_file, target)
        copied_bytes += size
        if i % 20 == 0 or size > 10 * 1024 * 1024:
            LOGGER.info(
                "  ... %d/%d ファイル (%.1f/%.1f MB) — %s",
                i, len(files), copied_bytes / (1024 * 1024),
                total_bytes / (1024 * 1024), rel,
            )
    LOGGER.info("ペルソナディレクトリ複製完了: %d ファイル", len(files))
    return len(files), total_bytes


# ---------------------------------------------------------------------------
# モデル / プロバイダ設定の依存解決
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("JSON の読み込みに失敗: %s (%s)", path, exc)
        return None


def _find_model_json(model_name: str, models_dir: Path) -> Optional[Path]:
    """モデル設定 JSON を探す (ファイル名 stem 一致 or "model" フィールド一致)。

    saiverse/model_configs.py の find_model_config と同じ規則の 1〜2 段目。
    """
    if not models_dir.is_dir():
        return None
    exact = models_dir / f"{model_name}.json"
    if exact.is_file():
        return exact
    for path in sorted(models_dir.glob("*.json")):
        data = _load_json(path)
        if data and data.get("model") == model_name:
            return path
    return None


def _find_provider_json(provider_id: str, providers_dir: Path) -> Optional[Path]:
    """プロバイダ設定 JSON を探す ("id" フィールド一致 or ファイル名 stem 一致)。

    saiverse/provider_configs.py は id = config["id"] or filename stem。
    """
    if not providers_dir.is_dir():
        return None
    for path in sorted(providers_dir.glob("*.json")):
        data = _load_json(path)
        if data is None:
            continue
        if (data.get("id") or path.stem) == provider_id:
            return path
    return None


def _resolve_model_dependencies(
    ai_values: Dict[str, Any],
    source_user_data: Path,
    dest_user_data: Path,
) -> Dict[str, List[str]]:
    """AI が参照するモデル設定の依存を dest 環境で解決する。

    読み込み優先度は user_data > builtin なので、source の user_data/models に
    あるモデルはそれが実効設定 — builtin に同名があっても dest へコピーする
    (builtin モデルの user_data 上書きを落とさないため)。user_data に無く
    builtin にあるモデルは何もしない。コピーしたモデルの provider_ref 先の
    provider JSON も同様に解決する。
    """
    builtin_models = ROOT / "builtin_data" / "models"
    builtin_providers = ROOT / "builtin_data" / "providers"
    source_models = source_user_data / "models"
    source_providers = source_user_data / "providers"
    dest_models = dest_user_data / "models"
    dest_providers = dest_user_data / "providers"

    report: Dict[str, List[str]] = {
        "builtin": [], "copied_models": [], "copied_providers": [], "missing": [],
    }

    model_names = sorted({
        ai_values[col] for col in MODEL_COLUMNS if ai_values.get(col)
    })
    provider_refs: set[str] = set()

    for name in model_names:
        # user_data > builtin の優先度に合わせ、source user_data を先に見る
        source_path = _find_model_json(name, source_models)
        if source_path is None:
            if _find_model_json(name, builtin_models) is not None:
                LOGGER.info("モデル '%s': builtin_data にあり — コピー不要", name)
                report["builtin"].append(name)
            else:
                LOGGER.warning(
                    "モデル '%s': builtin_data にも source の user_data/models にも"
                    "見つかりません。dest で手動設定が必要かもしれません。", name,
                )
                report["missing"].append(name)
            continue
        dest_models.mkdir(parents=True, exist_ok=True)
        dest_path = dest_models / source_path.name
        shutil.copy2(source_path, dest_path)
        LOGGER.info("モデル '%s': %s → %s へコピー", name, source_path, dest_path)
        report["copied_models"].append(name)

        data = _load_json(source_path) or {}
        ref = data.get("provider_ref")
        if ref:
            provider_refs.add(ref)

    for ref in sorted(provider_refs):
        source_path = _find_provider_json(ref, source_providers)
        if source_path is None:
            if _find_provider_json(ref, builtin_providers) is not None:
                LOGGER.info("プロバイダ '%s': builtin_data にあり — コピー不要", ref)
            else:
                LOGGER.warning(
                    "プロバイダ '%s': builtin_data にも source の user_data/providers にも"
                    "見つかりません。", ref,
                )
                report["missing"].append(f"provider:{ref}")
            continue
        dest_providers.mkdir(parents=True, exist_ok=True)
        dest_path = dest_providers / source_path.name
        shutil.copy2(source_path, dest_path)
        LOGGER.info("プロバイダ '%s': %s → %s へコピー", ref, source_path, dest_path)
        report["copied_providers"].append(ref)

    return report


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------


def _default_confirm(message: str) -> bool:
    answer = input(f"{message} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def clone_persona(
    persona_id: str,
    source_db: Path,
    source_home: Path,
    dest_db: Path,
    dest_home: Path,
    *,
    force: bool = False,
    confirm: Callable[[str], bool] = _default_confirm,
) -> Dict[str, Any]:
    """本番ペルソナをテスト環境へ複製する。

    Returns:
        サマリ情報の dict (再マップ結果・複製ファイル数・モデル依存解決)。
    Raises:
        CloneError: 前提未達 (source に無い / dest 未セットアップ / ユーザー中断)。
    """
    _verify_column_classification()

    source_db = Path(source_db).resolve()
    dest_db = Path(dest_db).resolve()
    source_home = Path(source_home).resolve()
    dest_home = Path(dest_home).resolve()

    LOGGER.info("複製元 DB   : %s", source_db)
    LOGGER.info("複製元 home : %s", source_home)
    LOGGER.info("複製先 DB   : %s", dest_db)
    LOGGER.info("複製先 home : %s", dest_home)

    if source_db == dest_db:
        raise CloneError("source と dest の DB が同一です。複製になりません。")
    if not source_db.is_file():
        raise CloneError(f"source DB が見つかりません: {source_db}")
    if not dest_db.is_file():
        raise CloneError(
            f"dest DB が見つかりません: {dest_db}。"
            "先に python test_fixtures/setup_test_env.py を実行してください。"
        )

    source_persona_dir = source_home / "personas" / persona_id
    dest_persona_dir = dest_home / "personas" / persona_id

    # --- 上書き確認 (dest 側の既存複製) ---
    session, engine = _open_dest_session(dest_db)
    try:
        dest_existing = session.query(AI).filter(AI.AIID == persona_id).first() is not None
    finally:
        session.close()
        engine.dispose()
    if (dest_existing or dest_persona_dir.exists()) and not force:
        detail = []
        if dest_existing:
            detail.append("AI 行")
        if dest_persona_dir.exists():
            detail.append(f"ペルソナディレクトリ ({dest_persona_dir})")
        if not confirm(
            f"テスト環境に '{persona_id}' の既存複製があります ({' / '.join(detail)})。上書きしますか?"
        ):
            raise CloneError("ユーザーにより中断されました (--force で確認をスキップできます)。")

    # --- 1. AI 行の複製 ---
    source_values, source_occupancy, building_names = _read_source_persona(source_db, persona_id)
    remap_report = _clone_ai_row(
        source_values, source_occupancy, building_names, dest_db, persona_id,
    )

    # --- 2. ペルソナディレクトリの複製 ---
    if source_persona_dir.is_dir():
        file_count, total_bytes = _copy_persona_dir(source_persona_dir, dest_persona_dir)
    else:
        LOGGER.warning(
            "source にペルソナディレクトリがありません: %s (memory.db 無しで続行します)",
            source_persona_dir,
        )
        file_count, total_bytes = 0, 0

    # --- 3. モデル設定の依存解決 ---
    source_user_data = _user_data_dir_for(source_db, source_home)
    dest_user_data = _user_data_dir_for(dest_db, dest_home)
    model_report = _resolve_model_dependencies(source_values, source_user_data, dest_user_data)

    # --- 4. サマリ ---
    summary = {
        "persona_id": persona_id,
        "remap": remap_report,
        "reset_columns": dict(RUNTIME_RESET_COLUMNS),
        "persona_dir_files": file_count,
        "persona_dir_bytes": total_bytes,
        "models": model_report,
        "dest_db": str(dest_db),
        "dest_home": str(dest_home),
    }
    _print_summary(summary)
    return summary


def _print_summary(summary: Dict[str, Any]) -> None:
    remap = summary["remap"]
    LOGGER.info("=" * 64)
    LOGGER.info("複製完了: %s", summary["persona_id"])
    LOGGER.info("-" * 64)
    LOGGER.info("再マップ:")
    LOGGER.info("  HOME_CITYID      : %s → %s", *remap["HOME_CITYID"])
    LOGGER.info("  PRIVATE_ROOM_ID  : %s → %s", *remap["PRIVATE_ROOM_ID"])
    LOGGER.info("  現在位置 (occupancy): %s → %s", *remap["current_building"])
    LOGGER.info("リセットした実行時状態: %s", summary["reset_columns"])
    LOGGER.info(
        "ペルソナディレクトリ: %d ファイル (%.1f MB)",
        summary["persona_dir_files"], summary["persona_dir_bytes"] / (1024 * 1024),
    )
    models = summary["models"]
    LOGGER.info(
        "モデル設定: builtin=%s / コピー=%s / プロバイダコピー=%s / 未解決=%s",
        models["builtin"], models["copied_models"],
        models["copied_providers"], models["missing"],
    )
    LOGGER.info("-" * 64)
    LOGGER.info("次の手順:")
    LOGGER.info("  1. 判断点 playbook をテスト DB に import (未実施なら):")
    LOGGER.info("     python scripts/import_playbook.py --file builtin_data/playbooks/public/judgment_day_open.json")
    LOGGER.info("     (day_open / post_session / post_conversation / on_event / day_close の 5 ファイル)")
    LOGGER.info("  2. シナリオの persona_id を '%s' に書き換えて実行:", summary["persona_id"])
    LOGGER.info("     SAIVERSE_HOME=%s SAIVERSE_USER_DATA_DIR=<dest user_data> \\", summary["dest_home"])
    LOGGER.info(
        "     python scripts/run_day_sim.py --scenario <file> --real --db-file %s",
        summary["dest_db"],
    )
    LOGGER.info("=" * 64)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="本番ペルソナをテスト環境へ複製する (本番側は読み取りのみ)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--persona", required=True, help="複製するペルソナ ID (AIID)")
    parser.add_argument("--source-db", type=Path, default=None,
                        help="複製元 DB (省略時: 本番の default_db_path())")
    parser.add_argument("--source-home", type=Path, default=None,
                        help="複製元 SAIVERSE_HOME (省略時: ~/.saiverse)")
    parser.add_argument("--dest-db", type=Path,
                        default=ROOT / "test_data" / "user_data" / "database" / "saiverse.db",
                        help="複製先 DB (省略時: test_data/user_data/database/saiverse.db)")
    parser.add_argument("--dest-home", type=Path,
                        default=ROOT / "test_data" / ".saiverse",
                        help="複製先 SAIVERSE_HOME (省略時: test_data/.saiverse)")
    parser.add_argument("--force", action="store_true",
                        help="既存複製の上書き確認をスキップ")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    source_db = args.source_db if args.source_db is not None else _default_source_db()
    source_home = args.source_home if args.source_home is not None else _default_source_home()

    try:
        clone_persona(
            args.persona,
            source_db=source_db,
            source_home=source_home,
            dest_db=args.dest_db,
            dest_home=args.dest_home,
            force=args.force,
        )
    except CloneError as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
