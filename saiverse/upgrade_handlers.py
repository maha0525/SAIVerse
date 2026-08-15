"""SAIVerse バージョンアップグレードハンドラの登録モジュール。

各ハンドラは :class:`saiverse.upgrade.UpgradeHandler` の定義に従い、特定の
``from_version → to_version`` 遷移で1度だけ実行される。冪等性は基本的に
Phase 1 の機構（``current >= target`` で no-op）に頼っているため、各ハンドラ
は「状態の冪等性」（再実行で同じ状態になる）を満たす必要がある。

設計詳細: ``docs/intent/version_aware_world_and_persona.md``
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, List

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
    """ペルソナの SAIMemory にアップデート検知通知を1件挿入する。"""
    from saiverse_memory.adapter import SAIMemoryAdapter

    adapter = SAIMemoryAdapter(persona_id)

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    upgrade_id = f"v0_3_0_dynamic_state_reset:{persona_id}:0.3.0.dev0"
    with adapter._db_lock:
        existing = adapter.conn.execute(
            "SELECT 1 FROM messages WHERE json_extract(metadata, '$.upgrade_id') = ? LIMIT 1",
            (upgrade_id,),
        ).fetchone()
    if existing:
        LOGGER.info("[handler] upgrade notification already exists for %s", persona_id)
        return

    # `event_message` タグがペルソナの会話コンテキストに取り込まれるキー
    # (sea/runtime_context.py の required_tags 参照)。dynamic_state.py の
    # 既存イベント通知と同じ扱いにする。`system_event` / `version_upgrade` は
    # 後から検索/フィルタするための識別子。
    message = {
        "role": "user",
        "content": (
            "<system>[システム通知]\n"
            "- SAIVerse v0.3.0 へのアップデートを検知しました。"
            "Memopediaの状態同期がリセットされました</system>"
        ),
        "metadata": {
            "tags": ["internal", "event_message", "system_event", "version_upgrade"],
            "upgrade_id": upgrade_id,
        },
    }
    message_id = adapter.append_persona_message(message)
    if not message_id:
        raise RuntimeError(f"Failed to insert upgrade notification for {persona_id}")
    LOGGER.info(
        "[handler] inserted v0.3.0 upgrade notification into SAIMemory for %s",
        persona_id,
    )


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
    """旧 ``cities/<slug>/buildings/<bid>/log.json`` を building_messages へ取り込む。

    リリース版 (v0.2.x, log.json 時代) から上がってきた環境で、世界が動き出す前に
    過去の部屋ログを DB へ移す。手動スクリプト前提だった移行を自動化する —
    走らなければ全部屋の過去ログがユーザーから見えなくなるため。

    冪等: 取り込み痕跡 (legacy_seq 付き行) のある部屋は skip。取り込めなかった
    部屋は起動時の検算 (manager/initialization.py) がアラートにするので、
    ここでは黙って続行してよい。commit は Phase 1 機構がエンティティ単位で行う。
    """
    from saiverse.data_paths import get_saiverse_home
    from saiverse.legacy_log_import import import_building_logs

    try:
        # SAVEPOINT で自分の書き込みだけを囲う。session.rollback() にすると、
        # 同一トランザクションに載っている前段ハンドラ (dev0〜dev4) の未コミット
        # 変更まで巻き戻した上にバージョンだけ刻まれ、前段の処理が失われる。
        with session.begin_nested():
            stats = import_building_logs(
                session, get_saiverse_home(), city_filter=city.CITY_SLUG,
            )
    except Exception:
        # 例外を上へ返すと起動そのものが中断される (framework は handler 失敗 =
        # abort)。取り込みの失敗でユーザーを起動不能にはしない — この取り込み分
        # だけ巻き戻して続行し、取り込めなかった部屋は起動時の検算
        # (manager/initialization.py) が毎起動アラートにする。
        LOGGER.error(
            "[upgrade] building log import for city=%s failed — continuing; "
            "deficits will surface via the startup reconciliation alerts",
            city.CITY_SLUG, exc_info=True,
        )
        return
    LOGGER.info(
        "[upgrade] building log import for city=%s: scanned=%d inserted=%d "
        "skipped(already=%d, live_rows=%d, unreadable=%d)",
        city.CITY_SLUG, stats.buildings_scanned, stats.messages_inserted,
        stats.buildings_skipped_already_migrated,
        stats.buildings_skipped_live_rows,
        stats.buildings_skipped_unreadable,
    )


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
            "Import legacy cities/<slug>/buildings/<bid>/log.json files into the "
            "building_messages table. Automates the previously manual "
            "migrate_building_logs_to_db.py so released v0.2.x users don't lose "
            "room history when the read path switched to the DB (Phase 2+3). "
            "Skips by current-file readability only — never by leftover "
            "log.json.corrupted_* markers (the Testarossa room lesson)."
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
]
