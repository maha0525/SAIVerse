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
# (common_prompt < persona_self < core_memory(250) < building < available_playbooks(400) <
#  autonomy_modes(550) < life_purpose(560) < spell_list(600) < desk(730))。
# 旧 open_notes(720) は P3c① (concept_consolidation.md「Note → テーマノード移行」)
# で退役し、後継の desk (机の物理) に置き換わった。
SYSTEM_PROMPT_SECTION_NAMES: tuple[str, ...] = (
    "common_prompt",
    "persona_self",
    "core_memory",
    "building",
    "available_playbooks",
    "autonomy_modes",
    "life_purpose",
    "spell_list",
    "desk",
)
MEMORY_WEAVE_SECTION_NAME = "memory_weave"
VISUAL_CONTEXT_SECTION_NAME = "visual_context"

# Phase 2 段階では line_id は単一の "main" 固定 (= メインライン 1 本想定)。
# Phase 3+ でサブライン対応する際に呼び出し側から line_id を渡せるようにする。
_DEFAULT_LINE_ID = "main"
_DEFAULT_LINE_ROLE = "main_line"


def build_line_head_input(
    persona: Any,
    manager: Any,
    building_id: str,
    *,
    line_id: str = _DEFAULT_LINE_ID,
    line_role: str = _DEFAULT_LINE_ROLE,
) -> LineHeadInput:
    """prepare_context の引数から LineHeadInput を組み立てる。"""
    persona_id = getattr(persona, "persona_id", "") or ""
    model_key = (
        getattr(persona, "default_model", None)
        or getattr(persona, "DEFAULT_MODEL", None)
        or "default"
    )
    model_key_str = str(model_key)

    anchor_updated_at, cache_ttl_seconds = _resolve_anchor_ttl_state(
        persona, manager, model_key_str,
    )

    return LineHeadInput(
        persona_id=persona_id,
        line_id=line_id,
        line_role=line_role,
        model_key=model_key_str,
        current_building_id=building_id,
        persona=persona,
        manager=manager,
        anchor_updated_at=anchor_updated_at,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def _resolve_anchor_ttl_state(
    persona: Any, manager: Any, model_key: str,
) -> tuple[Optional[float], Optional[int]]:
    """``METABOLISM_ANCHORS[model_key].updated_at`` を epoch seconds として取得、
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

    Returns:
        ラベルが 1 件以上 push された場合 True、差分なしなら False。
    """
    pipeline = pipeline or get_default_pipeline()
    ctx = build_line_head_input(persona, manager, building_id)
    ensure_snapshot(pipeline, ctx)
    labels = pipeline.flush_diffs(ctx, all_sections=True)
    if not labels:
        return False

    sai_mem = getattr(persona, "sai_memory", None)
    if sai_mem is None or not sai_mem.is_ready():
        LOGGER.debug(
            "head_pipeline: SAIMemory not ready, %d notification labels discarded",
            len(labels),
        )
        return False

    for label in labels:
        try:
            sai_mem.push_perception("world_state", label.label)
        except Exception:
            LOGGER.exception(
                "head_pipeline: push_perception failed for world_state label",
            )
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
    """
    history_manager = getattr(persona, "history_manager", None)
    if not history_manager:
        return

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

        try:
            recall_text = history_manager.recall_conversation_with(
                occupant_id,
                current_thread_only=False,
                max_results=6,
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


def ensure_snapshot(pipeline: HeadPipeline, ctx: LineHeadInput) -> None:
    """pipeline に snapshot が無ければ store から load、それでも無ければ capture_all。

    load_from_store が成功しても、登録済み Section のうち snapshot.sections に
    入っていないものがあれば (= 旧 schema 等で deserialize が失敗した) 自己修復で
    capture_all を走らせて欠損を埋める。

    加えて、 ctx に anchor TTL 状態 (= ``anchor_updated_at`` + ``cache_ttl_seconds``)
    が積まれていて TTL を超過していたら、 snapshot 全体を再 capture する。
    prompt cache TTL が切れたタイミングでは「head 不変による cache hit」 の根拠が
    消えるので、 cache hit を諦めて最新状態を反映する方が情報量で勝る。
    """
    if pipeline.has_snapshot(ctx.persona_id, ctx.line_id):
        snapshot = pipeline.get_snapshot(ctx.persona_id, ctx.line_id)
        if _is_anchor_ttl_expired(ctx):
            LOGGER.info(
                "head_pipeline: anchor TTL expired (in-memory snapshot), recapturing persona=%s line=%s",
                ctx.persona_id, ctx.line_id,
            )
            pipeline.capture_all(ctx)
            return
        expected_names = {s.name for s in pipeline.registry.all_sections()}
        actual_names = set((snapshot.sections if snapshot is not None else {}).keys())
        if expected_names - actual_names:
            pipeline.capture_all(ctx)
        return
    if pipeline.load_from_store(ctx.persona_id, ctx.line_id):
        snapshot = pipeline.get_snapshot(ctx.persona_id, ctx.line_id)
        if _is_anchor_ttl_expired(ctx):
            LOGGER.info(
                "head_pipeline: anchor TTL expired (loaded snapshot), recapturing persona=%s line=%s",
                ctx.persona_id, ctx.line_id,
            )
            pipeline.capture_all(ctx)
            return
        expected_names = {s.name for s in pipeline.registry.all_sections()}
        actual_names = set((snapshot.sections if snapshot is not None else {}).keys())
        if expected_names - actual_names:
            pipeline.capture_all(ctx)
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
) -> list[dict[str, Any]]:
    """pipeline 経由で head の message 列を組み立てる。

    snapshot 不在なら自動で capture_all する (= 初回呼び出し / 再起動後)。
    snapshot が既にあれば render するだけで cache 安定する。

    ``enabled_sections`` を渡すと、その名前の Section のみを composition 対象に
    する (= 旧 ``reqs.system_prompt`` / ``reqs.memory_weave`` / ``reqs.visual_context``
    フラグ相当)。``None`` なら全 Section を render する。

    戻り値は ``[{"role": ..., "content": ..., "metadata": ...}, ...]`` の標準
    message dict 列。``prepare_context`` の system / memory_weave / visual_context
    部分の置き換えとして使う。
    """
    pipeline = pipeline or get_default_pipeline()
    ctx = build_line_head_input(persona, manager, building_id)
    ensure_snapshot(pipeline, ctx)

    # render_head は (section_name, RenderedSection) を返すので、名前で直接
    # enabled フィルタできる。位置依存の zip は廃止 (None render セクションで
    # 名前がズレて内容が欠落するバグの根治)。
    rendered = pipeline.render_head(ctx.persona_id, ctx.line_id)
    rendered_by_name = {
        name: section
        for name, section in rendered
        if enabled_sections is None or name in enabled_sections
    }
    return _compose_messages(pipeline, ctx, rendered_by_name)


def _compose_messages(
    pipeline: HeadPipeline,
    ctx: LineHeadInput,
    rendered_by_name: dict[str, RenderedSection],
) -> list[dict[str, Any]]:
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

    # Memory Weave: snapshot から chronicle / track_chronicle / memopedia の
    # entry を取り出して、それぞれ個別 user message として展開する。preview UI
    # は metadata.__memory_weave_type__ で section ラベルを切り替えるため、
    # 1 つにまとめずに 3 つの message を保つ必要がある。
    if MEMORY_WEAVE_SECTION_NAME in rendered_by_name:
        snapshot = pipeline.get_snapshot(ctx.persona_id, ctx.line_id)
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
