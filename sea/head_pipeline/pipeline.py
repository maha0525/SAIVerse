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
    # store.save の成功を確認できた最後の snapshot_version (W6 fail-closed)。
    # 0 = 一度も保存確認なし。bool フラグでなく version で持つのは、並行 capture
    # (Pulse と world イベント / keepalive は別スレッド) で「旧 snapshot の保存
    # 成功が新 snapshot の保存失敗を上書きする」競合を防ぐため — 保存結果は
    # 「どの版を保存したか」に結び付けてしか前進させない (Codex P1, 2026-07-21)。
    # readiness 検証 (ensure_persisted) は persisted_version >= 現行版 を要求する。
    # store 未設定環境では検証自体がスキップされる。
    persisted_version: int = 0


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
        capture_failures: dict[str, str] = {}
        for section in sections:
            try:
                sections_dict[section.name] = section.capture(ctx)
            except Exception as exc:
                LOGGER.exception(
                    "head_pipeline: capture failed for section=%s persona=%s model=%s",
                    section.name, ctx.persona_id, ctx.model_key,
                )
                # capture 失敗時は既存値を使う (stale-but-real)。既存値も無ければ
                # key ごと省略し capture_failures に理由を残す — None を snapshot
                # 値として保存すると「欠損なのに存在扱い」になり fail-closed 検証
                # (W6 / SEA 監査 S6) をすり抜けるため。
                existing = self._existing_section_snapshot(ctx, section.name)
                if existing is not None:
                    sections_dict[section.name] = existing
                else:
                    capture_failures[section.name] = f"capture failed: {exc!r}"

        # state 不在時の採番フォールバック (DB 行の版継続) はロック外で先に
        # 引いておく — 版の**割り当て自体**は公開と同じロック区間で行う。
        # 採番と公開が別区間だと、並行 capture_all が「同じ版番号の異なる
        # snapshot」を作り、片方の保存成功がもう片方 (未保存) を保存済みに
        # 見せかける (Codex 三巡 P1)。
        stored_base = 0
        if not self.has_snapshot(ctx.persona_id, ctx.model_key):
            stored_base = self._stored_base_version(ctx.persona_id, ctx.model_key)

        snapshot = LineHeadSnapshot(
            persona_id=ctx.persona_id,
            model_key=ctx.model_key,
            line_role=ctx.line_role,
            captured_at=time.time(),
            snapshot_version=0,  # 公開ロック内で採番する (下)
            sections=sections_dict,
            capture_failures=capture_failures,
        )

        with self._lock:
            prev = self._states.get((ctx.persona_id, ctx.model_key))
            base = prev.snapshot.snapshot_version if prev is not None else stored_base
            snapshot.snapshot_version = base + 1
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

    def recapture_missing(
        self, ctx: LineHeadInput, names: set[str],
    ) -> Optional[LineHeadSnapshot]:
        """欠損している Section (``names``) **だけ** capture を再試行する (W6)。

        ensure_snapshot の自己修復経路。capture_all で全 Section を作り直すと、
        1 つの Section が失敗し続けるだけで毎 Pulse 全 head が再構築され、
        「Metabolism まで凍結」の原則 (= prompt cache の安定) が壊れ、
        last_notified (B) も毎回リセットされて差分通知の基準が消える
        (Codex P2, 2026-07-21)。ここでは欠損分のみを埋め、他 Section の
        snapshot / B / dirty は一切触らない。

        成功した Section が 1 つでもあれば版を進めて永続化する。全滅なら
        既存 snapshot の capture_failures に理由を追記するだけ (版は据え置き =
        cache も B も無傷)。state 不在時は capture_all にフォールバック。
        """
        with self._lock:
            state = self._states.get((ctx.persona_id, ctx.model_key))
        if state is None:
            return self.capture_all(ctx)

        sections_by_name = {s.name: s for s in self._registry.all_sections()}
        captured: dict[str, object] = {}
        failures: dict[str, str] = {}
        for name in sorted(names):
            section = sections_by_name.get(name)
            if section is None:
                continue  # registry から消えた Section の残骸は無視
            try:
                captured[name] = section.capture(ctx)
            except Exception as exc:
                LOGGER.exception(
                    "head_pipeline: recapture failed for section=%s persona=%s model=%s",
                    name, ctx.persona_id, ctx.model_key,
                )
                failures[name] = f"capture failed: {exc!r}"

        with self._lock:
            state = self._states.get((ctx.persona_id, ctx.model_key))
            if state is None:
                return None  # 並行 discard — 呼び出し側の readiness 検証に委ねる

            # ロック外 capture の間に並行 capture (Metabolism 等) が同じ Section
            # を新値で埋めていたら、こちらの (より古い世界を見た) 結果で上書き
            # しない — 「今も欠損している名前」だけを適用する (Codex 二巡 P2)。
            still_missing = {
                name for name in names
                if state.snapshot.sections.get(name) is None
            }
            captured = {k: v for k, v in captured.items() if k in still_missing}
            failures = {k: v for k, v in failures.items() if k in still_missing}

            if not captured:
                # 全滅 (or 並行 capture が全部埋めた): 失敗理由だけ追記して
                # 据え置き (readiness 検証がこれを見て required なら止める)。
                # 版を進めない = cache / B は無傷。
                state.snapshot.capture_failures.update(failures)
                return state.snapshot

            new_sections = dict(state.snapshot.sections)
            new_failures = dict(state.snapshot.capture_failures)
            new_failures.update(failures)
            for name, value in captured.items():
                new_sections[name] = value
                new_failures.pop(name, None)
                # 欠損からの復帰は「初めて head に載る」扱い — B も新値に合わせ、
                # 復帰直後の diff 通知スパムを防ぐ (capture_for_event と同じ規約)。
                state.last_notified_sections[name] = value
                state.dirty_sections.discard(name)

            new_snapshot = LineHeadSnapshot(
                persona_id=state.snapshot.persona_id,
                model_key=state.snapshot.model_key,
                line_role=state.snapshot.line_role,
                captured_at=time.time(),
                snapshot_version=state.snapshot.snapshot_version + 1,
                sections=new_sections,
                capture_failures=new_failures,
            )
            state.snapshot = new_snapshot
            notified_snapshot_copy = dict(state.last_notified_sections)

        LOGGER.info(
            "head_pipeline: recaptured missing sections=%s persona=%s model=%s version=%d",
            sorted(captured.keys()), ctx.persona_id, ctx.model_key,
            new_snapshot.snapshot_version,
        )
        self._persist_snapshot(new_snapshot, notified_snapshot_copy)
        return new_snapshot

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
            capture_failures = dict(state.snapshot.capture_failures)
            for section in sections:
                try:
                    new_sections[section.name] = section.capture(ctx)
                    capture_failures.pop(section.name, None)
                    state.last_notified_sections[section.name] = new_sections[section.name]
                    state.dirty_sections.discard(section.name)
                except Exception as exc:
                    LOGGER.exception(
                        "head_pipeline: capture failed for section=%s event=%s",
                        section.name, event.value,
                    )
                    # 既存値があれば据え置き (stale-but-real)。無ければ欠損として
                    # 理由を記録する (capture_all と同じ fail-closed 規約)。
                    if new_sections.get(section.name) is None:
                        new_sections.pop(section.name, None)
                        capture_failures[section.name] = f"capture failed: {exc!r}"

            new_snapshot = LineHeadSnapshot(
                persona_id=state.snapshot.persona_id,
                model_key=state.snapshot.model_key,
                line_role=state.snapshot.line_role,
                captured_at=time.time(),
                snapshot_version=state.snapshot.snapshot_version + 1,
                sections=new_sections,
                capture_failures=capture_failures,
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
        self, persona_id: str, model_key: str,
        *, fail_closed_sections: frozenset[str] = frozenset(),
        snapshot: Optional[LineHeadSnapshot] = None,
    ) -> list[tuple[str, RenderedSection]]:
        """snapshot から head の ``(section_name, RenderedSection)`` 列を作る。

        ``snapshot`` 省略時は現在の state の snapshot を読む。呼び出し側が
        readiness 検証済みの snapshot を持っている場合は明示で渡す (pin) —
        検証と render の間に別スレッドが未検証の新版を公開しても、検証した
        その版を描画する (Codex 二巡 P1: 検証・永続化・描画の版一致)。

        snapshot 経由のみで render する (= live state 参照不可)。snapshot 不在時は
        空 list を返す (呼び出し側で capture_all を先に呼ぶこと)。

        Section 名を render 結果に同梱する。render が None のセクションは除外される
        ため、呼び出し側が「order 順の位置」だけから名前を復元しようとすると、None
        セクションの分だけ後続がズレる (enabled フィルタが別セクションに化け、内容が
        欠落する)。名前を一緒に返して位置依存を排除する。

        ``fail_closed_sections`` に入った名前の render 例外は握らず
        :class:`HeadNotReadyError` (stage="render") にして投げる (W6 / SEA 監査
        S6 — required Section を黙って skip すると人格を欠いた head で LLM が
        走る)。それ以外の render 例外は従来どおり skip + ERROR ログで degrade。
        render が None を返すのは「空 Section」の正規挙動で、fail-closed の
        対象ではない。
        """
        from sea.head_pipeline.types import HeadNotReadyError

        if snapshot is None:
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
            except Exception as exc:
                LOGGER.exception(
                    "head_pipeline: render failed for section=%s persona=%s model=%s",
                    section.name, persona_id, model_key,
                )
                if section.name in fail_closed_sections:
                    raise HeadNotReadyError(
                        persona_id, model_key, "render",
                        {section.name: f"render failed: {exc!r}"},
                    ) from exc
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
                # store から来た snapshot は定義上 durable (= その版は保存確認済み)
                persisted_version=stored.snapshot.snapshot_version,
            )
        LOGGER.info(
            "head_pipeline: loaded snapshot from store persona=%s model=%s version=%d",
            persona_id, model_key, stored.snapshot.snapshot_version,
        )
        return True

    def ensure_persisted(
        self, persona_id: str, model_key: str,
        *, min_version: Optional[int] = None,
    ) -> bool:
        """指定版までの snapshot 保存が未確認なら再 persist を試みる (W6)。

        readiness 検証 (integration.render_head_messages) が LLM 実行前に呼ぶ。
        ``min_version`` は呼び出し側が pin した snapshot の版 — 「少なくとも
        この版までは durable」を要求する (省略時は現行版)。戻り値 False =
        いまもその版を永続化できていない (= restart で旧 head にロールバック
        する状態のまま LLM を走らせてはいけない)。
        store 未設定 / state 不在 / 保存確認済みなら True。

        不変条件は **単調性** — 「durable な版 >= 描画する版」であって版の厳密
        一致ではない (Codex 五巡の指摘に対する裁定, 2026-07-21)。pin した版が
        未保存でも、並行 capture のより新しい版が durable なら restart は前進
        方向にしか動かず、監査 S6 の害 (旧 head への黙った巻き戻り) は起きない。
        厳密一致を要求すると並行 capture のたびに正当な Pulse が止まる。
        """
        if self._store is None:
            return True
        with self._lock:
            state = self._states.get((persona_id, model_key))
            if state is None:
                return True
            target = min_version if min_version is not None else state.snapshot.snapshot_version
            if state.persisted_version >= target:
                return True
            snapshot = state.snapshot
            notified = dict(state.last_notified_sections)
        # 現行 state の snapshot を保存する (版は pin した版以上)。成功すれば
        # persisted_version >= 現行版 >= target になる。
        if not self._persist_snapshot(snapshot, notified):
            # 失敗が stale 拒否 (in-memory 版 < DB 版) なら再採番して一度だけ
            # 再試行する — capture_all 時の load_version 一時失敗で低い版から
            # 採番してしまった state が、DB 復旧後も恒久拒否され続けるのを防ぐ
            # (Codex 四巡 P1: 読取失敗を「行なし」と同一視した毒の自己回復)。
            rebased = self._try_rebase_stale_version(persona_id, model_key, snapshot)
            if rebased is None or not self._persist_snapshot(rebased, notified):
                return False
        with self._lock:
            state = self._states.get((persona_id, model_key))
            if state is None:
                return True
            return state.persisted_version >= target

    def _persist_snapshot(
        self,
        snapshot: LineHeadSnapshot,
        last_notified_sections: dict[str, object],
    ) -> bool:
        """store.save を試み、成功なら「保存した版」を persisted_version に記帳する。

        記帳は「保存した snapshot オブジェクトが現行 state の snapshot と同一
        (is)」のときだけ行う: 版番号の一致では足りない — 並行 capture_all が
        同版番号の別内容 snapshot を作った場合に、旧オブジェクトの保存成功が
        現行 (未保存) を保存済みに見せかける (Codex 三巡 P1)。同一でなければ
        記帳しない = 現行版は未確認のまま残り、ensure_persisted が現行を
        保存し直す。失敗は何も進めない。

        store 未設定 (テスト / startup 前) は「永続化なし」が構成上の正なので
        True 扱い (fail-closed の対象外)。
        """
        if self._store is None:
            return True
        try:
            ok = self._store.save(snapshot, last_notified_sections)
        except Exception:
            LOGGER.exception(
                "head_pipeline: store.save failed persona=%s model=%s",
                snapshot.persona_id, snapshot.model_key,
            )
            ok = False
        if ok:
            with self._lock:
                state = self._states.get((snapshot.persona_id, snapshot.model_key))
                if (
                    state is not None
                    and state.snapshot is snapshot
                    and state.persisted_version < snapshot.snapshot_version
                ):
                    state.persisted_version = snapshot.snapshot_version
        return ok

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

    def _try_rebase_stale_version(
        self, persona_id: str, model_key: str, snapshot: LineHeadSnapshot,
    ) -> Optional[LineHeadSnapshot]:
        """保存失敗が stale 拒否だった場合の再採番 (Codex 四巡 P1 の自己回復)。

        DB の現在版を読み直し、渡された snapshot がまだ現行 state のもので、
        かつ版が DB 以下 (= store.save の版ガードに拒否される) なら DB+1 に
        採番し直して返す。該当しない失敗 (DB 障害そのもの / state 交代済み /
        版はすでに新しい) は None — 呼び出し側はそのまま失敗を返し、次の
        readiness 検証で再試行される。
        """
        loader = getattr(self._store, "load_version", None)
        if not callable(loader):
            return None
        try:
            stored = int(loader(persona_id, model_key) or 0)
        except Exception:
            return None
        with self._lock:
            state = self._states.get((persona_id, model_key))
            if state is None or state.snapshot is not snapshot:
                return None
            if snapshot.snapshot_version > stored:
                return None  # stale ではない (別要因の失敗)
            LOGGER.warning(
                "head_pipeline: rebasing snapshot version v%d -> v%d after stale "
                "rejection persona=%s model=%s",
                snapshot.snapshot_version, stored + 1, persona_id, model_key,
            )
            snapshot.snapshot_version = stored + 1
            return snapshot

    def _stored_base_version(self, persona_id: str, model_key: str) -> int:
        """state 不在時の採番ベース: store の既存行の SNAPSHOT_VERSION (無ければ 0)。

        再起動後に load を経ずに capture_all へ来た経路 (dispatch_event の
        Metabolism 等) で 1 から振り直すと、store.save の stale 版ガード
        (Codex 二巡 P1) に「古い版の上書き」として永久拒否され、fail-closed が
        解けなくなる — DB の版から採番を継続する。
        """
        loader = getattr(self._store, "load_version", None)
        if not callable(loader):
            return 0
        try:
            return int(loader(persona_id, model_key) or 0)
        except Exception:
            LOGGER.exception(
                "head_pipeline: load_version failed persona=%s model=%s",
                persona_id, model_key,
            )
            return 0


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
