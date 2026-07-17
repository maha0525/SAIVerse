"""Dynamic State Sync — A/B/C 状態モデルによる Building 状態管理 (Phase 3 で head_pipeline へ統合)。

このモジュールは旧 SAIVerse の `DynamicStateManager`。Building 内のアイテム/居住者/
Memopedia/Chronicle の差分通知を担当していたが、Phase 3-e で実装本体が
`sea.head_pipeline.sections` の 4 Section + `sea.head_pipeline.integration.inject_diff_notifications`
に統合された。

本ファイルは互換のための **facade** を提供する:
  - `maybe_inject_event_messages` → head_pipeline 経由で diff 通知
  - `on_building_entered` → BUILDING_ENTERED イベントを head_pipeline に dispatch
  - `on_metabolism` → METABOLISM イベントを head_pipeline に dispatch

旧 `PersonaBuildingState` テーブルは saiverse.upgrade_handlers が触る経路が残っている
ため、モデル定義 (`database.models.PersonaBuildingState`) はしばらく残す。新しい
read/write はすべて `session_head_snapshot` (SessionHeadSnapshot テーブル、
PK=(persona, model) — beat_execution_context.md §3.1) 側に流れる。

詳細: docs/intent/cached_head_architecture.md / dynamic_state_sync.md
"""
from __future__ import annotations

import logging
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)


class DynamicStateManager:
    """Building 状態同期の facade (= head_pipeline への薄い委譲)。"""

    @staticmethod
    def maybe_inject_event_messages(persona: Any, manager: Any) -> bool:
        """world 状態の差分を末尾通知として SAIMemory に注入する。

        Phase 3-e で実装が ``sea.head_pipeline.integration.inject_diff_notifications``
        に統合された。本メソッドはその facade。

        Returns:
            True if a notification message was injected.
        """
        persona_id = getattr(persona, "persona_id", None)
        building_id = getattr(persona, "current_building_id", None)
        if not persona_id or not building_id:
            return False

        try:
            from sea.head_pipeline import inject_diff_notifications
        except Exception:
            LOGGER.warning(
                "[dynamic_state] head_pipeline unavailable, skipping diff inject",
                exc_info=True,
            )
            return False

        try:
            return bool(inject_diff_notifications(persona, manager, building_id))
        except Exception:
            LOGGER.exception(
                "[dynamic_state] maybe_inject_event_messages (via head_pipeline) failed for %s/%s",
                persona_id, building_id,
            )
            return False

    @staticmethod
    def on_building_entered(persona: Any, building_id: str, manager: Any) -> None:
        """ペルソナが新しい Building に入室したときの hook。

        Phase 3-e: BUILDING_ENTERED イベントを head_pipeline に dispatch するだけ。
        refresh_on_events に列挙した Section (building / visual_context /
        building_items / building_occupants 等) の snapshot が再構築される。

        dispatch_event の前に、新 Building の building_id を使って diff 通知を注入する。
        この時点では persona.current_building_id がまだ旧 Building を指しているため、
        maybe_inject_event_messages (persona.current_building_id を参照) は使えない。
        引数の building_id (= 移動先) を直接渡す。
        """
        if not getattr(persona, "persona_id", None):
            return
        try:
            from sea.head_pipeline import inject_diff_notifications
            inject_diff_notifications(persona, manager, building_id)
        except Exception:
            LOGGER.warning(
                "[dynamic_state] pre-dispatch diff inject failed for %s -> %s",
                getattr(persona, "persona_id", "?"), building_id, exc_info=True,
            )

        # 既に同じ Building にいる他ペルソナにも「この入室」を検知させる。
        # 入室は客観時間のイベントなので、居合わせる全員の知覚バッファへ push して
        # おく (消費は各自の次 Pulse)。これが無いと、既存者は自分が次に Pulse を
        # 打つまで新入りに気づけず、プレビューにも出ない (実運用で顕在化した穴、
        # 2026-07-09)。inject_diff_notifications は検知器 (push のみ、flush しない)
        # なので、居合わせる側を起こさずバッファに積むだけ。詳細: perception_buffer.md
        try:
            from sea.head_pipeline import inject_diff_notifications
            newcomer_id = getattr(persona, "persona_id", None)
            personas_map = getattr(manager, "personas", {})
            occupants = list(getattr(manager, "occupants", {}).get(building_id, []))
            for oid in occupants:
                if oid == newcomer_id:
                    continue
                other = personas_map.get(oid)
                if other is None:
                    continue  # user 等・未ロードのペルソナは対象外
                inject_diff_notifications(other, manager, building_id)
        except Exception:
            LOGGER.warning(
                "[dynamic_state] notify existing occupants failed on entry -> %s",
                building_id, exc_info=True,
            )

        # 移動先の様子 (アイテム一覧・内装画像・居合わせる他ペルソナの外見) を、移動した
        # 本人の知覚バッファへ push する。head の visual_context は移動で refresh されない
        # (cache 保護) ため、これが無いと本人は次の Metabolism まで新しい部屋のアイテム/
        # 内装を正確に知れず、旧部屋のものと誤認しうる (まはー指摘 2026-07-09)。
        # get_visual_context(include_self=False) は他ペルソナ外見+内装+アイテム(無い時も
        # 明示)+Fixture を返す。self は head と重複するので除外。消費は本人の次 Pulse。
        try:
            from builtin_data.tools.get_visual_context import get_visual_context
            from tools.context import persona_context
            pid = getattr(persona, "persona_id", None)
            pdir = getattr(persona, "persona_dir", None)
            sai_mem = getattr(persona, "sai_memory", None)
            if pid and sai_mem is not None:
                with persona_context(pid, pdir, manager):
                    # for_perception=True: 知覚バッファ向けの簡潔記法・<system> 包みなし。
                    vc_messages = get_visual_context(
                        building_id=building_id, include_self=False, for_perception=True,
                    )
                if vc_messages:
                    vc = vc_messages[0]
                    content = (vc.get("content") or "").strip()
                    media = (vc.get("metadata") or {}).get("media") or []
                    if content:
                        sai_mem.push_perception("surroundings", content, media=media)
        except Exception:
            LOGGER.warning(
                "[dynamic_state] surroundings push on entry failed -> %s",
                building_id, exc_info=True,
            )

        _dispatch_head_event(persona, manager, building_id, "building_entered")

    @staticmethod
    def on_metabolism(persona: Any, manager: Any, model_key: Optional[str] = None) -> None:
        """Metabolism 発火時の hook。

        Phase 3-e: METABOLISM イベントを head_pipeline に dispatch。
        全 Section の snapshot を再構築 + last_notified を A にリセット
        (= 末尾通知の窓を最新でリスタート)。

        ``model_key``: 可視化は model の節目 (beat_execution_context.md §3.2) —
        anchor を進めた model の (persona, model) snapshot だけを再 capture する。
        None は従来どおり persona の標準 model (organize-memory の全 model
        リセット経路など、節目の主が特定 model でない呼び出し)。
        """
        building_id = getattr(persona, "current_building_id", None)
        if not getattr(persona, "persona_id", None) or not building_id:
            return
        _dispatch_head_event(persona, manager, building_id, "metabolism", model_key=model_key)


def _dispatch_head_event(
    persona: Any, manager: Any, building_id: str, event_value: str,
    model_key: Optional[str] = None,
) -> None:
    """Cached Head Architecture pipeline に world イベントを通知する。

    pipeline が未初期化なら no-op (= startup 完了前のテスト経路では何もしない)。
    Phase 2-h / 3-e で挿入された統合点。
    詳細: docs/intent/cached_head_architecture.md
    """
    try:
        from sea.head_pipeline import (
            EventType,
            build_line_head_input,
            get_default_pipeline,
        )
    except Exception:
        return

    pipeline = get_default_pipeline()
    if not pipeline.registry.all_sections():
        # default sections 未登録 (= 初期化前 / テスト経路) なら何もしない
        return

    try:
        event = EventType(event_value)
    except ValueError:
        LOGGER.debug("dynamic_state: unknown head event %s", event_value)
        return

    ctx = build_line_head_input(persona, manager, building_id, model_key=model_key)
    try:
        pipeline.dispatch_event(ctx, event)
    except Exception:
        LOGGER.warning(
            "dynamic_state: head pipeline dispatch_event failed event=%s",
            event_value, exc_info=True,
        )
