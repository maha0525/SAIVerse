"""Cached Head Architecture: runtime_context への統合層。

`prepare_context` から呼ばれる thin adapter。LineHeadInput を組み立てて
pipeline.render_head の RenderedSection 列を取得し、それを LLM に渡す
message dict 列 (system + user / media 含む) に composition する。

Section 群と message role / metadata の対応はこの層で握る:
- ``common_prompt`` / ``persona_self`` / ``building`` / ``available_playbooks`` /
  ``spell_list``: text-only、まとめて 1 つの system message にする
- ``memory_weave``: text-only、独立した user role message にする (旧 get_memory_weave_context 経路と互換)
- ``visual_context``: text + media、独立した user role message + metadata.media +
  ``__visual_context__`` marker を保持 (旧 get_visual_context 経路と互換)

詳細: docs/intent/cached_head_architecture.md §3.5 / §5
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from sea.head_pipeline.pipeline import HeadPipeline, get_default_pipeline
from sea.head_pipeline.types import LineHeadInput, NotificationLabel, RenderedSection

# 旧 builtin_data/tools/get_memory_weave_context.py が message metadata に付与していた
# marker / type field 名と互換のキーを composition で再現する (= preview UI の
# section 識別ロジックがこれらを参照する: sea/runtime_context.py:618 周辺)。
_MEMORY_WEAVE_CONTEXT_MARKER = "__memory_weave_context__"
_MEMORY_WEAVE_TYPE_KEY = "__memory_weave_type__"
_VISUAL_CONTEXT_MARKER = "__visual_context__"

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
VISUAL_CONTEXT_SECTION_NAME = "visual_context"

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


def current_head_room(
    persona: Any,
    *,
    model_key: Optional[str] = None,
    pipeline: HeadPipeline | None = None,
) -> tuple[Optional[str], str]:
    """いま head が見せている部屋を ``(building_id, 知覚記法の全文)`` で返す。

    **撮り直さない読み口** — snapshot が無ければ ``(None, "")``。ここで
    ``ensure_snapshot`` を呼ぶと、入室処理や勘定のついでに head を撮り直す
    副作用が生まれる (「凍結された文脈の頭」の前提が崩れる)。

    head の visual_context は移動では撮り直されない (Metabolism / anchor TTL
    切れのみ) ので、返る building_id はペルソナの現在地とは限らない — **それが
    この読み口の要点**で、呼び出し側は「head が今いる部屋を見せているか」を
    この値で判定する:

    - 積む側 (saiverse/dynamic_state.py): 見せていれば、その姿を土台にした差分
      だけを積む (head と知覚が同じ部屋の全文を二枚見せない)。
    - 提示側 (sea/runtime_context.py): 見せていなければ、head を土台にした
      過去の差分を全文へ開き直す (部屋の全体像がどこにも無い状態を作らない)。

    ``model_key`` を省略するとペルソナの標準 model の head を見る。

    **今の snapshot を読む口**なので、「実際に送る head」を既に確定させている
    呼び出し (prepare_context) はこれを使わない — :func:`head_room_of_snapshot`
    にその確定済み snapshot を渡す (読み直すと、確定と読みの間に走った
    Metabolism / TTL の撮り直しで別の head を見てしまう)。
    """
    persona_id = getattr(persona, "persona_id", "") or ""
    if not persona_id:
        return (None, "")
    try:
        pipeline = pipeline or get_default_pipeline()
        key = str(model_key) if model_key else resolve_default_model_key(persona)
        return head_room_of_snapshot(pipeline.get_snapshot(persona_id, key))
    except Exception:
        LOGGER.warning(
            "head_pipeline: could not read the head's current room persona=%s",
            persona_id, exc_info=True,
        )
        return (None, "")


def head_room_of_snapshot(snapshot: Any) -> tuple[Optional[str], str]:
    """**この** snapshot が見せている部屋を ``(building_id, 知覚記法の全文)`` で返す。

    :func:`current_head_room` (今の snapshot を引いてくる口) と、render で
    pin した snapshot を持っている呼び出し (:func:`render_head_messages` の
    ``head_room_out``) が同じ規則を共有するための一点。判定を二枚書くと、
    「送る head」と「判定に使う head」で規則がずれる。
    """
    section = (
        snapshot.sections.get(VISUAL_CONTEXT_SECTION_NAME)
        if snapshot is not None else None
    )
    if section is None:
        return (None, "")
    building_id = getattr(section, "building_id", None)
    room_text = getattr(section, "room_text", "") or ""
    # **両方揃って初めて「見せている」**。head の本文が空なら描画されない
    # (= 部屋の全体像は head に無い) し、知覚記法の姿が無ければ土台にできる
    # ものが無い。どちらか欠けたら「見せていない」に倒す — 積む側は全文を
    # 積み、提示側は head 土台の差分を全文へ開き直す (どちらも安全側)。
    if not building_id or not room_text or not getattr(section, "text", ""):
        return (None, "")
    return (str(building_id), str(room_text))


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
) -> bool:
    """全 Section の diff を検知し、知覚バッファへ型付き項目として push する (消費はしない)。

    【知覚バッファ経由に変更 (2026-07-09, Phase 2)】以前は差分を直接 SAIMemory へ
    append していたが、これは「検知＝消費」を癒着させ、pulse 前のプレビューを不可能に
    していた。本関数は **検知器** に徹し、差分を知覚バッファ (kind='world_state') へ
    push するだけにする。実際に SAIMemory へ入る (= ペルソナが知覚する) のは呼び出し元
    が ``flush_perception_buffer`` を呼ぶ消費時。呼び出し元ごとの flush 制御:
    - Pulse 開始 (run_meta_user): push → 末尾で flush (同 Pulse で消費)。
    - Pulse 中の metabolism 直後 (runtime_context): push → 直後に flush (同ターン知覚)。
    - 移動時 (on_building_entered, pulse 外): push のみ。次の Pulse で消費される
      (= 主観時間が止まっている間の知覚は詰まって待つ、という時間モデル通り)。

    入室想起 (persona_recall) も同じくバッファへ push する。詳細:
    docs/intent/perception_buffer.md §4.5 / §5.1。

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

    ledger = getattr(manager, "execution_ledger", None)
    if ledger is None:
        return _inject_diff_notifications_direct(persona, pipeline, ctx, building_id)

    labels, detected = pipeline.flush_diffs(ctx, all_sections=True, advance=False)
    if not labels:
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
                    "metadata": None,
                },
            }
            for label in labels
        ]
        ledger.mark_applied(
            execution_id,
            result={"labels": len(labels), "sections": sorted(detected.keys())},
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

    # outbox 積みが durable に確定した後で B を前進 (S3 の修正)。
    for section_name, new_snapshot in detected.items():
        pipeline.advance_last_notified(ctx.persona_id, section_name, new_snapshot)

    LOGGER.info(
        "head_pipeline: queued %d world_state notification(s) via ledger "
        "for persona=%s building=%s", len(labels), ctx.persona_id, building_id,
    )

    # 入室想起は head 操作でも diff でもないので outbox 化の対象外 (従来どおり)。
    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is not None and sai_mem.is_ready():
        _inject_persona_recall_on_enter(persona, labels, sai_mem)

    return True


def _inject_diff_notifications_direct(
    persona: Any,
    pipeline: HeadPipeline,
    ctx: LineHeadInput,
    building_id: str,
) -> bool:
    """台帳が無い環境の degrade 経路 (配達保証なし)。

    台帳経路と同じく「検出 (advance=False) → push → 成功後に B 前進」の順で行う。
    旧実装は flush_diffs (advance=True) で先に B を進めてから SAIMemory readiness
    と push を確認していたため、未 ready / push 失敗で通知を捨てた後も B だけが
    進み、その差分は永久に再検出されなかった (Codex 2026-08-17 medium — C8 の
    「配送確定後の前進」違反)。失敗時は B と dirty を据え置き、次回 flush の
    再検出に委ねる (push 済みラベルの再通知はあり得る = at-least-once。台帳経路
    の再配送と同じ倒し方)。
    """
    labels, detected = pipeline.flush_diffs(ctx, all_sections=True, advance=False)
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

    push_failed = False
    for label in labels:
        try:
            sai_mem.push_perception("world_state", label.label)
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
        len(labels), ctx.persona_id, building_id,
    )

    _inject_persona_recall_on_enter(persona, labels, sai_mem)

    return True


def _inject_persona_recall_on_enter(
    persona: Any,
    labels: list[NotificationLabel],
    sai_mem: Any,
) -> None:
    """occupant_entered ラベルに対応する過去会話・Memopedia を知覚バッファへ push する。

    Note システム完成までの繋ぎ実装。ペルソナが同じ Building に入室した際、過去の会話と
    Memopedia ページを想起する。以前は直接 SAIMemory へ append していたが、Phase 2 で
    知覚バッファ (kind='persona_recall') への push に変更 (消費は呼び出し元の flush)。

    【再会の門 (2026-09-05, v0.3.9)】相手が直近の文脈に居るあいだは想起しない
    (:meth:`HistoryManager.should_recall_persona`)。この繋ぎ実装は門を呼ばないまま
    出荷されていたため、ずっと会話している相手にも移動のたびに「過去会話 6 件
    (各 2,000 字) + 相手の Memopedia 個人ページ全文」が積まれ、本番で知覚
    18 万字まで膨らんだ (docs/issues/persona_recall_perception_unbounded.md)。

    見出しに書く相手の名前は ``persona.id_to_name_map`` (manager と参照を共有する
    id→表示名の対応) で解決して渡す。解決できないときだけ ID のままになる。
    """
    history_manager = getattr(persona, "history_manager", None)
    if not history_manager:
        return

    id_to_name = getattr(persona, "id_to_name_map", None) or {}

    for label in labels:
        if label.kind != "occupant_entered":
            continue
        occupant_id = label.metadata.get("occupant_id")
        occupant_kind = label.metadata.get("occupant_kind")
        # ユーザーも対ペルソナと同様に想起する (まはー裁定 2026-07-11)。
        # 従来はユーザーページの肥大化に打つ手が無く persona 限定だったが、
        # 編纂の分割 (P4-a) が肥大を受けられるようになったため前提が変わった。
        # 過去会話の検索 (audience) も個人ページの解決 (metadata.persona_id) も
        # ユーザー ID でそのまま機能する。
        if not occupant_id or occupant_kind not in ("persona", "user"):
            continue

        if not _should_recall_on_enter(history_manager, occupant_id):
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
            sai_mem.push_perception("persona_recall", recall_text)
            LOGGER.info(
                "head_pipeline: pushed persona recall perception for %s on occupant_entered",
                occupant_id,
            )
        except Exception:
            LOGGER.exception(
                "head_pipeline: failed to push persona recall for %s", occupant_id,
            )


def _should_recall_on_enter(history_manager: Any, occupant_id: Any) -> bool:
    """再会の門。直近の文脈に相手が居るなら想起しない。

    判定の本体は :meth:`HistoryManager.should_recall_persona` (直近 20 メッセージに
    相手の発言か audience があれば False)。ここは繋ぎ実装からその門へ配線するだけ。

    判定自体が失敗したときは想起する側に倒す (= 従来挙動)。門は想起の量を抑える
    最適化で、想起そのものが機能 — 判定の故障で再会の記憶を静かに失わせるより、
    例外を記録したうえで従来どおり積む方が損失が小さい。
    """
    try:
        return bool(history_manager.should_recall_persona(occupant_id))
    except Exception:
        LOGGER.exception(
            "head_pipeline: should_recall_persona failed for %s "
            "(falling back to recall)", occupant_id,
        )
        return True


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
    head_room_out: dict[str, Any] | None = None,
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

    ``head_room_out`` は呼び出し側が渡す out-param dict (``context_meta`` と同じ
    型の受け皿)。**実際に描画した snapshot** が見せている部屋を
    ``{"building_id": ..., "room_text": ...}`` で書き戻す。知覚の「部屋の様子」
    の差分をそのまま出すか全文へ開き直すかは「今この prompt に部屋の全体像が
    載っているか」で決まるので、判定は描画済みの head と同じ値を見なければ
    ならない — 後から :func:`current_head_room` で読み直すと、その間に走った
    Metabolism / TTL の撮り直しで別の部屋を見て、全体像の無い差分を送りうる
    (2026-09-05 Codex 指摘)。渡さなければ書き戻さない。

    戻り値は ``[{"role": ..., "content": ..., "metadata": ...}, ...]`` の標準
    message dict 列。``prepare_context`` の system / memory_weave / visual_context
    部分の置き換えとして使う。
    """
    from sea.head_pipeline.types import HeadNotReadyError

    pipeline = pipeline or get_default_pipeline()
    ctx = build_line_head_input(persona, manager, building_id, model_key=model_key)
    ensure_snapshot(pipeline, ctx)

    # 以降の readiness 検証・render・composition は全てこの pin した snapshot
    # に対して行う (Codex 二巡 P1): 検証と render の間に別スレッドが未保存の
    # 新版を公開しても、「検証した版」を描画するので fail-closed が崩れない。
    pinned = pipeline.get_snapshot(ctx.persona_id, ctx.model_key)

    if head_room_out is not None:
        # pin した版から読む。以降の readiness 検証・render はこの版に対して
        # 行われるので、書き戻す値は「実際に送る head の部屋」と必ず一致する。
        # ただし enabled_sections が visual_context を外す呼び出しでは、この
        # prompt に部屋の全体像は載らない — 「どの部屋も見せていない」を書き、
        # head 土台の差分を全文へ開き直す側に倒す (2026-09-05 Codex 二巡:
        # 描画されないセクションの部屋を報告すると、全体像なしで差分だけを
        # 送る契約破れになる)。
        if (
            enabled_sections is None
            or VISUAL_CONTEXT_SECTION_NAME in enabled_sections
        ):
            building_id, room_text = head_room_of_snapshot(pinned)
        else:
            building_id, room_text = None, ""
        head_room_out["building_id"] = building_id
        head_room_out["room_text"] = room_text

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

    vc = rendered_by_name.get(VISUAL_CONTEXT_SECTION_NAME)
    if vc is not None and (vc.text or vc.media):
        media_list = [
            {"path": m.path, "mime_type": m.mime_type, "type": m.role}
            for m in vc.media
        ]
        messages.append({
            "role": "user",
            "content": vc.text or "",
            "metadata": {
                "media": media_list,
                _VISUAL_CONTEXT_MARKER: True,
            },
        })

    return messages
