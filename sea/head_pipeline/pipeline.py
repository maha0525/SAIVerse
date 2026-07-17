"""Cached Head Architecture: pipeline skeleton (capture / diff / render).

Section registry に登録された Section 群を順に呼び、
- Metabolism / refresh_on_events 発火時の snapshot capture
- 各 Pulse 起動時の diff チェック + 末尾通知ラベル生成
- LLM 投入直前の head render

を担当する。実 Section の登録は Phase 2 で進めるが、本モジュールは未登録状態でも
no-op として動く skeleton として完成させる (= 後で section 実装を差し込めば即動く形)。

DB 永続化は Phase 1-d (別モジュール) で繋ぐ。本モジュールはメモリ上の
LineHeadSnapshot 操作と Section 呼び出しの調整に集中する。

詳細: docs/intent/cached_head_architecture.md §3.5
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from sea.head_pipeline.registry import HeadSectionRegistry, get_default_registry
from sea.head_pipeline.types import (
    EventType,
    LineHeadInput,
    LineHeadSnapshot,
    NotificationLabel,
    RenderedSection,
)
from sea.head_pipeline.store import LineHeadSnapshotStore

LOGGER = logging.getLogger(__name__)


@dataclass
class _LineState:
    """1 Session (persona, model) 分の in-memory state。pipeline が保持する。

    永続化される snapshot とは別に、dirty フラグ・最後の通知済み snapshot・
    backstop タイマーを持つ。クラス名の "Line" は歴史的名称。
    """
    snapshot: LineHeadSnapshot                                 # A 相当 (frozen until refresh)
    last_notified_sections: dict[str, object] = field(default_factory=dict)  # B 相当 (section.name -> snapshot)
    dirty_sections: set[str] = field(default_factory=set)      # capture を急ぐべき Section 名
    last_backstop_check: float = 0.0                           # 最後に periodic backstop が走った epoch


class HeadPipeline:
    """Section pipeline の調整役。

    Phase 1 段階: in-memory のみ。DB 永続化は Phase 1-d で外部 store を inject する形に
    拡張する (= ここでは load / save の hook を空実装で持っておく)。

    複数 Session (= 複数 persona × model_key) の state を 1 つの pipeline で保持する。
    key は ``(persona_id, model_key)`` (beat_execution_context.md §3.1 —
    head は (persona, model) に一つ。line で分けない)。
    """

    # periodic backstop の最低間隔 (= 全 Section の capture+diff を回す保険)。
    # dirty マークが取りこぼされても最終的にここで救済される。
    BACKSTOP_INTERVAL_SECONDS = 300.0

    def __init__(
        self,
        registry: Optional[HeadSectionRegistry] = None,
        store: Optional[LineHeadSnapshotStore] = None,
    ) -> None:
        self._registry = registry or get_default_registry()
        self._store = store  # None なら永続化なし (Phase 1 テスト / startup 前用)
        self._states: dict[tuple[str, str], _LineState] = {}
        self._lock = threading.RLock()

    def attach_store(self, store: LineHeadSnapshotStore) -> None:
        """startup 後に DB session が用意できた段階で store を後付けする経路。"""
        self._store = store

    @property
    def registry(self) -> HeadSectionRegistry:
        """この pipeline が参照する Section registry。"""
        return self._registry

    # ---- snapshot 構築 ----

    def capture_all(self, ctx: LineHeadInput) -> LineHeadSnapshot:
        """全 Section の capture を走らせて新規 LineHeadSnapshot を作る。

        Metabolism 発火時 / 初回 / snapshot 不在時に呼ぶ。state も更新し、
        last_notified を新 snapshot にリセットする (= 通知済み = 直近 capture)。
        """
        sections = self._registry.all_sections()
        sections_dict: dict[str, object] = {}
        for section in sections:
            try:
                sections_dict[section.name] = section.capture(ctx)
            except Exception:
                LOGGER.exception(
                    "head_pipeline: capture failed for section=%s persona=%s model=%s",
                    section.name, ctx.persona_id, ctx.model_key,
                )
                # capture 失敗時は既存値を使う (なければ None)。pipeline 全体を止めない。
                existing = self._existing_section_snapshot(ctx, section.name)
                sections_dict[section.name] = existing

        snapshot = LineHeadSnapshot(
            persona_id=ctx.persona_id,
            model_key=ctx.model_key,
            line_role=ctx.line_role,
            captured_at=time.time(),
            snapshot_version=self._next_version(ctx),
            sections=sections_dict,
        )

        with self._lock:
            state = _LineState(
                snapshot=snapshot,
                last_notified_sections=dict(sections_dict),  # B = A reset
                dirty_sections=set(),
                last_backstop_check=time.time(),
            )
            self._states[(ctx.persona_id, ctx.model_key)] = state

        LOGGER.info(
            "head_pipeline: captured all sections persona=%s model=%s version=%d sections=%d",
            ctx.persona_id, ctx.model_key, snapshot.snapshot_version, len(sections_dict),
        )
        self._persist_snapshot(snapshot, dict(sections_dict))
        return snapshot

    def capture_for_event(self, ctx: LineHeadInput, event: EventType) -> Optional[LineHeadSnapshot]:
        """``event`` を refresh_on_events に含む Section だけ capture を再実行する。

        Metabolism 以外の refresh イベントで使う。snapshot の他 section は据え置き。
        該当 Section が 0 件なら何もしない (= None を返す)。
        """
        if event == EventType.METABOLISM:
            return self.capture_all(ctx)

        sections = self._registry.sections_for_event(event)
        if not sections:
            return None

        with self._lock:
            state = self._states.get((ctx.persona_id, ctx.model_key))
            if state is None:
                # snapshot 不在なら丸ごと作り直す方が安全 (event を Metabolism として扱う)
                LOGGER.debug(
                    "head_pipeline: capture_for_event without prior snapshot, falling back to capture_all",
                )
                return self.capture_all(ctx)

            new_sections = dict(state.snapshot.sections)
            for section in sections:
                try:
                    new_sections[section.name] = section.capture(ctx)
                    state.last_notified_sections[section.name] = new_sections[section.name]
                    state.dirty_sections.discard(section.name)
                except Exception:
                    LOGGER.exception(
                        "head_pipeline: capture failed for section=%s event=%s",
                        section.name, event.value,
                    )

            new_snapshot = LineHeadSnapshot(
                persona_id=state.snapshot.persona_id,
                model_key=state.snapshot.model_key,
                line_role=state.snapshot.line_role,
                captured_at=time.time(),
                snapshot_version=state.snapshot.snapshot_version + 1,
                sections=new_sections,
            )
            state.snapshot = new_snapshot
            notified_snapshot_copy = dict(state.last_notified_sections)

        LOGGER.info(
            "head_pipeline: captured event=%s sections=%s persona=%s model=%s",
            event.value, [s.name for s in sections], ctx.persona_id, ctx.model_key,
        )
        self._persist_snapshot(new_snapshot, notified_snapshot_copy)
        return new_snapshot

    # ---- イベント dispatch ----

    def dispatch_event(self, ctx: LineHeadInput, event: EventType) -> Optional[LineHeadSnapshot]:
        """world イベントを pipeline に届ける統一エントリ。

        event を ``refresh_on_events`` に含む Section があれば即 capture を走らせる
        (= snapshot 更新)。該当 Section が無い場合は Section の diff 検出をいつかは
        拾えるように、全 Section を dirty マークして次回 flush_diffs での backstop を
        早める (= snapshot は据え置きだが live 状態の差分は通知される)。

        Metabolism は常に全 Section の snapshot を再構築する (= capture_all 相当)。

        この経路を経由することで、新規 Section の登録だけで世界イベントへの
        反応 (refresh / 通知) が自動的に取れるようになる。詳細:
        docs/intent/cached_head_architecture.md §3.6
        """
        if event == EventType.METABOLISM:
            return self.capture_all(ctx)

        matched = self._registry.sections_for_event(event)
        if matched:
            return self.capture_for_event(ctx, event)

        # Section が refresh_on_events に列挙しなくても、live state の変化を
        # 次回 flush_diffs で拾えるように dirty マークだけはする。
        with self._lock:
            state = self._states.get((ctx.persona_id, ctx.model_key))
            if state is not None:
                state.dirty_sections.update(s.name for s in self._registry.all_sections())
        return None

    # ---- dirty 制御 + diff チェック ----

    def mark_dirty(self, persona_id: str, model_key: str, section_name: str) -> None:
        """指定 Section を dirty にマーク (= 次回 flush_diffs で diff チェック対象)。

        refresh_on_events の hook 等が個別 Section の dirty 化を伝えるのに使う。
        """
        with self._lock:
            state = self._states.get((persona_id, model_key))
            if state is not None:
                state.dirty_sections.add(section_name)

    def flush_diffs(
        self, ctx: LineHeadInput, *, all_sections: bool = False, advance: bool = True,
    ) -> list[NotificationLabel] | tuple[list[NotificationLabel], dict[str, object]]:
        """dirty Section + periodic backstop 対象 Section の diff をチェックし、
        差分があれば NotificationLabel 列を返す。

        ``all_sections=True`` で dirty / backstop 関係なく全 Section を強制チェックする
        (= 旧 dynamic_state.maybe_inject_event_messages 相当のフル diff 走査)。

        Section が ``capture_changes_since`` メソッドを持つ場合 (例: MemopediaIndex)、
        old.captured_at 以降の変化分を取りに行く since-aware capture を使う
        (= 全件比較を避けるため)。

        この戻り値を呼び出し側 (= runtime 経路) が末尾通知メッセージとして注入する。
        diff 検出後は last_notified を新 capture に進める (= 同じ差分を二重通知しない)。
        既読状態 (B) は (persona, model) の Session ごとに独立
        (beat_execution_context.md §3.1 — 「line ごとの diff 既読」→「model ごと」)。

        ``advance=False`` (S3 修正、統合工事 §6-4): **検出だけ行い B を進めない**。
        戻り値は ``(labels, {section_name: new_snapshot})`` のタプルになり、呼び出し側
        (integration.inject_diff_notifications) が配送を durable に確定
        (outbox mark_applied) した後に :meth:`advance_last_notified` で B を進める。
        配送前に B を進めると、配送失敗時に差分が永久に失われる (SEA 監査 S3)。
        差分が出た section の dirty マークも据え置く (= 配送失敗時は次回 flush で
        再検出される)。
        """
        with self._lock:
            state = self._states.get((ctx.persona_id, ctx.model_key))
            if state is None:
                # snapshot 不在 = 初回 capture 前、diff チェックの対象なし
                return [] if advance else ([], {})

            now = time.time()
            do_backstop = (now - state.last_backstop_check) >= self.BACKSTOP_INTERVAL_SECONDS
            if do_backstop or all_sections:
                state.last_backstop_check = now

            target_names: set[str] = set(state.dirty_sections)
            if do_backstop or all_sections:
                target_names.update(s.name for s in self._registry.all_sections())

            if not target_names:
                return [] if advance else ([], {})

            labels: list[NotificationLabel] = []
            detected: dict[str, object] = {}
            for section in self._registry.all_sections():
                if section.name not in target_names:
                    continue
                old_snapshot = state.last_notified_sections.get(section.name)

                capture_since = getattr(section, "capture_changes_since", None)
                if callable(capture_since):
                    # since-aware: old.captured_at 以降の変化のみ取りに行く
                    since = getattr(old_snapshot, "captured_at", 0.0) if old_snapshot else 0.0
                    try:
                        new_snapshot = capture_since(ctx, since)
                    except Exception:
                        LOGGER.exception(
                            "head_pipeline: capture_changes_since failed for section=%s",
                            section.name,
                        )
                        continue
                else:
                    try:
                        new_snapshot = section.capture(ctx)
                    except Exception:
                        LOGGER.exception(
                            "head_pipeline: capture during flush_diffs failed for section=%s",
                            section.name,
                        )
                        continue

                try:
                    section_labels = section.diff_to_notifications(old_snapshot, new_snapshot)
                except Exception:
                    LOGGER.exception(
                        "head_pipeline: diff_to_notifications failed for section=%s",
                        section.name,
                    )
                    continue

                if section_labels:
                    labels.extend(section_labels)
                    detected[section.name] = new_snapshot
                    if advance:
                        state.last_notified_sections[section.name] = new_snapshot
                    else:
                        # 検出のみ: B も dirty も据え置き (配送確定後に
                        # advance_last_notified が進める)。
                        continue

                state.dirty_sections.discard(section.name)

            notified_snapshot_copy = dict(state.last_notified_sections)

        if not advance:
            return labels, detected
        if labels:
            self._persist_last_notified(ctx.persona_id, ctx.model_key, notified_snapshot_copy)
        return labels

    def advance_last_notified(
        self,
        persona_id: str,
        section_name: str,
        new_section_snapshot: object,
    ) -> None:
        """該当 persona の**全 (persona, model) 行**の B (last_notified) を、
        指定 section だけ ``new_section_snapshot`` に前進させる (+ store 永続化)。

        head 操作の内容型通知 (§6-4) / outbox 化された diff 通知 (S3) の
        「push 確定後の B 前進」に使う。根拠: 知覚バッファ → SAIMemory は persona
        共有の履歴ストリームで、push は全 Session の窓に届く — 前進させないと
        backstop flush_diffs が同じ変化を model ごとに再通知する。
        dirty マークも該当 section だけ除去する。
        """
        persist_targets: list[tuple[str, dict[str, object]]] = []
        with self._lock:
            for (pid, model_key), state in self._states.items():
                if pid != persona_id:
                    continue
                state.last_notified_sections[section_name] = new_section_snapshot
                state.dirty_sections.discard(section_name)
                persist_targets.append(
                    (model_key, dict(state.last_notified_sections))
                )
        for model_key, notified in persist_targets:
            self._persist_last_notified(persona_id, model_key, notified)

    # ---- render ----

    def render_head(
        self, persona_id: str, model_key: str
    ) -> list[tuple[str, RenderedSection]]:
        """現在の snapshot から head の ``(section_name, RenderedSection)`` 列を作る。

        snapshot 経由のみで render する (= live state 参照不可)。snapshot 不在時は
        空 list を返す (呼び出し側で capture_all を先に呼ぶこと)。

        Section 名を render 結果に同梱する。render が None のセクションは除外される
        ため、呼び出し側が「order 順の位置」だけから名前を復元しようとすると、None
        セクションの分だけ後続がズレる (enabled フィルタが別セクションに化け、内容が
        欠落する)。名前を一緒に返して位置依存を排除する。
        """
        with self._lock:
            state = self._states.get((persona_id, model_key))
            if state is None:
                return []
            snapshot = state.snapshot

        rendered: list[tuple[str, RenderedSection]] = []
        for section in self._registry.all_sections():
            section_snapshot = snapshot.sections.get(section.name)
            if section_snapshot is None:
                continue
            try:
                result = section.render(section_snapshot)
            except Exception:
                LOGGER.exception(
                    "head_pipeline: render failed for section=%s persona=%s model=%s",
                    section.name, persona_id, model_key,
                )
                continue
            if result is not None:
                rendered.append((section.name, result))
        return rendered

    # ---- アクセサ ----

    def get_snapshot(self, persona_id: str, model_key: str) -> Optional[LineHeadSnapshot]:
        with self._lock:
            state = self._states.get((persona_id, model_key))
            return state.snapshot if state is not None else None

    def has_snapshot(self, persona_id: str, model_key: str) -> bool:
        return self.get_snapshot(persona_id, model_key) is not None

    def discard_session(self, persona_id: str, model_key: str, *, delete_persisted: bool = False) -> None:
        """指定 Session の in-memory state を破棄 (= cleanup 用)。

        ``delete_persisted=True`` のときは store 側のレコードも削除する。
        デフォルトは in-memory のみで、次回 startup 時に DB から復元できるように
        しておく。(旧名 ``discard_line`` — (persona, model) キー化で改名。)
        """
        with self._lock:
            self._states.pop((persona_id, model_key), None)
        if delete_persisted and self._store is not None:
            try:
                self._store.delete(persona_id, model_key)
            except Exception:
                LOGGER.exception(
                    "head_pipeline: store.delete failed persona=%s model=%s",
                    persona_id, model_key,
                )

    # ---- 永続化との同期 ----

    def load_from_store(self, persona_id: str, model_key: str) -> bool:
        """DB から state を復元して in-memory に積む。snapshot が無ければ False。

        startup 時 / 再起動後の状態復旧に使う。snapshot 復元後は last_notified も
        DB の値で B = 復元値 とする (= 同じ差分の二重通知を防ぐ)。
        """
        if self._store is None:
            return False
        stored = self._store.load(persona_id, model_key)
        if stored is None:
            return False
        with self._lock:
            self._states[(persona_id, model_key)] = _LineState(
                snapshot=stored.snapshot,
                last_notified_sections=dict(stored.last_notified_sections),
                dirty_sections=set(),
                last_backstop_check=time.time(),
            )
        LOGGER.info(
            "head_pipeline: loaded snapshot from store persona=%s model=%s version=%d",
            persona_id, model_key, stored.snapshot.snapshot_version,
        )
        return True

    def _persist_snapshot(
        self,
        snapshot: LineHeadSnapshot,
        last_notified_sections: dict[str, object],
    ) -> None:
        if self._store is None:
            return
        try:
            self._store.save(snapshot, last_notified_sections)
        except Exception:
            LOGGER.exception(
                "head_pipeline: store.save failed persona=%s model=%s",
                snapshot.persona_id, snapshot.model_key,
            )

    def _persist_last_notified(
        self,
        persona_id: str,
        model_key: str,
        last_notified_sections: dict[str, object],
    ) -> None:
        if self._store is None:
            return
        try:
            self._store.save_last_notified(persona_id, model_key, last_notified_sections)
        except Exception:
            LOGGER.exception(
                "head_pipeline: store.save_last_notified failed persona=%s model=%s",
                persona_id, model_key,
            )

    # ---- 内部ヘルパー ----

    def _existing_section_snapshot(self, ctx: LineHeadInput, section_name: str) -> object:
        with self._lock:
            state = self._states.get((ctx.persona_id, ctx.model_key))
            if state is None:
                return None
            return state.snapshot.sections.get(section_name)

    def _next_version(self, ctx: LineHeadInput) -> int:
        with self._lock:
            state = self._states.get((ctx.persona_id, ctx.model_key))
            return (state.snapshot.snapshot_version + 1) if state is not None else 1


_default_pipeline: Optional[HeadPipeline] = None
_default_pipeline_lock = threading.Lock()


def get_default_pipeline() -> HeadPipeline:
    """プロセス内 singleton pipeline を返す。"""
    global _default_pipeline
    if _default_pipeline is None:
        with _default_pipeline_lock:
            if _default_pipeline is None:
                _default_pipeline = HeadPipeline()
    return _default_pipeline
