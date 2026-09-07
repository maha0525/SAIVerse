"""Cached Head Architecture: runtime_context への統合層。

`prepare_context` から呼ばれる thin adapter。LineHeadInput を組み立てて
pipeline.render_head の RenderedSection 列を取得し、それを LLM に渡す
message dict 列 (system + user / media 含む) に composition する。

Section 群と message role / metadata の対応はこの層で握る:
- ``common_prompt`` / ``persona_self`` / ``building`` / ``available_playbooks`` /
  ``spell_list``: text-only、まとめて 1 つの system message にする
- ``memory_weave``: text-only、独立した user role message にする (旧 get_memory_weave_context 経路と互換)

部屋の描画 (旧 ``visual_context`` Section) は 2026-09-06 に head から退役した —
部屋の様子の置き場は知覚 (tail) 一つ (docs/intent/room_state_packages.md)。
本層はその供給側の検知 (:func:`inject_diff_notifications` の部屋の照合) も持つ。

詳細: docs/intent/cached_head_architecture.md §3.5 / §5
"""
from __future__ import annotations

import json
import logging
import threading
import time
from functools import partial
from typing import Any, Optional

from sea.head_pipeline.pipeline import HeadPipeline, get_default_pipeline
from sea.head_pipeline.types import LineHeadInput, RenderedSection

# 旧 builtin_data/tools/get_memory_weave_context.py が message metadata に付与していた
# marker / type field 名と互換のキーを composition で再現する (= preview UI の
# section 識別ロジックがこれらを参照する: sea/runtime_context.py:618 周辺)。
_MEMORY_WEAVE_CONTEXT_MARKER = "__memory_weave_context__"
_MEMORY_WEAVE_TYPE_KEY = "__memory_weave_type__"

LOGGER = logging.getLogger(__name__)

# 既知 Section の役割マッピング。新規 Section 追加時はここに分類を足す。
# 並び順 = system メッセージ内の出力順。各 Section の order 属性に合わせる
# (common_prompt < persona_self < core_memory(250) < building(300) < facilities(310) <
#  available_playbooks(400) < autonomy_modes(550) < self_image(560) <
#  spell_list(600) < desk(730))。
# 旧 open_notes(720) は P3c① (concept_consolidation.md「Note → テーマノード移行」)
# で退役し、後継の desk (机の物理) に置き換わった。
SYSTEM_PROMPT_SECTION_NAMES: tuple[str, ...] = (
    # 注意: Section を新設して head に描画させる場合、ここと
    # sea/runtime_context.py の enabled_sections の**両方**に名前を足すこと。
    # 片方でも漏れると「登録済みなのに一度も描画されない」silent 故障になる
    # (DeskSection P2a〜P3c① / MemopediaIndexSection P4-d で二度起きた実績)。
    # tests/test_head_section_wiring.py が両点の整合を機械検査する。
    "common_prompt",
    "persona_self",
    "core_memory",
    "building",
    "facilities",
    "available_playbooks",
    "autonomy_modes",
    "self_image",
    "spell_list",
    "desk",
    # P4-d: Memopedia 目次 (opt-in 実験。per-persona フラグ MEMOPEDIA_INDEX_ENABLED
    # が render 内でゲートするので、ここは無条件で列挙してよい — OFF なら None render)
    "memopedia_index",
)
MEMORY_WEAVE_SECTION_NAME = "memory_weave"

_DEFAULT_LINE_ROLE = "main_line"


def resolve_default_model_key(persona: Any) -> str:
    """ExecutionContext が届かない経路の model_key フォールバック。

    ペルソナの標準 (default) model を返す。実属性は ``persona.model``
    (persona/core.py) — 旧実装は存在しない ``persona.default_model`` を先に
    引いていたため、実ペルソナでは常に "default" 文字列に落ちていた
    (line_head_snapshot の MODEL_KEY が全行 'default' だった実バグ)。
    ``default_model`` はテストスタブ互換のため第 2 候補として残す。
    """
    model_key = (
        getattr(persona, "model", None)
        or getattr(persona, "default_model", None)
        or getattr(persona, "DEFAULT_MODEL", None)
        or "default"
    )
    return str(model_key)


def build_line_head_input(
    persona: Any,
    manager: Any,
    building_id: str,
    *,
    model_key: Optional[str] = None,
    line_role: str = _DEFAULT_LINE_ROLE,
) -> LineHeadInput:
    """prepare_context の引数から LineHeadInput を組み立てる。

    head の同定キーは (persona_id, model_key) (beat_execution_context.md §3.1)。
    ``model_key`` は「この head をどの Session (persona, model) に向けて
    組むか」— ExecutionContext が届いている呼び出し元 (LLM node / work_session /
    sluice / keepalive) は ``execution_context.model_key`` を明示で渡す。
    None なら persona の標準 model にフォールバックする (起動時 capture /
    dynamic_state の world イベント dispatch 等、実行の身分が無い経路)。
    """
    persona_id = getattr(persona, "persona_id", "") or ""
    model_key_str = str(model_key) if model_key else resolve_default_model_key(persona)

    anchor_updated_at, cache_ttl_seconds = _resolve_anchor_ttl_state(
        persona, manager, model_key_str,
    )

    return LineHeadInput(
        persona_id=persona_id,
        model_key=model_key_str,
        line_role=line_role,
        current_building_id=building_id,
        persona=persona,
        manager=manager,
        anchor_updated_at=anchor_updated_at,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def _resolve_anchor_ttl_state(
    persona: Any, manager: Any, model_key: str,
) -> tuple[Optional[float], Optional[int]]:
    """session_anchor 行 (persona, model_key) の updated_at を epoch seconds として取得、
    同 model の cache TTL (秒) と組で返す。

    LLM 呼び出し成功後に ``SessionLifecycle.touch_anchor_after_llm_call`` が touch する updated_at が
    prompt cache の真の起点 (= 最後の cache 書き込み時刻)。 これと TTL を ctx に
    積むことで、 ``ensure_snapshot`` が「TTL 超えたら snapshot も再 capture」 を
    判定できる。 anchor 不在 (初期状態 / load 失敗) / model 不明時は両方 None で返し、
    判定スキップ = 従来挙動になる。
    """
    if not manager or not model_key:
        return (None, None)
    sea_runtime = getattr(manager, "sea_runtime", None) or getattr(manager, "runtime", None)
    if sea_runtime is None:
        return (None, None)

    lifecycle = getattr(sea_runtime, "session_lifecycle", None)
    load_anchors = getattr(lifecycle, "load_anchors", None)
    get_validity = getattr(lifecycle, "get_anchor_validity_seconds", None)
    if load_anchors is None or get_validity is None:
        return (None, None)

    try:
        anchors = load_anchors(persona) or {}
        entry = anchors.get(model_key)
        if not entry:
            return (None, None)
        updated_at_iso = entry.get("updated_at")
        if not updated_at_iso:
            return (None, None)
        from datetime import datetime
        updated_at_epoch = datetime.fromisoformat(updated_at_iso).timestamp()
        # 書き込み時に記録した ttl_seconds を優先 (設定変更の遡及影響を防ぐ)。
        # 旧 anchor (ttl_seconds 無し) は現行設定にフォールバック。
        stored_ttl = entry.get("ttl_seconds")
        validity = int(stored_ttl) if stored_ttl else int(get_validity(model_key, getattr(persona, "persona_id", None)))
        return (updated_at_epoch, validity)
    except Exception:
        LOGGER.warning(
            "head_pipeline: failed to resolve anchor TTL state persona=%s model=%s",
            getattr(persona, "persona_id", "?"), model_key, exc_info=True,
        )
        return (None, None)


def inject_diff_notifications(
    persona: Any,
    manager: Any,
    building_id: str,
    *,
    pipeline: HeadPipeline | None = None,
    model_key: str | None = None,
    detect_room: bool = True,
    only_sections: set[str] | None = None,
) -> bool:
    """全 Section の diff を検知し、知覚バッファへ型付き項目として push する (消費はしない)。

    ``only_sections`` を渡すと、その名前の Section だけを検知の対象にする
    (:meth:`HeadPipeline.flush_diffs` の ``only``)。対象外の Section は capture も
    diff もされないので、その変化は次に全 Section で走る検知が拾う。

    ``detect_room`` — 「部屋の様子」のパッケージ照合 + 自己回復
    (:func:`_detect_room_state_changes`、docs/intent/room_state_packages.md §6-2)
    も併せて走らせるか。入室処理 (saiverse/dynamic_state.on_building_entered) の
    本人向け呼び出しだけ False — 直後の入室 push が同じ部屋を積むので、ここでも
    照合すると入室が二重に語られる。戻り値は従来どおり Section のラベルの
    有無だけを見る (部屋の照合の push は数えない)。

    【知覚バッファ経由に変更 (2026-07-09, Phase 2)】以前は差分を直接 SAIMemory へ
    append していたが、これは「検知＝消費」を癒着させ、pulse 前のプレビューを不可能に
    していた。本関数は **検知器** に徹し、差分を知覚バッファ (kind='world_state') へ
    push するだけにする。実際に SAIMemory へ入る (= ペルソナが知覚する) のは呼び出し元
    が ``flush_perception_buffer`` を呼ぶ消費時。呼び出し元ごとの flush 制御:
    - Pulse 開始 (run_meta_user): 全 Section を push → 末尾で flush (同 Pulse で消費)。
    - Pulse 中の metabolism 直後 (runtime_context): push → 直後に flush (同ターン知覚)。
    - 移動時 (on_building_entered, pulse 外): 移動した本人へ push するのは
      **移動の事実だけ** で、対象は building と building_occupants の 2 つ
      (``only_sections={"building", "building_occupants"}``)。在室者の側は
      部屋替えの分岐が deliver=False のラベルしか出さないので文は届かず、
      ここで走るのは基準合わせ (新しい部屋の顔ぶれまで) だけ。
      スペル・Memopedia 等の状態の差分は Pulse 開始時の検知が「最後に知らせた
      状態 vs 今」で計算する — 移動のたびに途中経過を積むと、読む時点
      (次の Pulse) には別の部屋の話になっている (2026-09-07、
      docs/issues/perception_state_pushed_at_event_time.md)。在室者を対象から
      外すと基準が旧部屋のまま残り、本人が Pulse を打つ前に誰かが同じ部屋へ
      入ってきた回が「部屋替え」の比較に化けて入室の知らせが消える。
      消費は次の Pulse (= 主観時間が止まっている間の知覚は詰まって待つ、という
      時間モデル通り)。

    ``deliver=False`` のラベル (:class:`~sea.head_pipeline.types.NotificationLabel`)
    は「検知はするが文は届けない」— push はせず、基準 (B) の前進にだけ使う。
    deliver=False だけの回は配送そのものが無いので台帳の execution も作らず、
    戻り値も False (= 何も届けていない)。

    再会の想起 (persona_recall) はこの検知器の仕事ではない — Pulse の頭で
    「いま同席している相手」を見る :func:`inject_copresence_recall` が積む
    (2026-09-07 に移動時のラベル発火から移した。
    docs/intent/perception_buffer.md §4.5 / §5.1)。

    ``model_key`` を省略した場合は persona の標準 model の Session に対して
    diff チェックする (= 従来の単一窓挙動と同じ)。既読状態 (last_notified) は
    (persona, model) ごとに独立 (beat_execution_context.md §3.1)。

    【outbox 経由に変更 (2026-07-17, 統合工事 §6-4 / SEA 監査 S5・S3)】
    manager が execution_ledger を持つ環境では、ラベル群を実行台帳の outbox
    (target='perception.push') で配送する — 知覚バッファの flush 失敗で通知が
    全消失する穴 (S5) を配達保証で塞ぐ。B (last_notified) の前進は outbox 積みの
    durable 確定 (mark_applied) **後** に行う (S3: 配送前に B を進めると配送失敗時に
    差分が永久に失われる)。B は persona の全 (persona, model) 行を前進させる —
    知覚バッファ → SAIMemory は persona 共有の履歴ストリームで、push は全 Session
    の窓に届くため。台帳が無い環境 (旧テスト等) は従来どおり直接 push +
    flush_diffs 内での B 前進に degrade する。

    Returns:
        ラベルが 1 件以上 push された場合 True、差分なしなら False。
    """
    pipeline = pipeline or get_default_pipeline()
    ctx = build_line_head_input(persona, manager, building_id, model_key=model_key)
    ensure_snapshot(pipeline, ctx)

    pushed = _push_section_diffs(
        persona, manager, pipeline, ctx, building_id,
        only_sections=only_sections,
    )

    if detect_room:
        try:
            _detect_room_state_changes(
                persona, manager, building_id, model_key=model_key,
            )
        except Exception:
            LOGGER.warning(
                "head_pipeline: room-state detection failed persona=%s "
                "building=%s", ctx.persona_id, building_id, exc_info=True,
            )
    return pushed


def _push_section_diffs(
    persona: Any,
    manager: Any,
    pipeline: HeadPipeline,
    ctx: LineHeadInput,
    building_id: str,
    *,
    only_sections: set[str] | None = None,
) -> bool:
    """Section 群の diff ラベルを検知して知覚バッファ (or outbox) へ push する。"""
    ledger = getattr(manager, "execution_ledger", None)
    if ledger is None:
        return _inject_diff_notifications_direct(
            persona, pipeline, ctx, building_id, only_sections=only_sections,
        )

    labels, detected = pipeline.flush_diffs(
        ctx, all_sections=True, advance=False, only=only_sections,
    )
    if not labels:
        return False

    deliverable = [label for label in labels if label.deliver]
    if not deliverable:
        # 検知だけのラベル (deliver=False) しか無い回。配送する文が無いので台帳は
        # 通さず、基準だけ新しい状態へ進める — 進めないと以後の差分が古い基準との
        # 比較になって出なくなる (部屋替え時の同席者がこれ)。
        for section_name, new_snapshot in detected.items():
            pipeline.advance_last_notified(ctx.persona_id, section_name, new_snapshot)
        return False

    try:
        execution_id, _created = ledger.begin_execution(
            "head.diff_notify", idempotency_key=None, persona_id=ctx.persona_id,
        )
        ledger.mark_running(execution_id)
        outbox_items = [
            {
                "target": "perception.push",
                "persona_id": ctx.persona_id,
                "payload": {
                    "kind": "world_state",
                    "content": label.label,
                    "reduce_key": None,
                    "salient": False,
                    "media": [],
                    # ラベルの型付け (label_kind 等) を知覚エントリへ写す —
                    # 未消費バッファの回収 (room_state_packages.md §11-2) が
                    # 移動通知をこの型で識別する。metadata の無いラベルは従来
                    # どおり None。
                    "metadata": (
                        json.dumps(label.metadata, ensure_ascii=False)
                        if label.metadata else None
                    ),
                },
            }
            for label in deliverable
        ]
        ledger.mark_applied(
            execution_id,
            result={"labels": len(deliverable), "sections": sorted(detected.keys())},
            outbox_items=outbox_items,
            deliver=True,
        )
    except Exception:
        # 配送予約に失敗 = 通知は届いていない。B は据え置き (次回 flush で再検出)。
        LOGGER.exception(
            "head_pipeline: failed to queue diff notifications via ledger "
            "persona=%s (labels left for retry)", ctx.persona_id,
        )
        return False

    # outbox 積みが durable に確定した後で B を前進 (S3 の修正)。検知した Section
    # は一律に進める — deliver=False のラベルしか出さない Section (部屋替え時の
    # 同席者) も、もう後段の処理を持たない (再会の想起は Pulse 頭の同席チェックへ
    # 移った、2026-09-07) ので、基準だけ進めて次の差分に備えればよい。
    for section_name, new_snapshot in detected.items():
        pipeline.advance_last_notified(ctx.persona_id, section_name, new_snapshot)

    LOGGER.info(
        "head_pipeline: queued %d world_state notification(s) via ledger "
        "for persona=%s building=%s", len(deliverable), ctx.persona_id, building_id,
    )

    return True


def _inject_diff_notifications_direct(
    persona: Any,
    pipeline: HeadPipeline,
    ctx: LineHeadInput,
    building_id: str,
    *,
    only_sections: set[str] | None = None,
) -> bool:
    """台帳が無い環境の degrade 経路 (配達保証なし)。

    台帳経路と同じく「検出 (advance=False) → push → 成功後に B 前進」の順で行う。
    旧実装は flush_diffs (advance=True) で先に B を進めてから SAIMemory readiness
    と push を確認していたため、未 ready / push 失敗で通知を捨てた後も B だけが
    進み、その差分は永久に再検出されなかった (Codex 2026-08-17 medium — C8 の
    「配送確定後の前進」違反)。失敗時は B と dirty を据え置き、次回 flush の
    再検出に委ねる (push 済みラベルの再通知はあり得る = at-least-once。台帳経路
    の再配送と同じ倒し方)。

    ``deliver=False`` のラベルは push の対象外 (基準の前進にだけ使う)。SAIMemory が
    未 ready の回は、届ける文の有無にかかわらず何も進めない — push 先が無い以上、
    次回の再検出でまとめてやり直す方が落としが無い。
    """
    labels, detected = pipeline.flush_diffs(
        ctx, all_sections=True, advance=False, only=only_sections,
    )
    if not labels:
        return False

    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is None or not sai_mem.is_ready():
        LOGGER.debug(
            "head_pipeline: SAIMemory not ready, %d notification labels deferred "
            "(baseline kept for re-detection)",
            len(labels),
        )
        return False

    deliverable = [label for label in labels if label.deliver]
    push_failed = False
    for label in deliverable:
        try:
            # 台帳経路と同じく、ラベルの型付け (label_kind 等) を知覚エントリへ
            # 写す (room_state_packages.md §11-3-2)。
            sai_mem.push_perception(
                "world_state", label.label,
                metadata=(
                    json.dumps(label.metadata, ensure_ascii=False)
                    if label.metadata else None
                ),
            )
        except Exception:
            push_failed = True
            LOGGER.exception(
                "head_pipeline: push_perception failed for world_state label",
            )
    if push_failed:
        # 一部でも失敗したら B を進めない — 次回 flush で全ラベル再検出される。
        return False

    for section_name, new_snapshot in detected.items():
        pipeline.advance_last_notified(ctx.persona_id, section_name, new_snapshot)

    LOGGER.info(
        "head_pipeline: pushed %d world_state perception(s) for persona=%s building=%s",
        len(deliverable), ctx.persona_id, building_id,
    )

    return bool(deliverable)


# 「不在から同席へ変わった一回だけ想起を試みる」ための、プロセス内の記憶。
# persona_id → その相手と同席が続いている間に想起を**試み済み**の相手 ID の集合。
# 想起が実際に積まれたかではなく「試みたか」を覚える (門で抑制された回・想起本文が
# 空だった回・push が失敗した回も試み済み) — 再会は一度きりの出来事で、毎 Pulse
# 再試行に戻すと「同席している間じゅう毎 Pulse 想起が積まれる」欠陥が別の形で残る。
# 相手が部屋から居なくなった Pulse で集合から落ちるので、次の再会でまた発火する。
# プロセスを再起動するとこの記憶は消え、再起動後の最初の Pulse で門を通った相手に
# 想起が一回出る — 発火点を移す前の「入室ごとに一回」と同じ量なので受容する
# (docs/issues/perception_state_pushed_at_event_time.md 直し方 6)。
_copresence_recalled: dict[str, set[str]] = {}
_copresence_recalled_lock = threading.Lock()


def reset_copresence_recall_memory(persona_id: str | None = None) -> None:
    """同席想起の「試み済み」の記憶を捨てる (テストの相互汚染の掃除用)。

    ``persona_id`` を渡すとその 1 人ぶんだけ、省略すると全員ぶんを忘れる。
    忘れた後の最初の Pulse は、同席中の相手を新顔として扱う (= 想起を一回試みる)。
    """
    with _copresence_recalled_lock:
        if persona_id is None:
            _copresence_recalled.clear()
        else:
            _copresence_recalled.pop(str(persona_id), None)


def _take_copresence_newcomers(
    persona_id: str, occupant_ids: list[str],
) -> list[str]:
    """同席者のうち「不在から同席へ変わった相手」だけを返し、全員を試み済みにする。

    記憶に残すのは**いま同席している試み済みの相手だけ** — 居なくなった相手は
    集合から落ちるので、次に再会したときは新顔として扱われる (再武装)。
    戻り値は ``occupant_ids`` の順序を保つ。
    """
    with _copresence_recalled_lock:
        already = _copresence_recalled.get(persona_id) or set()
        current = set(occupant_ids)
        newcomers = [oid for oid in occupant_ids if oid not in already]
        # 新顔は「これから試みる」ぶんも含めて試み済みにする (門で抑制されても
        # push に失敗しても、この同席の間は再試行しない)。
        if current:
            _copresence_recalled[persona_id] = current
        else:
            _copresence_recalled.pop(persona_id, None)
    return newcomers


def inject_copresence_recall(
    persona: Any,
    manager: Any,
    building_id: str,
) -> None:
    """いま同席している相手との過去会話・Memopedia を知覚バッファへ push する。

    呼ばれるのは Pulse の頭ただ一箇所 (``sea.runtime.SEARuntime.
    _run_meta_user_locked`` の、建物発言の取り込みの直後・知覚の消費の直前)。
    「目を覚ましたときに隣に居る相手のことを思い出す」形で、``manager.occupants``
    の**いまの**顔ぶれを見る。

    【発火の規約】

    - **不在から同席へ変わった相手に一回だけ**試みる。
    - 同席が続いている間は再発火しない (門で抑制された回も、想起が空だった回も、
      push に失敗した回も「試み済み」— 再会は一度きりの出来事)。
    - 相手が部屋から居なくなると再武装され、次の再会でまた発火する。
    - プロセスを再起動するとこの記憶は消える (再起動後の最初の Pulse で同席中の
      相手に一回出る)。記憶の実体と受容の理由は :data:`_copresence_recalled`。

    【発火点を移した理由 (2026-09-07)】以前は入室の検知が出す
    kind=``occupant_entered`` のラベルを目印に、移動の瞬間に想起を積んでいた。
    移動の瞬間に積んだ想起は次の Pulse まで知覚バッファで待つので、その間に
    本人が別の部屋へ移ると「もう居ない相手との再会」を思い出す嘘になる
    (まはーの実機報告)。往復すればそのぶん想起が積み重なる欠陥も同じ根。
    Pulse の頭で発火させれば、同じ Pulse が同席者を見て同じ Pulse で消費するので
    どちらも構造的に消える (docs/issues/perception_state_pushed_at_event_time.md)。

    【縁の記憶が要る理由 (2026-09-07 の同日追記)】発火の条件が「同席している」と
    いう**状態**になったので、そのままだと同席中は毎 Pulse 門が開きうる (隣で
    黙っている相手は直近の文脈に痕跡が無いため門を通り続ける)。旧実装が
    「入室」という一回きりの出来事に紐づいていた性質を、上の「試み済み」の記憶で
    復元する。

    【積んだ想起に印を付ける (2026-09-07 の移行の掃除)】push する知覚の metadata
    に ``{"copresence": true, "occupant_id": "<相手 ID>"}`` を載せる。旧方式が
    移動の瞬間に積んだ想起は v0.3.9 までのユーザーの知覚バッファに未消費のまま
    残っていて、そのままだと次の Pulse で新方式の想起と二重に読まれる。印の無い
    ``persona_recall`` を回収 (``sai_memory.room_state.
    reclaim_pending_perceptions`` の §11-2 規則 3(c)) が遺物として捨てるので、
    掃除は再起動後の最初の消費で自動的に済む (手動の掃除は要らない)。

    Note システム完成までの繋ぎ実装であることは変わらない (配送保証は無い)。

    【再会の門 (2026-09-05, v0.3.9)】相手が直近の文脈に居るあいだは想起しない
    (:meth:`HistoryManager.should_recall_persona`)。この繋ぎ実装は門を呼ばないまま
    出荷されていたため、ずっと会話している相手にも移動のたびに「過去会話 6 件
    (各 2,000 字) + 相手の Memopedia 個人ページ全文」が積まれ、本番で知覚
    18 万字まで膨らんだ (docs/issues/archive/persona_recall_perception_unbounded.md)。

    見出しに書く相手の名前は ``persona.id_to_name_map`` (manager と参照を共有する
    id→表示名の対応) で解決して渡す。解決できないときだけ ID のままになる。

    SAIMemory が未 ready の回は静かに見送る — **記憶を触らずに**戻るので、次の
    Pulse の頭が同じ同席を新顔としてやり直す (何も失われない)。
    """
    history_manager = getattr(persona, "history_manager", None)
    if not history_manager or not building_id:
        return
    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is None or not sai_mem.is_ready():
        # 「試み済み」を刻まずに戻る — 刻むと、起動直後の未 ready の一回で
        # その同席まるごとの想起が永久に飛ぶ。
        LOGGER.debug(
            "head_pipeline: SAIMemory not ready, skipping copresence recall "
            "(the next pulse head checks the same room again)",
        )
        return

    id_to_name = getattr(persona, "id_to_name_map", None) or {}
    self_id = getattr(persona, "persona_id", None)
    occupants = list(getattr(manager, "occupants", {}).get(building_id, []) or [])
    # ペルソナか、ユーザーか。訪問中のペルソナも居るので all_personas を優先する
    # (無い manager では personas に degrade)。
    persona_ids = set(
        getattr(manager, "all_personas", None) or getattr(manager, "personas", {}) or {}
    )

    # 同席者一覧の ID は生の値 (ユーザーは数値のことがある)。旧実装は入室の
    # 検知 (capture) が文字列へ揃えた後の値を見ていたので、同じ揃えを通す —
    # 生のまま比較すると、門の照合 (履歴の with / audience は文字列) が
    # 型の違いで空振りして、想起が二重に出る。「試み済み」の記憶もこの揃えた
    # 形で持つ。
    occupant_ids: list[str] = []
    for raw_id in occupants:
        occupant_id = str(raw_id) if raw_id is not None else ""
        if not occupant_id or occupant_id == str(self_id):
            continue
        occupant_ids.append(occupant_id)

    # 想起を試みるのは「不在から同席へ変わった相手」だけ。ここで全員が試み済みに
    # なる (下の門・想起・push の結果は問わない)。
    newcomers = _take_copresence_newcomers(str(self_id or ""), occupant_ids)
    if not newcomers:
        return

    from sai_memory.room_state import (
        RECALL_COPRESENCE_META_KEY,
        RECALL_OCCUPANT_META_KEY,
    )

    for occupant_id in newcomers:
        # ユーザーも対ペルソナと同様に想起する (まはー裁定 2026-07-11)。
        # 従来はユーザーページの肥大化に打つ手が無く persona 限定だったが、
        # 編纂の分割 (P4-a) が肥大を受けられるようになったため前提が変わった。
        # 過去会話の検索 (audience) も個人ページの解決 (metadata.persona_id) も
        # ユーザー ID でそのまま機能する。
        occupant_kind = "persona" if occupant_id in persona_ids else "user"

        if not _should_recall_on_enter(history_manager, occupant_id, occupant_kind):
            LOGGER.debug(
                "head_pipeline: skipped persona recall for %s "
                "(already in recent context)", occupant_id,
            )
            continue

        try:
            recall_text = history_manager.recall_conversation_with(
                occupant_id,
                current_thread_only=False,
                max_results=6,
                display_name=id_to_name.get(str(occupant_id)) or None,
            )
        except Exception:
            LOGGER.exception(
                "head_pipeline: recall_conversation_with failed for %s", occupant_id,
            )
            continue
        if not recall_text:
            continue

        try:
            # 同席の印を metadata に刻む。未消費バッファの回収
            # (sai_memory/room_state.reclaim_pending_perceptions — §11-2 規則
            # 3(c)) は、この印が無い persona_recall を旧方式 (移動の瞬間に積む、
            # 2026-09-07 退役) の遺物として捨てる。相手の ID は診断とプレビュー
            # のために併記する。
            sai_mem.push_perception(
                "persona_recall", recall_text,
                metadata=json.dumps(
                    {
                        RECALL_COPRESENCE_META_KEY: True,
                        RECALL_OCCUPANT_META_KEY: occupant_id,
                    },
                    ensure_ascii=False,
                ),
            )
            LOGGER.info(
                "head_pipeline: pushed persona recall perception for %s "
                "(copresent at the pulse head)", occupant_id,
            )
        except Exception:
            LOGGER.exception(
                "head_pipeline: failed to push persona recall for %s", occupant_id,
            )


def _should_recall_on_enter(
    history_manager: Any, occupant_id: Any, occupant_kind: Any = None
) -> bool:
    """再会の門。直近の文脈に相手が居るなら想起しない。

    判定の本体は :meth:`HistoryManager.should_recall_persona` (直近 20 メッセージに
    相手の痕跡 — metadata.with / audience / persona_id — があれば False)。ここは
    繋ぎ実装からその門へ配線するだけ。``occupant_kind`` は同席者の種別
    "persona" | "user" — ユーザー発言は id を持たない形 (with=["user"]) で
    履歴に刻まれるため、ユーザー相手の照合には種別が要る (2026-09-06)。

    判定自体が失敗したときは想起する側に倒す (= 従来挙動)。門は想起の量を抑える
    最適化で、想起そのものが機能 — 判定の故障で再会の記憶を静かに失わせるより、
    例外を記録したうえで従来どおり積む方が損失が小さい。
    """
    try:
        return bool(
            history_manager.should_recall_persona(occupant_id, target_kind=occupant_kind)
        )
    except Exception:
        LOGGER.exception(
            "head_pipeline: should_recall_persona failed for %s "
            "(falling back to recall)", occupant_id,
        )
        return True


def _detect_room_state_changes(
    persona: Any, manager: Any, building_id: str,
    *, model_key: Optional[str] = None,
) -> None:
    """滞在中の「部屋の様子」のパッケージ照合と自己回復 (room_state_packages.md §6-2/§6-3)。

    検知の瞬間 (Pulse 開始・入室時の居合わせ側・Beat 頭の MCP 変動) に今の部屋の
    パッケージの束を組み、**提示と同じ窓で見えている**最新の束と指紋で
    突き合わせる (2026-09-06 八巡目修正 1 — 窓の外の束を「前回」に拾うと、
    窓の中に残る古い提示との差を「変化なし」と誤読して覆い隠す):

    - 変わっていれば diff を push (末尾 = 出来事)。これが旧 BuildingItemsSection
      のアイテム差分ラベル (「追加されました」— 中身を運ばない) の後継で、
      部屋の差分機構はこの照合一本 (アイテムの中身・絵ごと届く)。
    - 部屋の様子が提示のどこにも見えなければ、全文を提示の最古端へ置き直す
      (自己回復 — 先頭 = 背景)。head の部屋描画の退役後、起動直後の
      ブートストラップ (§6-3) と Chronicle 無効ペルソナの窓絞り、想定外の穴
      すべての受け皿がここ。

    ``model_key`` は**その回の実行 model** — Chronicle 無効ペルソナの窓判定は
    (ペルソナ, model) ごとの提示窓で行うので、提示側と同じ model の窓を見ない
    と「提示では部屋が窓の外なのに自己回復が発火しない」がねじれる。None は
    標準 model (``persona.model``) の窓。

    world の読み (開いている文書の読み込み込み) を伴うので、失敗はすべて
    WARN + 続行 (呼び出し側で包む)。ペルソナが ``building_id`` に居ない回
    (照合対象の部屋が現在地でない) は何もしない。束の組成の間に別スレッド
    (ユーザー操作の移動 API 等) が現在地を変えた回も、積む直前の再確認で
    見送る (次の検知が現在地でやり直す) — 競合の窓は「再確認から DB 書き込み
    まで」に縮むがゼロにはならない。深い対処 (移動の冪等キー・行き先照合) は
    docs/issues/entry_delivery_retry_duplicates_room_perception.md の修正方向に
    合流する。提示窓が解決できない回
    (:class:`~sai_memory.perception_buffer.WindowResolutionError`) は部屋の
    判定そのものを WARN つきで見送る — 窓の三値 (窓なし / 窓キー / 解決失敗)
    の「解決失敗」で、抑止でもループでもない (2026-09-06 四巡目修正 2)。床の
    予算 (:func:`_presentation_floor_chars`) は「anchor の行が読めないときの
    代替」の材料なので、ここでは解決せず遅延の口で渡す — 床の一時失敗が
    「解決失敗」になるのは、anchor が読めず床が実際に要る回だけ (2026-09-06
    五巡目修正 2)。
    """
    if getattr(persona, "current_building_id", None) != building_id:
        return
    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is None or not getattr(sai_mem, "is_ready", lambda: False)():
        return

    from builtin_data.tools.get_visual_context import build_room_bundle
    from tools.context import persona_context

    persona_id = getattr(persona, "persona_id", None)
    persona_dir = getattr(persona, "persona_dir", None)
    if not persona_id:
        return
    with persona_context(persona_id, persona_dir, manager):
        bundle = build_room_bundle(building_id)
    if not bundle:
        return

    from sai_memory.perception_buffer import WindowResolutionError
    from sai_memory.room_state import room_key, snapshot_digest

    key = room_key(building_id)
    chronicle_on = _room_chronicle_enabled(persona, manager)
    # 提示窓 (anchor) を持つのは Chronicle 無効ペルソナだけ。検知の読み
    # (latest_room_snapshot) と置き直し (reseat_room_state) の**両方**に同じ
    # 篩を渡す — 検知の読みが窓を通らないと、窓の外の最新束を「前回」に拾って
    # 「変化なし」と誤判定し、窓の中に残る古い提示を差分も置き直しも来ないまま
    # 覆い隠す (2026-09-06 八巡目修正 1: digest 比較の「前回」と運搬役判定は
    # 同じ窓付きの読み一本 — None = 窓に束が無い = 運搬役なし)。置き直しの
    # 内側の門 (「運搬役が生きているか」) にも同じ篩 (2026-09-06 二巡目修正 1)。
    # 床の予算 (floor_chars) は anchor の行が読めない劣化時のフォールバック
    # 入力 — 提示の最小ロードと同じ値を渡し、起点の解決は resolve_window_key
    # の一枚で提示側と揃える (2026-09-06 三巡目 #1: ここが無いと劣化時に検知
    # だけ「全部見える」扱いで割れ、自己回復が抑止される)。床はここでは解決
    # せず**遅延の口 (callable)** で渡す — resolve_window_key が anchor の行を
    # 読めなかった回だけ呼ばれる (2026-09-06 五巡目修正 2: 先に無条件で解決
    # すると、床の一時失敗が正当な anchor で判定できる回まで「窓の解決失敗」に
    # 巻き込み、自己回復を不要にスキップしていた)。窓が解決できない回
    # (WindowResolutionError) は部屋の判定そのものを見送る — 「全部見える」
    # (抑止) にも「全部見えない」(毎検知の置き直しループ) にも倒さず、次の
    # 検知でやり直す (2026-09-06 四巡目修正 2)。
    anchor_id = None
    floor_chars = None
    if not chronicle_on:
        anchor_id = _presentation_anchor_id(persona, manager, model_key)
        floor_chars = partial(
            _presentation_floor_chars, persona, manager, model_key,
        )
    try:
        latest = sai_mem.latest_room_snapshot(
            key, anchor_id=anchor_id, floor_chars=floor_chars,
        )
    except WindowResolutionError:
        LOGGER.warning(
            "head_pipeline: the presentation window could not be resolved "
            "(room detection read); skipping the room judgment this round "
            "persona=%s building=%s (the next detection will retry)",
            persona_id, building_id, exc_info=True,
        )
        return
    # 積む直前の現在地の再確認 — 冒頭の確認から束の組成 (world の読み) を
    # 挟んで時間が経っており、その間に別スレッドの移動で現在地が変わって
    # いたら、この束はもう「現在の知覚」ではない。見送れば次の検知が現在地で
    # 正しくやり直す (docstring の競合の窓の注記を参照)。
    if getattr(persona, "current_building_id", None) != building_id:
        LOGGER.warning(
            "head_pipeline: persona moved during room-bundle composition "
            "(persona=%s composed_for=%s now_at=%s); skipping the room "
            "push/reseat this round (the next detection redoes it at the "
            "current location)",
            persona_id, building_id,
            getattr(persona, "current_building_id", None),
        )
        return
    if latest is None:
        # 窓に見える束が一枚も無い = 提示に部屋が無い (ブートストラップと、
        # Chronicle 無効の窓絞りで運搬役が落ちた形を含む)。窓が解決できない
        # 回は上の見送りで既に抜けている。
        sai_mem.reseat_room_state(
            bundle, anchor_id=anchor_id, floor_chars=floor_chars,
        )
        return
    if snapshot_digest(latest) != snapshot_digest(bundle):
        # 変化は出来事 — 末尾へ束を積む。描画 (差分 / Chronicle 無効は毎回
        # 全文) は消費の組成の一回だけ (room_state_packages.md §11-2 規則 2)。
        sai_mem.push_room_state(building_id, bundle, allow_diff=chronicle_on)


def _room_chronicle_enabled(persona: Any, manager: Any) -> bool:
    """Chronicle 編纂が有効か (積む側の門と同じ読み — 判定不能なら有効側)。"""
    try:
        from saiverse.dynamic_state import _chronicle_enabled
        return _chronicle_enabled(persona, manager)
    except Exception:
        return True


def _presentation_anchor_id(
    persona: Any, manager: Any, model_key: Optional[str] = None,
) -> Optional[str]:
    """(ペルソナ, 実行 model) の提示窓の起点 (anchor)。読めなければ None。

    Chronicle 無効ペルソナの窓絞りの判定に使う。窓は (ペルソナ, model) ごと
    なので、``model_key`` にはその回の実行 model を渡す (None は標準 model =
    ``persona.model`` の窓)。``persist_advance=False`` は読みだけの解決
    (keepalive / preview と同じ口) — 行は触らない。
    """
    try:
        runtime = (
            getattr(manager, "sea_runtime", None)
            or getattr(manager, "runtime", None)
        )
        lifecycle = getattr(runtime, "session_lifecycle", None)
        if lifecycle is None:
            return None
        anchor_id, _resolution = lifecycle.resolve_metabolism_anchor(
            persona, model_key=model_key, persist_advance=False,
        )
        return anchor_id
    except Exception:
        LOGGER.debug(
            "head_pipeline: could not resolve the presentation anchor for the "
            "room visibility check", exc_info=True,
        )
        return None


def _presentation_floor_chars(
    persona: Any, manager: Any, model_key: Optional[str] = None,
) -> int:
    """anchor が読めない劣化時に窓の床を近似する予算 (文字数)。

    値は提示の最小ロード (sea/runtime_context._minimal_load_chars) と同じ —
    anchor の行が引けないとき、提示の組成は recent (最小ロードで読んだ生ログ)
    の最古行を床にする。検知は recent を持たないので、同じ予算を
    :func:`sai_memory.perception_buffer.resolve_window_key` へ渡し、messages の
    末尾から同じ量ぶんの最古行を床にする (窓の近似 — 提示より見えない側に
    倒れる)。

    検知はこれを**遅延の口 (partial) で渡し**、resolve_window_key が anchor の
    行を読めなかった回だけ呼ばれる — 床は anchor の代替の材料なので、正当な
    anchor で判定できる回をここの一時失敗に巻き込まない (2026-09-06 五巡目
    修正 2)。

    **解決に失敗したら**
    :class:`~sai_memory.perception_buffer.WindowResolutionError` を送出する
    (2026-09-06 四巡目修正 2)。旧実装の None (= 床なし = 窓なし・全件可視) は、
    床が要る劣化の回に検知だけ「全部見える」へ倒れて自己回復を抑止した —
    予算が分からない回は窓が解決できない回で、呼び出し側は自己回復の判定
    そのものを見送る。
    """
    from sai_memory.perception_buffer import WindowResolutionError

    try:
        from sea.runtime_context import _minimal_load_chars

        runtime = (
            getattr(manager, "sea_runtime", None)
            or getattr(manager, "runtime", None)
        )
        chars = (
            int(_minimal_load_chars(runtime, persona, model_key))
            if runtime is not None else None
        )
    except Exception as exc:
        raise WindowResolutionError(
            "could not resolve the window floor budget for the room "
            "visibility check"
        ) from exc
    if chars is None:
        raise WindowResolutionError(
            "the manager has no runtime to resolve the window floor budget from"
        )
    return chars


def _missing_section_names(pipeline: HeadPipeline, snapshot) -> set[str]:
    """登録済み Section のうち snapshot に実体値が無い名前の集合。

    value が ``None`` の Section は欠損として数える (W6 / SEA 監査 S6 —
    旧実装は key 集合しか見ず、capture 失敗の None を「存在する」と誤認して
    再 capture しなかった)。Section.capture が None を返す正規経路は存在しない
    (全 Section が空でも snapshot dataclass を返す) ため、None = 失敗痕跡。
    """
    expected_names = {s.name for s in pipeline.registry.all_sections()}
    sections = snapshot.sections if snapshot is not None else {}
    actual_names = {name for name, value in sections.items() if value is not None}
    return expected_names - actual_names


def ensure_snapshot(pipeline: HeadPipeline, ctx: LineHeadInput) -> None:
    """pipeline に snapshot が無ければ store から load、それでも無ければ capture_all。

    load_from_store が成功しても、登録済み Section のうち snapshot.sections に
    実体値が無いものがあれば (= 旧 schema 等で deserialize が失敗した / 前回の
    capture が失敗した) 自己修復で **欠損分だけ** recapture_missing を走らせて
    埋める (capture_all で全再構築すると、1 Section の持続故障が毎 Pulse の全
    head 再構築 = cache 破壊 + B リセットに化ける — Codex P2)。この再 capture が
    「復旧後の再試行」の実体 — 失敗が続く限り毎回試み、直れば次の Pulse から
    head が揃う。

    加えて、 ctx に anchor TTL 状態 (= ``anchor_updated_at`` + ``cache_ttl_seconds``)
    が積まれていて TTL を超過していたら、 snapshot 全体を再 capture する。
    prompt cache TTL が切れたタイミングでは「head 不変による cache hit」 の根拠が
    消えるので、 cache hit を諦めて最新状態を反映する方が情報量で勝る。
    TTL 再 capture は head を最新化するだけで、通知の既読基準 (last_notified) には
    触らない — 休止中に起きた差分 (入退室等) の通知は直後の flush_diffs に委ねる
    (2026-08-17 実バグの修正: 撮り直しが基準を上書きし、入退室通知が届かないまま
    既読化されていた。capture_all docstring の不変条件参照)。
    """
    if pipeline.has_snapshot(ctx.persona_id, ctx.model_key):
        snapshot = pipeline.get_snapshot(ctx.persona_id, ctx.model_key)
        if _is_anchor_ttl_expired(ctx):
            LOGGER.info(
                "head_pipeline: anchor TTL expired (in-memory snapshot), recapturing persona=%s model=%s",
                ctx.persona_id, ctx.model_key,
            )
            # capture_all は B (通知の既読基準) に触らない — 休止中の入退室等の
            # 差分は直後の flush_diffs が届ける (2026-08-17 実バグの修正、
            # capture_all docstring の不変条件参照)。
            pipeline.capture_all(ctx)
            return
        missing = _missing_section_names(pipeline, snapshot)
        if missing:
            pipeline.recapture_missing(ctx, missing)
        return
    if pipeline.load_from_store(ctx.persona_id, ctx.model_key):
        snapshot = pipeline.get_snapshot(ctx.persona_id, ctx.model_key)
        if _is_anchor_ttl_expired(ctx):
            LOGGER.info(
                "head_pipeline: anchor TTL expired (loaded snapshot), recapturing persona=%s model=%s",
                ctx.persona_id, ctx.model_key,
            )
            # 上の in-memory 分岐と同じ — 再起動直後でも store から復元した B を
            # 基準に、休止中の差分を次の flush で届ける。
            pipeline.capture_all(ctx)
            return
        missing = _missing_section_names(pipeline, snapshot)
        if missing:
            pipeline.recapture_missing(ctx, missing)
        return
    pipeline.capture_all(ctx)


def _is_anchor_ttl_expired(ctx: LineHeadInput) -> bool:
    """``anchor_updated_at + cache_ttl_seconds < now`` なら True。

    どちらか None なら判定スキップ (False)。 anchor は LLM 呼び出し成功時にのみ
    touch されるため、 anchor.updated_at が prompt cache 書き込みの真の起点。
    判定で True を返した場合、 呼び出し側は snapshot を全 Section 再 capture する。
    """
    if ctx.anchor_updated_at is None or ctx.cache_ttl_seconds is None:
        return False
    elapsed = time.time() - ctx.anchor_updated_at
    return elapsed > ctx.cache_ttl_seconds


def render_head_messages(
    persona: Any,
    manager: Any,
    building_id: str,
    *,
    enabled_sections: set[str] | None = None,
    pipeline: HeadPipeline | None = None,
    model_key: str | None = None,
) -> list[dict[str, Any]]:
    """pipeline 経由で head の message 列を組み立てる。

    snapshot 不在なら自動で capture_all する (= 初回呼び出し / 再起動後)。
    snapshot が既にあれば render するだけで cache 安定する。

    ``model_key`` はこの head を届ける Session (persona, model) の model
    (beat_execution_context.md §3.1)。ExecutionContext が届いている呼び出し元は
    ``execution_context.model_key`` を渡す。None なら persona の標準 model。

    ``enabled_sections`` を渡すと、その名前の Section のみを composition 対象に
    する (= 旧 ``reqs.system_prompt`` / ``reqs.memory_weave`` / ``reqs.visual_context``
    フラグ相当)。``None`` なら全 Section を render する。

    fail-closed (W6 / SEA 監査 S6): enabled に含まれる required Section
    (common_prompt / persona_self / core_memory) について、capture 失敗による
    欠損・render 失敗・snapshot の永続化失敗が残っている場合は
    :class:`HeadNotReadyError` を投げ、呼び出し側 (prepare_context) 経由で
    LLM 実行前に Pulse を中断させる。人格に属さない発話を本人履歴へ確定させる
    より、正直に失敗して次 Pulse の自己修復 (ensure_snapshot 再 capture /
    ensure_persisted 再保存) に委ねる。

    戻り値は ``[{"role": ..., "content": ..., "metadata": ...}, ...]`` の標準
    message dict 列。``prepare_context`` の system / memory_weave 部分の
    置き換えとして使う。
    """
    from sea.head_pipeline.types import HeadNotReadyError

    pipeline = pipeline or get_default_pipeline()
    ctx = build_line_head_input(persona, manager, building_id, model_key=model_key)
    ensure_snapshot(pipeline, ctx)

    # 以降の readiness 検証・render・composition は全てこの pin した snapshot
    # に対して行う (Codex 二巡 P1): 検証と render の間に別スレッドが未保存の
    # 新版を公開しても、「検証した版」を描画するので fail-closed が崩れない。
    pinned = pipeline.get_snapshot(ctx.persona_id, ctx.model_key)

    required_names = pipeline.registry.required_section_names()
    if enabled_sections is not None:
        required_names = required_names & enabled_sections

    if required_names:
        # 1) capture readiness: required Section が snapshot に実体値を持つか。
        #    ensure_snapshot が再 capture を済ませた後なので、ここで欠けている
        #    ものは「今回も capture に失敗した」Section。
        sections = pinned.sections if pinned is not None else {}
        failures = pinned.capture_failures if pinned is not None else {}
        missing = {
            name: failures.get(name, "section missing from snapshot")
            for name in required_names
            if sections.get(name) is None
        }
        if missing:
            raise HeadNotReadyError(ctx.persona_id, ctx.model_key, "capture", missing)

        # 2) persist readiness: pin した版までの保存が未確認なら再試行し、
        #    それでもダメなら止める (restart で旧 head に黙ってロールバック
        #    する状態のまま応答を確定させない)。
        pinned_version = pinned.snapshot_version if pinned is not None else 0
        if not pipeline.ensure_persisted(
            ctx.persona_id, ctx.model_key, min_version=pinned_version,
        ):
            raise HeadNotReadyError(
                ctx.persona_id, ctx.model_key, "persist",
                {"__snapshot__": "store.save failing (will retry next pulse)"},
            )

    # render_head は (section_name, RenderedSection) を返すので、名前で直接
    # enabled フィルタできる。位置依存の zip は廃止 (None render セクションで
    # 名前がズレて内容が欠落するバグの根治)。required Section の render 例外は
    # HeadNotReadyError (stage="render") として上がってくる。
    rendered = pipeline.render_head(
        ctx.persona_id, ctx.model_key,
        fail_closed_sections=required_names, snapshot=pinned,
    )
    rendered_by_name = {
        name: section
        for name, section in rendered
        if enabled_sections is None or name in enabled_sections
    }
    return _compose_messages(pinned, rendered_by_name)


def _compose_messages(
    snapshot: Any,
    rendered_by_name: dict[str, RenderedSection],
) -> list[dict[str, Any]]:
    # ``snapshot`` は呼び出し側が pin した LineHeadSnapshot (None 可)。
    # Memory Weave の entries 展開もこの pin から読む — render と別の版を
    # 読まない (Codex 二巡 P1 と同じ版一致の原則)。
    messages: list[dict[str, Any]] = []

    system_parts: list[str] = []
    for name in SYSTEM_PROMPT_SECTION_NAMES:
        rendered = rendered_by_name.get(name)
        if rendered is None or not rendered.text:
            continue
        system_parts.append(rendered.text)
    if system_parts:
        messages.append({
            "role": "system",
            "content": "\n\n---\n\n".join(system_parts),
        })

    # Memory Weave: snapshot の entry を種類ごとに個別 user message として展開
    # する。preview UI は metadata.__memory_weave_type__ で section ラベルを
    # 切り替えるため、1 つにまとめない (現存する種類は chronicle のみ)。
    if MEMORY_WEAVE_SECTION_NAME in rendered_by_name:
        mw_section_snapshot = (
            snapshot.sections.get(MEMORY_WEAVE_SECTION_NAME)
            if snapshot is not None else None
        )
        entries = getattr(mw_section_snapshot, "entries", None) or ()
        for entry in entries:
            content = getattr(entry, "content", None)
            kind = getattr(entry, "kind", None)
            if not content or not kind:
                continue
            messages.append({
                "role": "user",
                "content": content,
                "metadata": {
                    _MEMORY_WEAVE_CONTEXT_MARKER: True,
                    _MEMORY_WEAVE_TYPE_KEY: kind,
                },
            })

    # 部屋の描画 (旧 visual_context Section) は head から退役した (2026-09-06)。
    # 部屋の様子は知覚 (tail) が運ぶ — docs/intent/room_state_packages.md §2。

    return messages
