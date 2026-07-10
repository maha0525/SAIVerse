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
            # データ破損ケース。スキップして他を続ける（このハンドラ単体は失敗扱いにしない）
            LOGGER.warning(
                "[handler:v0_3_0_dynamic_state_reset] persona=%s building=%s: "
                "malformed LAST_NOTIFIED_JSON, skipping: %s",
                persona_id, row.BUILDING_ID, exc,
            )
            skipped_count += 1
            continue

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
    try:
        from saiverse_memory.adapter import SAIMemoryAdapter
    except ImportError as exc:
        LOGGER.warning(
            "[handler] cannot import SAIMemoryAdapter, skipping notification for %s: %s",
            persona_id, exc, exc_info=True,
        )
        return

    try:
        adapter = SAIMemoryAdapter(persona_id)
    except Exception as exc:
        LOGGER.warning(
            "[handler] failed to initialise SAIMemory adapter for %s, "
            "skipping notification: %s",
            persona_id, exc, exc_info=True,
        )
        return

    if not adapter.is_ready():
        LOGGER.warning(
            "[handler] SAIMemory not ready for %s, skipping notification",
            persona_id,
        )
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
        },
    }
    try:
        adapter.append_persona_message(message)
        LOGGER.info(
            "[handler] inserted v0.3.0 upgrade notification into SAIMemory for %s",
            persona_id,
        )
    except Exception as exc:
        LOGGER.warning(
            "[handler] failed to insert notification for %s: %s",
            persona_id, exc, exc_info=True,
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
            LOGGER.warning(
                "[handler:v0_3_0_dev2_legacy_schedule_selected_playbook] persona=%s "
                "schedule_id=%s: malformed PLAYBOOK_PARAMS, skipping: %s",
                persona_id, row.SCHEDULE_ID, exc,
            )
            continue
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
HANDLERS: List[UpgradeHandler] = [
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
]
