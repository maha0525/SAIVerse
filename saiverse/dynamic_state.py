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
read/write はすべて `line_head_snapshot` (LineHeadSnapshot テーブル) 側に流れる。

詳細: docs/intent/cached_head_architecture.md / dynamic_state_sync.md
"""
from __future__ import annotations

import logging
from typing import Any

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
        _dispatch_head_event(persona, manager, building_id, "building_entered")

    @staticmethod
    def on_metabolism(persona: Any, manager: Any) -> None:
        """Metabolism 発火時の hook。

        Phase 3-e: METABOLISM イベントを head_pipeline に dispatch。
        全 Section の snapshot を再構築 + last_notified を A にリセット
        (= 末尾通知の窓を最新でリスタート)。
        """
        building_id = getattr(persona, "current_building_id", None)
        if not getattr(persona, "persona_id", None) or not building_id:
            return
        _dispatch_head_event(persona, manager, building_id, "metabolism")


def _dispatch_head_event(
    persona: Any, manager: Any, building_id: str, event_value: str,
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

    ctx = build_line_head_input(persona, manager, building_id)
    try:
        pipeline.dispatch_event(ctx, event)
    except Exception:
        LOGGER.warning(
            "dynamic_state: head pipeline dispatch_event failed event=%s",
            event_value, exc_info=True,
        )
