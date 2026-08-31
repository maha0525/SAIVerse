"""SAIVerse バージョンアップグレードハンドラの登録モジュール。

各ハンドラは :class:`saiverse.upgrade.UpgradeHandler` の定義に従い、特定の
``from_version → to_version`` 遷移で1度だけ実行される。冪等性は基本的に
Phase 1 の機構（``current >= target`` で no-op）に頼っているため、各ハンドラ
は「状態の冪等性」（再実行で同じ状態になる）を満たす必要がある。

設計詳細: ``docs/intent/version_aware_world_and_persona.md``
"""
from __future__ import annotations

import datetime as _datetime
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Set

from saiverse.upgrade import UpgradeHandler

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from database.models import AI

LOGGER = logging.getLogger(__name__)


def _no_op_city_upgrade(*, session: "Session", city) -> None:
    """Explicit edge for releases that changed persona state but not City state."""
    del session, city


# ---- v0.3.0 第1号: dynamic_state captured_at リセット ----

def _v0_3_0_dynamic_state_reset(*, session: "Session", ai: "AI") -> None:
    """v0.3.0 で dynamic_state の Memopedia 判定がタイムスタンプベースに変わった
    ため、各ペルソナの ``PersonaBuildingState.LAST_NOTIFIED_JSON`` の
    ``captured_at`` を現在時刻にリセットし、旧形式の ``memopedia_pages`` を
    空配列化する。

    あわせて SAIMemory に「アップデート検知」通知を1件挿入してペルソナに
    アップデートがあったことを伝える。

    冪等性:
    - captured_at リセット: 何度走っても「現在時刻」が入るので状態として同じ
    - memopedia_pages: 既に空でも問題なし
    - SAIMemory 通知: Phase 1 の機構によりアップグレード時に1度だけ走る前提
      （テストで強制的に複数回走らせると通知が累積するが、これは許容範囲）
    """
    from database.models import PersonaBuildingState

    persona_id = ai.AIID
    LOGGER.info("[handler:v0_3_0_dynamic_state_reset] starting for persona=%s", persona_id)

    rows = session.query(PersonaBuildingState).filter_by(PERSONA_ID=persona_id).all()
    LOGGER.info(
        "[handler:v0_3_0_dynamic_state_reset] persona=%s: %d PersonaBuildingState row(s) to process",
        persona_id, len(rows),
    )

    now_ts = time.time()
    reset_count = 0
    skipped_count = 0
    for row in rows:
        if not row.LAST_NOTIFIED_JSON:
            skipped_count += 1
            continue
        try:
            data = json.loads(row.LAST_NOTIFIED_JSON)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"persona={persona_id} building={row.BUILDING_ID} has malformed LAST_NOTIFIED_JSON"
            ) from exc

        old_captured_at = data.get("captured_at")
        old_pages_count = len(data.get("memopedia_pages") or [])
        data["captured_at"] = now_ts
        data["memopedia_pages"] = []
        row.LAST_NOTIFIED_JSON = json.dumps(data, ensure_ascii=False)
        reset_count += 1
        LOGGER.debug(
            "[handler:v0_3_0_dynamic_state_reset] persona=%s building=%s: "
            "captured_at %r -> %s, memopedia_pages %d -> 0",
            persona_id, row.BUILDING_ID, old_captured_at, now_ts, old_pages_count,
        )

    LOGGER.info(
        "[handler:v0_3_0_dynamic_state_reset] persona=%s: reset=%d skipped=%d",
        persona_id, reset_count, skipped_count,
    )

    # SAIMemory にアップデート通知を挿入（失敗してもハンドラ全体は成功扱い：
    # 状態側のリセットは既に成功しているので）
    _insert_upgrade_notification(persona_id)


def _insert_upgrade_notification(persona_id: str) -> None:
    """ペルソナへアップデート検知通知を届ける (知覚バッファ経由・一度きり)。

    W14 (perception_buffer.md §10.6) で event_message 直挿しから知覚バッファ
    (kind='world_state') へ移送した。冪等は upgrade_id の二段照合:

    - legacy: 移送前に messages へ直挿しされた行 (metadata.upgrade_id)。
    - 台帳: 知覚バッファの全行 (未消費 + 消費済み — 消費済み行も残るので、
      消費を跨いでも「もう届けた」が台帳だけで分かる)。
    """
    import json as _json
    from saiverse_memory.adapter import SAIMemoryAdapter

    adapter = SAIMemoryAdapter(persona_id)

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    try:
        upgrade_id = f"v0_3_0_dynamic_state_reset:{persona_id}:0.3.0.dev0"
        with adapter._db_lock:
            existing = adapter.conn.execute(
                "SELECT 1 FROM messages WHERE json_extract(metadata, '$.upgrade_id') = ? LIMIT 1",
                (upgrade_id,),
            ).fetchone()
        if existing or adapter.has_perception_marker("upgrade_id", upgrade_id):
            LOGGER.info("[handler] upgrade notification already exists for %s", persona_id)
            return

        adapter.push_perception(
            "world_state",
            "SAIVerse v0.3.0 へのアップデートを検知しました。",
            metadata=_json.dumps(
                {"upgrade_id": upgrade_id, "system_event": "version_upgrade"},
                ensure_ascii=False,
            ),
        )
        LOGGER.info(
            "[handler] queued v0.3.0 upgrade notification into perception buffer for %s",
            persona_id,
        )
    finally:
        # 使い捨て adapter は閉じる (Windows では開いた conn が temp/backup の
        # ファイル操作を妨げる)。
        try:
            adapter.close()
        except Exception:
            pass


# ---- v0.3.0.dev1: 削除済み Playbook 名のスケジュール書き換え ----

# Phase 3 移行 (handoff_2026-05-08) で削除した meta_* / sub_router_user / basic_chat
# Playbook の名前。persona_schedule.META_PLAYBOOK にこれらが残っていると Pulse 起動時に
# Playbook not found で実行エラーになるため、起動時に track_user_conversation へ
# 巻き取る。
_DEPRECATED_PLAYBOOK_NAMES_V0_3_0_DEV1 = {
    "meta_user",
    "meta_user_manual",
    "basic_chat",
    "sub_router_user",
}

_LEGACY_PLAYBOOK_REPLACEMENT = "track_user_conversation"


def _v0_3_0_dev1_legacy_schedule_playbook_names(*, session: "Session", ai: "AI") -> None:
    """v0.3.0.dev1 で削除した meta_user / meta_user_manual / basic_chat / sub_router_user を
    persona_schedule.META_PLAYBOOK から track_user_conversation に書き換える。

    背景: Phase 3 で旧 meta_* Playbook を削除したため、既存スケジュールに古い名前が
    残っていると実行時に Playbook not found エラーになる (PersonaSchedule.META_PLAYBOOK
    は NOT NULL)。本ハンドラで AI 単位に自分のスケジュールを安全な値に巻き取る。

    UserSettings.SELECTED_META_PLAYBOOK は frontend (ToolModeSelector / page.tsx) が
    legacy 値を auto モードに collapse しているため実害がなく、本ハンドラでは触らない。

    冪等性: 削除済み名前を track_user_conversation に置換するだけ。すでに正常値が
    入っていれば触らない。何度走らせても同じ状態に収束する。

    副作用の局所化: filter で PERSONA_ID を絞るので、自ペルソナのスケジュールしか
    触らない (Intent 規約「副作用は局所化」の遵守)。
    """
    from database.models import PersonaSchedule

    persona_id = ai.AIID
    rows = (
        session.query(PersonaSchedule)
        .filter(
            PersonaSchedule.PERSONA_ID == persona_id,
            PersonaSchedule.META_PLAYBOOK.in_(_DEPRECATED_PLAYBOOK_NAMES_V0_3_0_DEV1),
        )
        .all()
    )

    if not rows:
        LOGGER.debug(
            "[handler:v0_3_0_dev1_legacy_schedule_playbook_names] persona=%s: "
            "no schedules with deprecated playbook names",
            persona_id,
        )
        return

    for row in rows:
        old_name = row.META_PLAYBOOK
        row.META_PLAYBOOK = _LEGACY_PLAYBOOK_REPLACEMENT
        LOGGER.info(
            "[handler:v0_3_0_dev1_legacy_schedule_playbook_names] persona=%s "
            "schedule_id=%s: %s -> %s",
            persona_id, row.SCHEDULE_ID, old_name, _LEGACY_PLAYBOOK_REPLACEMENT,
        )

    LOGGER.info(
        "[handler:v0_3_0_dev1_legacy_schedule_playbook_names] persona=%s: "
        "rewrote %d schedule(s) to %s",
        persona_id, len(rows), _LEGACY_PLAYBOOK_REPLACEMENT,
    )


# ---- v0.3.0.dev2: 旧 selected_playbook を pre_spells に変換 ----


def _v0_3_0_dev2_legacy_schedule_selected_playbook(*, session: "Session", ai: "AI") -> None:
    """旧 ``PLAYBOOK_PARAMS.selected_playbook`` を ``pre_spells`` 経路に変換する。

    背景: Phase 3 移行で ``meta_user_manual`` Playbook が削除された結果、
    旧スケジュールに残っている ``PLAYBOOK_PARAMS={"selected_playbook": "X"}``
    は実行時に解釈されない (旧 ``meta_user_manual`` の exec ノードでのみ使われ
    ていた)。Phase 3 B (handoff_2026-05-08) で ``pre_spells`` 機構が引数あり
    Spell の動的引数決定 (= ``spell_args_decider`` 経由) に対応したので、
    旧 ``selected_playbook`` を ``pre_spells: ["/spell name='X'"]`` (引数省略形)
    に書き換えれば、スケジュール起動時に Spell が自然に呼ばれる。

    変換規則:
    - ``PLAYBOOK_PARAMS.selected_playbook`` が文字列なら、``pre_spells`` リスト
      に ``"/spell name='<value>'"`` を追加 (既存の ``pre_spells`` があれば末尾追加)
    - ``selected_playbook`` キー自体は削除
    - ``selected_playbook`` が空文字 / 非文字列の場合はキーだけ削除 (no-op)

    冪等性: ``selected_playbook`` キーが無いレコードは触らない。何度走らせても
    同じ状態に収束する。

    副作用の局所化: ``PERSONA_ID`` で絞るので自ペルソナのスケジュールしか触らない。
    """
    from database.models import PersonaSchedule

    persona_id = ai.AIID
    rows = (
        session.query(PersonaSchedule)
        .filter(
            PersonaSchedule.PERSONA_ID == persona_id,
            PersonaSchedule.PLAYBOOK_PARAMS.isnot(None),
        )
        .all()
    )

    converted = 0
    for row in rows:
        params_raw = row.PLAYBOOK_PARAMS
        if not params_raw:
            continue
        try:
            params = json.loads(params_raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                f"persona={persona_id} schedule={row.SCHEDULE_ID} has malformed PLAYBOOK_PARAMS"
            ) from exc
        if not isinstance(params, dict):
            continue
        if "selected_playbook" not in params:
            continue

        selected = params.pop("selected_playbook")
        if isinstance(selected, str) and selected.strip():
            spell_entry = f"/spell name='{selected.strip()}'"
            existing_pre_spells = params.get("pre_spells")
            if isinstance(existing_pre_spells, list):
                existing_pre_spells.append(spell_entry)
            else:
                params["pre_spells"] = [spell_entry]
            LOGGER.info(
                "[handler:v0_3_0_dev2_legacy_schedule_selected_playbook] persona=%s "
                "schedule_id=%s: selected_playbook=%r -> pre_spells append %r",
                persona_id, row.SCHEDULE_ID, selected, spell_entry,
            )
        else:
            LOGGER.info(
                "[handler:v0_3_0_dev2_legacy_schedule_selected_playbook] persona=%s "
                "schedule_id=%s: dropping empty/invalid selected_playbook=%r",
                persona_id, row.SCHEDULE_ID, selected,
            )

        # Re-serialize. Empty dict → store empty JSON object so the column stays
        # parseable (vs None which suppresses params entirely).
        row.PLAYBOOK_PARAMS = json.dumps(params, ensure_ascii=False) if params else None
        converted += 1

    if converted:
        LOGGER.info(
            "[handler:v0_3_0_dev2_legacy_schedule_selected_playbook] persona=%s: "
            "rewrote selected_playbook -> pre_spells in %d schedule(s)",
            persona_id, converted,
        )
    else:
        LOGGER.debug(
            "[handler:v0_3_0_dev2_legacy_schedule_selected_playbook] persona=%s: "
            "no schedules with selected_playbook in PLAYBOOK_PARAMS",
            persona_id,
        )


# ---- v0.3.0.dev3: SPELL_ENABLED デフォルト ON 化 ----


def _v0_3_0_dev3_spell_enabled_default_on(*, session: "Session", ai: "AI") -> None:
    """Spell 機能が基幹機能化したため、既存ペルソナの ``AI.SPELL_ENABLED`` を
    ON に引き上げる。

    背景: Spell はもともと ``SPELL_ENABLED`` デフォルト OFF で導入されたが、
    判断ループ・Track 続行・native tool コール撲滅（prefix キャッシュ共用）の
    中核を担う基幹機能に育った。v0.3.0.dev3 でカラムデフォルトを True に変更した
    が、既存 DB の行には既に False が保存済みで、スキーマ差分が出ないため
    ``database/migrate.py`` の追加系/全書換どちらの経路でも書き換わらない。
    そこでバージョン認識アップグレードで既存ペルソナを一括 ON にする。

    「以前のバージョンから上げてきた場合も初期値 ON」というリリース方針に沿う。
    OFF を明示選択したペルソナと「触っていないだけの OFF」を DB 上で区別できない
    ため、本ハンドラは一律 True にする（= 移行時点の初期値を ON に揃える）。

    冪等性: True を代入するだけなので何度走らせても同じ状態に収束する。Phase 1 の
    機構により、通常は dev2→dev3 の遷移で1度だけ走る。

    副作用の局所化: ``ai`` 単体を触るだけで他ペルソナには影響しない。
    """
    persona_id = ai.AIID
    old_value = bool(ai.SPELL_ENABLED)
    ai.SPELL_ENABLED = True
    LOGGER.info(
        "[handler:v0_3_0_dev3_spell_enabled_default_on] persona=%s: "
        "SPELL_ENABLED %s -> True",
        persona_id, old_value,
    )


# ---- v0.3.0.dev4: v1 自律系 Playbook (track_autonomous 等) の退役巻き取り ----

# 概念再編⑥ P2c-3 (2026-07-10、④オートノミー整理「時間割へ完全移行」裁定) で
# 退役した v1 自律系 Playbook の名前。public JSON 除去 + playbook_sync の DB prune
# により実体が消えるため、参照が残っていると Pulse 起動時に Playbook not found に
# なる。詳細: docs/overview/v030_release_worklist.md ④ /
# docs/handoff/2026-07-10_memory_atlas_p2c_consumer_audit.md §3
_DEPRECATED_PLAYBOOK_NAMES_V0_3_0_DEV4 = {
    "track_autonomous",
    "meta_autonomy_decision",
}


def _v0_3_0_dev4_retired_autonomy_schedule_names(*, session: "Session", ai: "AI") -> None:
    """persona_schedule.META_PLAYBOOK の track_autonomous / meta_autonomy_decision を
    track_user_conversation に巻き取る (dev1 ハンドラと同じ巻き取りパターン)。

    冪等性: 退役名を置換するだけ。何度走らせても同じ状態に収束する。
    副作用の局所化: PERSONA_ID で絞るので自ペルソナのスケジュールしか触らない。
    """
    from database.models import PersonaSchedule

    persona_id = ai.AIID
    rows = (
        session.query(PersonaSchedule)
        .filter(
            PersonaSchedule.PERSONA_ID == persona_id,
            PersonaSchedule.META_PLAYBOOK.in_(_DEPRECATED_PLAYBOOK_NAMES_V0_3_0_DEV4),
        )
        .all()
    )
    if not rows:
        LOGGER.debug(
            "[handler:v0_3_0_dev4_retired_autonomy_schedule_names] persona=%s: "
            "no schedules with retired playbook names",
            persona_id,
        )
        return

    for row in rows:
        old_name = row.META_PLAYBOOK
        row.META_PLAYBOOK = _LEGACY_PLAYBOOK_REPLACEMENT
        LOGGER.info(
            "[handler:v0_3_0_dev4_retired_autonomy_schedule_names] persona=%s "
            "schedule_id=%s: %s -> %s",
            persona_id, row.SCHEDULE_ID, old_name, _LEGACY_PLAYBOOK_REPLACEMENT,
        )


def _v0_3_0_dev4_retired_autonomy_selected_playbook(*, session: "Session", city) -> None:
    """UserSettings.SELECTED_META_PLAYBOOK の退役名を NULL (= auto 既定) に巻き取る。

    背景: saiverse_manager が起動時に SELECTED_META_PLAYBOOK を
    ``state.current_playbook`` へ読み込む。退役 Playbook の名前が残っていると、
    prune 済みで実体の無い Playbook を指し続ける。NULL に戻せば読み込みが
    スキップされ、既定 (auto) で動く。

    dev1 ハンドラは「frontend が legacy 値を auto に collapse するため触らない」
    としたが、backend の state 読み込み (saiverse_manager) は collapse を通らない
    ため、退役分は DB 側で掃除する。

    スコープ: UserSettings はペルソナでなくユーザー単位のため city スコープに
    乗せる (複数 City 環境では複数回走るが、NULL 化は冪等)。
    """
    from database.models import UserSettings

    rows = (
        session.query(UserSettings)
        .filter(
            UserSettings.SELECTED_META_PLAYBOOK.in_(
                _DEPRECATED_PLAYBOOK_NAMES_V0_3_0_DEV4
            )
        )
        .all()
    )
    if not rows:
        LOGGER.debug(
            "[handler:v0_3_0_dev4_retired_autonomy_selected_playbook] "
            "no user settings with retired playbook names",
        )
        return

    for row in rows:
        old_name = row.SELECTED_META_PLAYBOOK
        row.SELECTED_META_PLAYBOOK = None
        LOGGER.info(
            "[handler:v0_3_0_dev4_retired_autonomy_selected_playbook] userid=%s: "
            "%s -> None (auto)",
            row.USERID, old_name,
        )


# ---- ハンドラ登録リスト ----

# 各ハンドラは to_version の昇順に書くと読みやすい（実行順は upgrade.py 側で
# select_handlers() がソートする）。
# ---- v0.3.0.dev5: 旧ファイル形式ログの自動取り込み ----

def _v0_3_0_dev5_building_log_import(*, session: "Session", city) -> None:
    """旧 log.json の取り込みは毎起動の検算が引き受けたので、ここでは何もしない。

    かつてはこのハンドラが取り込みを走らせていた。今は
    ``manager/initialization.py`` の ``_check_legacy_building_log_import`` が
    毎起動、漏れを見つけてその場で取り込む。同じ仕事を 2 箇所に置く理由が無く、
    ここに残すと弊害の方が大きい:

    - **確定の境界がずれる**: SQLite は最外周の SAVEPOINT を RELEASE した時点で
      確定する。このハンドラの SAVEPOINT が最外周になると、取り込みは枠組みの
      commit を待たずに確定し、後続ハンドラの失敗で巻き戻せなくなる
      (バージョンだけ刻まれない状態と食い違う。2026-08-16 Codex 指摘)。
    - **一度きり**: バージョンを刻んだ後は二度と走らない。漏れの再試行は
      毎起動の検算が持つ方が確実。

    エッジ自体は残す (0.3.0.dev4 → 0.3.0.dev5 の連なりを切らないため)。
    """
    del session, city


def _v0_3_0_dev5_conscious_log_cursor_import(*, session: "Session", ai: "AI") -> None:
    """旧 ``personas/<id>/conscious_log.json`` の pulse cursor を DB へ移管する。

    building log の取り込み (city ハンドラ) の後に走る前提 — run_startup_upgrade
    は City → AI の順で実行するため、この順序は枠組みが保証する。

    生きた cursor 行が既にある環境 (= DB 移行後も稼働してきた環境) では、
    古いファイルのリマップ値で上書きしないよう何もしない。
    """
    from saiverse.data_paths import get_saiverse_home
    from saiverse.legacy_log_import import CursorStats, migrate_persona_cursors

    persona_dir = get_saiverse_home() / "personas" / ai.AIID
    if not persona_dir.is_dir():
        return
    stats = CursorStats()
    try:
        # SAVEPOINT で自分の書き込みだけを囲う (city ハンドラと同じ理由)。
        with session.begin_nested():
            migrate_persona_cursors(
                session, persona_dir, stats, dry_run=False,
                only_if_no_existing_rows=True,
            )
    except Exception:
        # 起動を止めない。cursor が欠けた場合の実害は「過去メッセージの再消化」で、
        # 履歴の喪失ではない。
        LOGGER.error(
            "[upgrade] cursor import for ai=%s failed — continuing",
            ai.AIID, exc_info=True,
        )
        return
    LOGGER.info(
        "[upgrade] cursor import for ai=%s: inserted=%d skipped(existing_rows=%d, missing=%d)",
        ai.AIID, stats.cursors_inserted,
        stats.personas_skipped_existing_rows, stats.personas_skipped_missing,
    )


# ---- v0.3.0.dev6: Playbook 全置き換え (v0.2 由来行の永久残留対策) ----


def _no_op_ai_upgrade(*, session: "Session", ai: "AI") -> None:
    """Explicit edge for releases that changed City/global state but not AI state."""
    del session, ai


def _serialize_playbook_row(row) -> dict:
    """Playbook ORM 行の全カラムを JSON 化可能な dict に写す。"""
    from database.models import Playbook

    data = {}
    for column in Playbook.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, (_datetime.datetime, _datetime.date)):
            value = value.isoformat()
        data[column.name] = value
    return data


def _collect_current_playbook_names_strict() -> Set[str]:
    """現行のファイル Playbook 名集合を、読み込み失敗ゼロを条件に取得する。

    全置き換えは一回きりの破壊操作なので、壊れたファイルを黙ってスキップする
    毎起動 sync の寛容さをここでは使わない — 一部のファイルが読めない状態で
    名前集合を「現行の全量」として削除に使うと、壊れていた分の Playbook と
    その permission 行が欠けたまま確定し、以後の sync でも復元されないため。
    """
    from saiverse.playbook_sync import _collect_file_playbooks

    collect_errors: List = []
    names = set(_collect_file_playbooks(collect_errors=collect_errors).keys())
    if collect_errors:
        details = "; ".join(f"{path}: {reason}" for path, reason in collect_errors)
        raise RuntimeError(
            "playbook wholesale replacement aborted: failed to read "
            f"{len(collect_errors)} playbook file(s), so the collected name set "
            f"is incomplete and cannot be used for deletion — {details}"
        )
    return names


def replace_all_playbooks(
    session: "Session",
    *,
    current_playbook_names: Optional[Set[str]] = None,
    backup_dir: Optional[Path] = None,
) -> None:
    """playbooks テーブルをバックアップしてから全行削除する (v0.2 → v0.3 移行)。

    背景: v0.2 時代の Playbook 取り込みは source_file / source_hash を記録しな
    かったため、v0.3 の起動時同期 (saiverse.playbook_sync) がそれらを
    「save_playbook 由来 = ユーザー作 = 保護対象」と誤認し、退役済み Playbook
    (meta_user 等 37 本) がアップグレード後の DB に永久残留する。ペルソナが
    Playbook を作る経路はリリース版にも現行にも存在しない (2026-08-29 まはー
    確認済み) ため、DB の全 Playbook 行は機械取り込み由来とみなし、選別せず
    全置き換えする (2026-08-29 まはー裁定)。

    実行順序の前提: 本処理は main.py の run_startup_upgrade (main.py:458) の中で
    走り、同じ起動の sync_playbooks_from_files (main.py:491) より必ず前に来る。
    削除された現行 Playbook は直後の sync が再取り込みする。

    手順 (fail-closed):
    1. 全行読み出し。0 行のときは、playbook_permission にも行が無ければ完全
       no-op (新規インストールでファイル収集を走らせない)。permission 行だけ
       残っている DB では、現行ファイル名集合に無い退役名の行を掃除する
       (playbooks の削除が無いのでバックアップは作らない)。
    2. 現行のファイル Playbook 名を収集。読み込みに失敗したファイルが 1 件でも
       あれば例外で止める (集合が不完全なまま削除すると、壊れていた分が欠けて
       確定するため)。0 件でも例外で止める — sync が何も再取り込みできない
       環境で全削除すると Playbook ゼロの世界になるため。
    3. 全行の全カラムを JSON でバックアップし、読み戻して行数を検算。
       書き込み失敗・検算失敗は例外で止め、削除には進まない。
    4. playbooks 全行 DELETE。
    5. playbook_permission から、現行ファイル名集合に無い playbook_name の行を
       DELETE (退役名の許可行の掃除。現行名は名前参照なので再取り込み後も効く)。

    commit はしない — 呼び出し元 (upgrade 枠組み) が LAST_KNOWN_VERSION の更新と
    まとめて commit することで、削除とバージョン刻印が原子的になる。

    Args:
        session: upgrade 枠組みが渡す SQLAlchemy セッション
        current_playbook_names: テスト注入用。None なら playbook_sync の
            _collect_file_playbooks() から取得
        backup_dir: テスト注入用。None なら <saiverse_home>/backups/playbooks
    """
    from database.models import Playbook, PlaybookPermission

    rows = session.query(Playbook).all()
    if not rows:
        perm_rows_exist = session.query(PlaybookPermission).first() is not None
        if not perm_rows_exist:
            LOGGER.info(
                "[playbook_replacement] playbooks and playbook_permission are "
                "both empty — nothing to replace"
            )
            return
        # playbooks は空だが permission に行が残っている DB (例: 過去の手動
        # 削除)。退役名の孤児 permission 行だけ掃除する。
        if current_playbook_names is None:
            current_playbook_names = _collect_current_playbook_names_strict()
        if not current_playbook_names:
            # 本線 (下) と同じ検査。名前集合が空のまま ~IN () を流すと全許可行が
            # 消える — ファイルが一つも見つからない環境では掃除も見送る。
            raise RuntimeError(
                "playbook wholesale replacement aborted: no file-based playbooks "
                "found on disk; refusing to treat every playbook_permission row "
                "as orphaned."
            )
        deleted_perms = (
            session.query(PlaybookPermission)
            .filter(~PlaybookPermission.playbook_name.in_(current_playbook_names))
            .delete(synchronize_session=False)
        )
        LOGGER.info(
            "[playbook_replacement] playbooks table is empty — deleted %d "
            "orphan playbook_permission row(s) not matching any current file "
            "playbook (current file playbooks: %d)",
            deleted_perms, len(current_playbook_names),
        )
        return

    if current_playbook_names is None:
        current_playbook_names = _collect_current_playbook_names_strict()

    if not current_playbook_names:
        raise RuntimeError(
            "playbook wholesale replacement aborted: no file-based playbooks found "
            "on disk. Deleting all DB playbooks now would leave the world with zero "
            "playbooks after startup sync."
        )

    if backup_dir is None:
        from saiverse.data_paths import get_saiverse_home

        backup_dir = get_saiverse_home() / "backups" / "playbooks"

    serialized = [_serialize_playbook_row(row) for row in rows]
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = Path(backup_dir) / f"playbooks_pre_v030_{timestamp}.json"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(
        json.dumps(serialized, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 読み戻し検算 — 破損したバックアップを頼りに削除へ進まない
    restored = json.loads(backup_path.read_text(encoding="utf-8"))
    if len(restored) != len(rows):
        raise RuntimeError(
            f"playbook backup verification failed: wrote {len(rows)} rows "
            f"but read back {len(restored)} ({backup_path})"
        )

    deleted_playbooks = session.query(Playbook).delete(synchronize_session=False)
    deleted_perms = (
        session.query(PlaybookPermission)
        .filter(~PlaybookPermission.playbook_name.in_(current_playbook_names))
        .delete(synchronize_session=False)
    )

    LOGGER.info(
        "[playbook_replacement] backed up %d playbook row(s) to %s; "
        "deleted %d playbooks row(s) and %d retired playbook_permission row(s) "
        "(current file playbooks: %d — re-imported by startup sync)",
        len(serialized), backup_path, deleted_playbooks, deleted_perms,
        len(current_playbook_names),
    )


def _v0_3_0_dev6_playbook_wholesale_replacement(*, session: "Session", city) -> None:
    """v0.3.0.dev6 City エッジ: Playbook 全置き換え。

    playbooks テーブルは City 単位でなく DB 全体 (public scope) のため、多 City
    DB では 2 巡目以降が「0 行 no-op」に落ちる (冪等)。
    """
    del city
    replace_all_playbooks(session)


HANDLERS: List[UpgradeHandler] = [
    UpgradeHandler(
        name="city_noop_v0_3_0_dev0",
        scope="city",
        from_version="0.0.0",
        to_version="0.3.0.dev0",
        run=_no_op_city_upgrade,
        description="Explicit no-op City edge for the v0.3.0.dev0 persona migration.",
    ),
    UpgradeHandler(
        name="city_noop_v0_3_0_dev1",
        scope="city",
        from_version="0.3.0.dev0",
        to_version="0.3.0.dev1",
        run=_no_op_city_upgrade,
        description="Explicit no-op City edge for v0.3.0.dev1.",
    ),
    UpgradeHandler(
        name="city_noop_v0_3_0_dev2",
        scope="city",
        from_version="0.3.0.dev1",
        to_version="0.3.0.dev2",
        run=_no_op_city_upgrade,
        description="Explicit no-op City edge for v0.3.0.dev2.",
    ),
    UpgradeHandler(
        name="city_noop_v0_3_0_dev3",
        scope="city",
        from_version="0.3.0.dev2",
        to_version="0.3.0.dev3",
        run=_no_op_city_upgrade,
        description="Explicit no-op City edge for v0.3.0.dev3.",
    ),
    UpgradeHandler(
        name="v0_3_0_dynamic_state_reset",
        scope="ai",
        from_version="0.0.0",
        # dev サフィックスを使うことで 0.2.x → 0.3.0.dev0 への遷移でも走り、
        # 0.3.0.dev0 → 0.3.0 (release) では走らない（既に走り済みのため）。
        to_version="0.3.0.dev0",
        run=_v0_3_0_dynamic_state_reset,
        description=(
            "Reset dynamic_state captured_at for all PersonaBuildingState rows "
            "and clear legacy memopedia_pages snapshot. Notify the persona via "
            "SAIMemory."
        ),
    ),
    UpgradeHandler(
        name="v0_3_0_dev1_legacy_schedule_playbook_names",
        scope="ai",
        from_version="0.3.0.dev0",
        to_version="0.3.0.dev1",
        run=_v0_3_0_dev1_legacy_schedule_playbook_names,
        description=(
            "Rewrite deprecated meta_user / meta_user_manual / basic_chat / "
            "sub_router_user references in persona_schedule.META_PLAYBOOK to "
            "track_user_conversation, so existing schedules don't error out "
            "after the Phase 3 playbook removal."
        ),
    ),
    UpgradeHandler(
        name="v0_3_0_dev2_legacy_schedule_selected_playbook",
        scope="ai",
        from_version="0.3.0.dev1",
        to_version="0.3.0.dev2",
        run=_v0_3_0_dev2_legacy_schedule_selected_playbook,
        description=(
            "Convert legacy PLAYBOOK_PARAMS.selected_playbook (orphaned by "
            "meta_user_manual removal) to the new pre_spells format "
            "(/spell name='X'). The runtime resolves the missing args via "
            "spell_args_decider Playbook at execution time."
        ),
    ),
    UpgradeHandler(
        name="v0_3_0_dev3_spell_enabled_default_on",
        scope="ai",
        from_version="0.3.0.dev2",
        to_version="0.3.0.dev3",
        run=_v0_3_0_dev3_spell_enabled_default_on,
        description=(
            "Enable AI.SPELL_ENABLED for all existing personas. Spell has become "
            "a core feature; the column default flips to True in v0.3.0.dev3, but "
            "existing rows already hold False and produce no schema diff, so this "
            "handler brings migrated personas up to the new ON default."
        ),
    ),
    UpgradeHandler(
        name="v0_3_0_dev4_retired_autonomy_selected_playbook",
        scope="city",
        from_version="0.3.0.dev3",
        to_version="0.3.0.dev4",
        run=_v0_3_0_dev4_retired_autonomy_selected_playbook,
        description=(
            "Reset UserSettings.SELECTED_META_PLAYBOOK to NULL (= auto) where it "
            "still names the retired v1 autonomy playbooks (track_autonomous / "
            "meta_autonomy_decision), removed in P2c-3 after the timetable "
            "migration verdict."
        ),
    ),
    UpgradeHandler(
        name="v0_3_0_dev4_retired_autonomy_schedule_names",
        scope="ai",
        from_version="0.3.0.dev3",
        to_version="0.3.0.dev4",
        run=_v0_3_0_dev4_retired_autonomy_schedule_names,
        description=(
            "Rewrite retired track_autonomous / meta_autonomy_decision references "
            "in persona_schedule.META_PLAYBOOK to track_user_conversation, so "
            "existing schedules don't error out after the v1 autonomy playbook "
            "retirement (P2c-3)."
        ),
    ),
    UpgradeHandler(
        name="v0_3_0_dev5_building_log_import",
        scope="city",
        from_version="0.3.0.dev4",
        to_version="0.3.0.dev5",
        run=_v0_3_0_dev5_building_log_import,
        description=(
            "No-op City edge for v0.3.0.dev5. The legacy log.json import moved to "
            "the every-startup reconciliation in manager/initialization.py, which "
            "retries on each boot and keeps the transaction boundary out of the "
            "upgrade framework's SAVEPOINT (SQLite releases an outermost SAVEPOINT "
            "as a commit)."
        ),
    ),
    UpgradeHandler(
        name="v0_3_0_dev5_conscious_log_cursor_import",
        scope="ai",
        from_version="0.3.0.dev4",
        to_version="0.3.0.dev5",
        run=_v0_3_0_dev5_conscious_log_cursor_import,
        description=(
            "Import legacy personas/<id>/conscious_log.json pulse cursors into "
            "persona_pulse_cursor. Runs after the city-scope building log import "
            "(framework runs City handlers before AI handlers), because cursor "
            "remapping resolves through building_messages.legacy_seq. No-op when "
            "the persona already has live cursor rows."
        ),
    ),
    UpgradeHandler(
        name="v0_3_0_dev6_playbook_wholesale_replacement",
        scope="city",
        from_version="0.3.0.dev5",
        to_version="0.3.0.dev6",
        run=_v0_3_0_dev6_playbook_wholesale_replacement,
        description=(
            "Back up every playbooks row to <saiverse_home>/backups/playbooks/ "
            "and delete them all, plus playbook_permission rows whose name no "
            "longer matches a file-based playbook. v0.2 imports recorded no "
            "source_file/source_hash, so the v0.3 startup sync would treat "
            "retired playbooks (meta_user etc.) as user-created and keep them "
            "forever. Runs before sync_playbooks_from_files (main.py runs "
            "run_startup_upgrade first), which then re-imports the current set."
        ),
    ),
    UpgradeHandler(
        name="ai_noop_v0_3_0_dev6",
        scope="ai",
        from_version="0.3.0.dev5",
        to_version="0.3.0.dev6",
        run=_no_op_ai_upgrade,
        description=(
            "Explicit no-op AI edge for v0.3.0.dev6 (playbook replacement is "
            "DB-global and runs on the City scope)."
        ),
    ),
    # ---- v0.3.0: リリースの空エッジ (2026-09-01) ----
    # dev6 と 0.3.0 の間にデータ移行は無いが、鎖にエッジが無いと dev6 の世界が
    # 0.3.0 起動時に version chain エラーで止まる (リリース必須作業 —
    # docs/handoff/2026-08-30_release_sweep_checklist.md §6)。
    UpgradeHandler(
        name="city_noop_v0_3_0_release",
        scope="city",
        from_version="0.3.0.dev6",
        to_version="0.3.0",
        run=_no_op_city_upgrade,
        description="Empty release edge dev6 -> 0.3.0 (no data migration).",
    ),
    UpgradeHandler(
        name="ai_noop_v0_3_0_release",
        scope="ai",
        from_version="0.3.0.dev6",
        to_version="0.3.0",
        run=_no_op_ai_upgrade,
        description="Empty release edge dev6 -> 0.3.0 (no data migration).",
    ),
]
