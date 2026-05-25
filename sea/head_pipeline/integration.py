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
SYSTEM_PROMPT_SECTION_NAMES: tuple[str, ...] = (
    "common_prompt",
    "persona_self",
    "building",
    "available_playbooks",
    "spell_list",
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

    LLM 呼び出し成功後に ``_touch_anchor_after_llm_call`` が touch する updated_at が
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

    load_anchors = getattr(sea_runtime, "_load_anchors", None)
    get_validity = getattr(sea_runtime, "_get_anchor_validity_seconds", None)
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
    """全 Section の diff を集めて末尾通知 user message として SAIMemory に注入する。

    旧 ``DynamicStateManager.maybe_inject_event_messages`` の置き換え。pipeline 経由で
    全 Section の diff_to_notifications を回し、得られた NotificationLabel 群を
    ``[システム通知]`` 形式 1 メッセージにまとめてペルソナの SAIMemory へ append する。

    notification は **Track 横断のメタログ扱い** (= 旧 dynamic_state と同じ):
    ``origin_track_id`` を付けず、``metadata.tags = ['internal', 'event_message']``
    で保存する。詳細: docs/intent/persona_cognition/handoff_2026-05-10.md §3

    Returns:
        ラベルが 1 件以上注入された場合 True、差分なしなら False。
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

    text = _format_notification_block(labels)
    message: dict[str, Any] = {
        "role": "user",
        "content": f"<system>{text}</system>",
        "metadata": {"tags": ["internal", "event_message"]},
    }
    try:
        sai_mem.append_persona_message(message)
    except Exception:
        LOGGER.exception(
            "head_pipeline: append_persona_message failed for diff notification",
        )
        return False
    LOGGER.info(
        "head_pipeline: injected %d notification labels for persona=%s building=%s",
        len(labels), ctx.persona_id, building_id,
    )
    return True


def _format_notification_block(labels: list[NotificationLabel]) -> str:
    lines = ["[システム通知]"]
    for label in labels:
        lines.append(f"- {label.label}")
    return "\n".join(lines)


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

    rendered = pipeline.render_head(ctx.persona_id, ctx.line_id)
    rendered_by_name = {
        name: section
        for name, section in _zip_rendered_with_names(pipeline, ctx, rendered)
        if enabled_sections is None or name in enabled_sections
    }
    return _compose_messages(pipeline, ctx, rendered_by_name)


def _zip_rendered_with_names(
    pipeline: HeadPipeline,
    ctx: LineHeadInput,
    rendered: list[RenderedSection],
) -> list[tuple[str, RenderedSection]]:
    """RenderedSection 列に Section 名を対応付ける。

    pipeline.render_head は order 順の RenderedSection を返すが、Section 名は
    含まれていない。registry から order 順で名前を引いて zip する。
    snapshot に無い Section は render されないので、空 entry はスキップして
    順序が崩れないようにする。
    """
    pairs: list[tuple[str, RenderedSection]] = []
    snapshot = pipeline.get_snapshot(ctx.persona_id, ctx.line_id)
    if snapshot is None:
        return pairs
    rendered_iter = iter(rendered)
    # registry.all_sections() は pipeline と同じ order を返すので、
    # snapshot.sections に含まれかつ render が None でない Section だけ拾う。
    for section in pipeline.registry.all_sections():
        if section.name not in snapshot.sections:
            continue
        try:
            r = next(rendered_iter)
        except StopIteration:
            break
        pairs.append((section.name, r))
    return pairs


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
