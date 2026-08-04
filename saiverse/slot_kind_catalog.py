"""コマ種別カタログ (時間割改修 T1、timetable_redesign.md §5.5)。

時間割のコマ種別 (kind) は固定の列挙ではなく**カタログ**である — 種別は環境
(アドオン・スペル・施設) に依存して増減するため、SAIVerse の資源 3 層優先に
そのまま乗せる:

    1. ~/.saiverse/user_data/slot_kinds/  (最優先)
    2. expansion_data/<addon>/slot_kinds/ (中間)
    3. builtin_data/slot_kinds/           (最下位)

種別定義スキーマ (intent §5.5 の項目立て。§11-9 のとおり仮確定):

    {
      "id": "英数字 ID (ファイル名の stem と一致させる)",
      "name": "コマ名 (日本語。時間割の kind 値そのもの)",
      "execution_type": "work_session" | "outing" | "stay_home" | "free_choice",
      "description": "ペルソナ / ユーザー向けの一行説明",
      "instruction_template": "作業セッションの指示書 (work_session のみ必須。{note} と {target} を展開)"
    }

- ``execution_type`` が未知の値の定義は読み込み時に拒否する (WARN + skip)。
- id / name の重複は**先勝ち** (優先層が勝つ) — provider_configs と同じ流儀。
- builtin_data 由来の定義には ``builtin: True`` が自動で付く。
- カスタム種別 (user_data / アドオン) の安全性は**作成者が持つ** (まはー裁定
  2026-08-01)。本体はここで書式だけを検証し、中身 (創発×後続依存を含む) の
  安全原則は builtin セットのみ保証する。

kind 語彙の消費側 (時間割の検証・ハンドラ登録・指示書テンプレート) は
``saiverse.day_plan`` がこのカタログから組み立てる。カタログを reload したら
``day_plan.reload_kind_vocabulary()`` も呼ぶこと (語彙キャッシュの再構築)。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .data_paths import (
    BUILTIN_DATA_DIR,
    EXPANSION_DATA_DIR,
    SLOT_KINDS_DIR,
    USER_DATA_DIR,
)

LOGGER = logging.getLogger(__name__)

_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]+$")

# 実行の型 (intent §5.5)。ハンドラの配線 (day_plan) がこの値で分岐する。
EXECUTION_WORK_SESSION = "work_session"  # 作業セッション運転 (予算ゲート対象)
EXECUTION_OUTING = "outing"              # 出かける (T1 では honest stub)
EXECUTION_STAY_HOME = "stay_home"        # 自室で過ごす (T1 では honest stub)
EXECUTION_FREE_CHOICE = "free_choice"    # 自由時間 = 種別の穴の名前付き版 (T1 では stub)

EXECUTION_TYPES = (
    EXECUTION_WORK_SESSION,
    EXECUTION_OUTING,
    EXECUTION_STAY_HOME,
    EXECUTION_FREE_CHOICE,
)


def _is_builtin_path(path: Path) -> bool:
    """Return True if path is under builtin_data/."""
    try:
        path.resolve().relative_to(BUILTIN_DATA_DIR.resolve())
        return True
    except ValueError:
        return False


def _iter_catalog_files():
    """カタログ JSON を優先順 (user > expansion > builtin) で全件列挙する。

    ``data_paths.iter_files`` と同じ 3 層走査だが、**各層の中をファイル名で
    ソート**する — カタログの並び順はそのまま kind 語彙 (時間割 enum) の並び順に
    なるため、決定論でなければ head / response_schema の文字列が起動ごとに揺れる
    (prefix キャッシュ破壊)。builtin のファイル名は数字プレフィクス
    (``10_research.json`` 等) で意図した提示順を固定している。

    同名ファイルの層間シャドーイング (上位が下位を隠す) はここではやらない —
    隠してよいのは**正常に検証できた**上位定義だけ (壊れた上位ファイルが
    builtin の正常定義まで消さないため。Codex 三巡目)。判定は
    :func:`load_catalog` 側が検証後に行う。
    """
    user_path = USER_DATA_DIR / SLOT_KINDS_DIR
    if user_path.exists():
        for file_path in sorted(user_path.glob("*.json")):
            if file_path.is_file():
                yield file_path

    if EXPANSION_DATA_DIR.exists():
        for project_dir in sorted(EXPANSION_DATA_DIR.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith(("_", ".")):
                continue
            exp_path = project_dir / SLOT_KINDS_DIR
            if exp_path.exists():
                for file_path in sorted(exp_path.glob("*.json")):
                    if file_path.is_file():
                        yield file_path

    builtin_path = BUILTIN_DATA_DIR / SLOT_KINDS_DIR
    if builtin_path.exists():
        for file_path in sorted(builtin_path.glob("*.json")):
            if file_path.is_file():
                yield file_path


def _validate_definition(data: dict, source_name: str) -> str | None:
    """種別定義の書式検証。不正なら理由の文字列、正当なら None を返す。"""
    kind_id = data.get("id")
    if not isinstance(kind_id, str) or not _SAFE_ID_PATTERN.match(kind_id or ""):
        return f"invalid 'id': {kind_id!r}"
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return f"invalid 'name': {name!r}"
    execution_type = data.get("execution_type")
    if execution_type not in EXECUTION_TYPES:
        return (
            f"invalid 'execution_type': {execution_type!r} "
            f"(expected one of {EXECUTION_TYPES})"
        )
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        return f"invalid 'description': {description!r}"
    template = data.get("instruction_template")
    if execution_type == EXECUTION_WORK_SESSION:
        if not isinstance(template, str) or not template.strip():
            return "'instruction_template' is required for execution_type='work_session'"
    elif template is not None and not isinstance(template, str):
        return f"invalid 'instruction_template': {template!r}"
    return None


def load_catalog() -> dict[str, dict]:
    """コマ種別カタログを全層から読み込む (id → 定義 dict)。

    - 同名ファイルの層間シャドーイング (user > expansion > builtin) は
      **検証に通った定義だけ**が主張できる — 壊れた上位ファイルは下位の正常な
      同名定義へフォールバックする (Codex 三巡目)。
    - id / name の重複は先勝ち (優先層・ファイル名昇順の先に読まれた方が勝つ)。
    - 不正定義は WARN + skip (1 枚の壊れた定義でカタログ全体を落とさない)。
    - builtin_data 由来の定義には ``builtin: True`` を付ける。
    - 戻り値の挿入順が語彙の提示順 (決定論)。
    """
    catalog: dict[str, dict] = {}
    seen_names: set[str] = set()
    claimed_filenames: set[str] = set()

    for config_file in _iter_catalog_files():
        if config_file.name in claimed_filenames:
            LOGGER.debug(
                "Slot kind file %s shadowed by a valid higher-layer definition",
                config_file,
            )
            continue
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning(
                "Failed to load slot kind definition from %s: %s",
                config_file.name, exc,
            )
            continue
        if not isinstance(data, dict):
            LOGGER.warning(
                "Slot kind definition %s is not a JSON object; skipping",
                config_file.name,
            )
            continue

        reason = _validate_definition(data, config_file.name)
        if reason is not None:
            LOGGER.warning(
                "Slot kind definition %s rejected: %s", config_file.name, reason,
            )
            continue

        kind_id = data["id"]
        name = data["name"].strip()
        if kind_id in catalog:
            LOGGER.debug(
                "Slot kind id %r already loaded; skipping %s (first-win)",
                kind_id, config_file.name,
            )
            continue
        if name in seen_names:
            LOGGER.warning(
                "Slot kind name %r already loaded; skipping %s (first-win)",
                name, config_file.name,
            )
            continue

        data = dict(data)
        data["name"] = name
        if _is_builtin_path(config_file):
            data["builtin"] = True

        catalog[kind_id] = data
        seen_names.add(name)
        # ファイル名の縄張り (下位層の同名を隠す権利) は**実際にカタログへ
        # 採用された**定義だけが主張できる — 構造は正当でも id/name 重複で
        # 読み飛ばした上位定義に主張させると、下位の正常な同名ファイルまで
        # 消える (Codex 四巡目 #4)。
        claimed_filenames.add(config_file.name)
        LOGGER.debug("Loaded slot kind: %s (%s) from %s", kind_id, name, config_file)

    LOGGER.info("Loaded %d slot kind definitions", len(catalog))
    return catalog


SLOT_KIND_CATALOG: dict[str, dict] = load_catalog()


def reload_catalog() -> dict[str, dict]:
    """カタログをディスクから読み直してグローバルキャッシュを更新する。

    語彙の消費側キャッシュは別持ちなので、続けて
    ``saiverse.day_plan.reload_kind_vocabulary()`` を呼ぶこと。
    """
    global SLOT_KIND_CATALOG
    SLOT_KIND_CATALOG = load_catalog()
    LOGGER.info(
        "Slot kind catalog reloaded: %d definitions", len(SLOT_KIND_CATALOG),
    )
    return SLOT_KIND_CATALOG


def get_kind(kind_id: str) -> dict | None:
    """種別定義を id で引く。無ければ None。"""
    return SLOT_KIND_CATALOG.get(kind_id)


def get_kind_by_name(name: str) -> dict | None:
    """種別定義をコマ名 (時間割の kind 値) で引く。無ければ None。"""
    for definition in SLOT_KIND_CATALOG.values():
        if definition.get("name") == name:
            return definition
    return None


def kind_names() -> tuple[str, ...]:
    """全種別のコマ名 (カタログ順 = 決定論の提示順)。"""
    return tuple(d["name"] for d in SLOT_KIND_CATALOG.values())


def kind_names_for_execution(execution_type: str) -> tuple[str, ...]:
    """指定の実行の型を持つ種別のコマ名 (カタログ順)。"""
    return tuple(
        d["name"] for d in SLOT_KIND_CATALOG.values()
        if d.get("execution_type") == execution_type
    )


def instruction_templates() -> dict[str, str]:
    """work_session 種別のコマ名 → 指示書テンプレート。"""
    return {
        d["name"]: d["instruction_template"]
        for d in SLOT_KIND_CATALOG.values()
        if d.get("execution_type") == EXECUTION_WORK_SESSION
    }


def is_builtin(kind_id: str) -> bool:
    """有効な定義が builtin_data 由来なら True (override があれば False)。"""
    definition = SLOT_KIND_CATALOG.get(kind_id)
    if definition is None:
        return False
    return bool(definition.get("builtin"))


__all__ = [
    "EXECUTION_WORK_SESSION",
    "EXECUTION_OUTING",
    "EXECUTION_STAY_HOME",
    "EXECUTION_FREE_CHOICE",
    "EXECUTION_TYPES",
    "SLOT_KIND_CATALOG",
    "load_catalog",
    "reload_catalog",
    "get_kind",
    "get_kind_by_name",
    "kind_names",
    "kind_names_for_execution",
    "instruction_templates",
    "is_builtin",
]
