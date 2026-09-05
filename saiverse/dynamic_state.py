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
    def on_building_entered(persona: Any, building_id: str, manager: Any) -> bool:
        """ペルソナが新しい Building に入室したときの hook。

        Phase 3-e: BUILDING_ENTERED イベントを head_pipeline に dispatch するだけ。
        refresh_on_events に列挙した Section (building / visual_context /
        building_items / building_occupants 等) の snapshot が再構築される。

        dispatch_event の前に、新 Building の building_id を使って diff 通知を注入する。
        persona.current_building_id には依存しない — 呼び出しタイミングは経路で
        異なり (W7 以降、通常経路では move_entity が配送前に属性を新 Building へ
        sync 済み。outbox 再配送では任意のタイミングになる)、引数の building_id
        (= 移動先) だけが常に正しい。

        Returns:
            全段が成功したか (2026-07-21 Codex レビュー P2)。各段はそれぞれ
            独立した best-effort (1 段の失敗が他段を止めない、従来どおり) だが、
            outbox 配送経路 (move.post_dynamic_state ハンドラ) がこの戻り値を
            見て「1 段でも失敗したら配送失敗として再試行する」ために集約する。
            直接呼び出し元 (縮退経路・既存テスト) は戻り値を無視してよい。
        """
        if not getattr(persona, "persona_id", None):
            return True
        ok = True
        try:
            from sea.head_pipeline import inject_diff_notifications
            inject_diff_notifications(persona, manager, building_id)
        except Exception:
            LOGGER.warning(
                "[dynamic_state] pre-dispatch diff inject failed for %s -> %s",
                getattr(persona, "persona_id", "?"), building_id, exc_info=True,
            )
            ok = False

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
            ok = False

        # 移動先の様子 (アイテム一覧・内装画像・居合わせる他ペルソナの外見) を、移動した
        # 本人の知覚バッファへ push する。head の visual_context は移動で refresh されない
        # (cache 保護) ため、これが無いと本人は次の Metabolism まで新しい部屋のアイテム/
        # 内装を正確に知れず、旧部屋のものと誤認しうる (まはー指摘 2026-07-09)。
        # get_visual_context(include_self=False) は他ペルソナ外見+内装+アイテム(無い時も
        # 明示)+Fixture を返す。self は head と重複するので除外。消費は本人の次 Pulse。
        #
        # 積むのは全文とは限らない: 同じ部屋の前回のエントリがまだ提示に見えて
        # いれば差分だけになる (sai_memory/room_state.py)。行き来のたびに 1 万字
        # 級の全文が積み上がるのを止めるため (2026-09-04 まはー裁定)。
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
                        sai_mem.push_room_state(
                            building_id, content, media=media,
                            allow_diff=_chronicle_enabled(persona, manager),
                        )
        except Exception:
            LOGGER.warning(
                "[dynamic_state] surroundings push on entry failed -> %s",
                building_id, exc_info=True,
            )
            ok = False

        # 入室配送: 移動先にフィード施設があれば、その未読記事を本人の知覚
        # バッファへ積む (「移動先の様子」への相乗り —
        # issue feed_arrival_pulse_cannot_see_articles)。読まれるのは次の Beat 頭:
        # 出かけるコマなら到着直後のコマ Pulse、スペル移動なら同じラウンドの
        # 次の Beat。既読カーソルが台帳なので定期サイクル配送と重複しない。
        # 失敗しても入室処理は止めない (fail-open — deliver_unread_on_entry 内で
        # WARN 済み)。
        feed_mgr = getattr(manager, "feed_manager", None)
        if feed_mgr is not None:
            feed_mgr.deliver_unread_on_entry(persona, building_id)

        if not _dispatch_head_event(persona, manager, building_id, "building_entered"):
            ok = False
        return ok

    @staticmethod
    def on_metabolism(persona: Any, manager: Any, model_key: Optional[str] = None) -> bool:
        """Metabolism 発火時の hook。

        Phase 3-e: METABOLISM イベントを head_pipeline に dispatch。
        全 Section の snapshot を再構築する。last_notified (通知の既読基準) には
        触らない — 配送だけが進める (2026-08-17 まはー裁定。旧挙動の「A に
        リセット」は、発火タイミング次第で未通知の差分を握り潰していた)。

        ``model_key``: 可視化は model の節目 (beat_execution_context.md §3.2) —
        anchor を進めた model の (persona, model) snapshot だけを再 capture する。
        None は従来どおり persona の標準 model (organize-memory の全 model
        リセット経路など、節目の主が特定 model でない呼び出し)。

        Returns:
            再 capture の dispatch が成立したか (§15 読み戻しが再試行判定に
            使う。persona/building 不明での no-op は False)。従来の呼び出し元は
            戻り値を無視してよい。
        """
        building_id = getattr(persona, "current_building_id", None)
        if not getattr(persona, "persona_id", None) or not building_id:
            return False
        return _dispatch_head_event(
            persona, manager, building_id, "metabolism", model_key=model_key,
        )


def _chronicle_enabled(persona: Any, manager: Any) -> bool:
    """このペルソナが Chronicle 編纂を有効にしているか (判定不能なら有効側)。

    「部屋の様子」を差分に縮めてよいかの門。Chronicle 無効のペルソナは提示窓
    (anchor) でバッチを忘れる — 付記が起きないので、差分の土台になった全文が
    移管を経ずに提示から消えうる。そのため無効なら毎回全文を積む
    (sai_memory/room_state.py の「既知の境界」)。判定不能なときに有効側へ倒す
    のは、提示側の同じ門 (sea/runtime_context._chronicle_enabled_for) と揃える
    ため — 二つが食い違うと、窓で忘れる側なのに差分を積む組み合わせができる。
    """
    try:
        # runtime のたどり方は兄弟三箇所 (sea/head_pipeline/integration.py /
        # sections/memory_weave.py / saiverse/day_plan.py) と同じ二段の別名
        # 引き。sea_runtime だけを見ていると、runtime 側の名前しか持たない
        # manager で lifecycle が引けず、無効のペルソナにも差分を積んでしまう。
        runtime = (
            getattr(manager, "sea_runtime", None)
            or getattr(manager, "runtime", None)
        )
        lifecycle = getattr(runtime, "session_lifecycle", None)
        if lifecycle is None:
            return True
        return bool(lifecycle.is_chronicle_enabled_for_persona(persona))
    except Exception:
        LOGGER.debug(
            "[dynamic_state] chronicle toggle lookup failed; "
            "treating the persona as chronicle-enabled", exc_info=True,
        )
        return True


def _dispatch_head_event(
    persona: Any, manager: Any, building_id: str, event_value: str,
    model_key: Optional[str] = None,
) -> bool:
    """Cached Head Architecture pipeline に world イベントを通知する。

    pipeline が未初期化なら no-op (= startup 完了前のテスト経路では何もしない)。
    Phase 2-h / 3-e で挿入された統合点。
    詳細: docs/intent/cached_head_architecture.md

    Returns:
        dispatch できたか (2026-07-21 Codex レビュー P2)。head_pipeline 未導入 /
        未初期化 / 未知イベントは「対象外」であって失敗ではないので True。
        呼び出し元 (:meth:`DynamicStateManager.on_building_entered` /
        :meth:`~DynamicStateManager.on_metabolism`) の大半は戻り値を無視して
        よい — building_entered だけが outbox 再試行判定に使う。
    """
    try:
        from sea.head_pipeline import (
            EventType,
            build_line_head_input,
            get_default_pipeline,
        )
    except Exception:
        return True

    pipeline = get_default_pipeline()
    if not pipeline.registry.all_sections():
        # default sections 未登録 (= 初期化前 / テスト経路) なら何もしない
        return True

    try:
        event = EventType(event_value)
    except ValueError:
        LOGGER.debug("dynamic_state: unknown head event %s", event_value)
        return True

    ctx = build_line_head_input(persona, manager, building_id, model_key=model_key)
    try:
        pipeline.dispatch_event(ctx, event)
        return True
    except Exception:
        LOGGER.warning(
            "dynamic_state: head pipeline dispatch_event failed event=%s",
            event_value, exc_info=True,
        )
        return False
