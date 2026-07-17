"""Cached Head Architecture: prompt の head 部分を section snapshot 経由でのみ
構築する基盤。詳細: docs/intent/cached_head_architecture.md
"""
from sea.head_pipeline.integration import (
    build_line_head_input,
    ensure_snapshot,
    inject_diff_notifications,
    render_head_messages,
)
from sea.head_pipeline.notify import (
    notify_head_mutation,
    notify_head_mutation_from_tool_context,
)
from sea.head_pipeline.pipeline import HeadPipeline, get_default_pipeline
from sea.head_pipeline.registry import HeadSectionRegistry, get_default_registry
from sea.head_pipeline.store import LineHeadSnapshotStore, StoredLineState
from sea.head_pipeline.types import (
    EventType,
    HeadSection,
    LineHeadInput,
    LineHeadSnapshot,
    MediaRef,
    NotificationLabel,
    RenderedSection,
)

__all__ = [
    "EventType",
    "HeadPipeline",
    "HeadSection",
    "HeadSectionRegistry",
    "LineHeadInput",
    "LineHeadSnapshot",
    "LineHeadSnapshotStore",
    "MediaRef",
    "NotificationLabel",
    "RenderedSection",
    "StoredLineState",
    "build_line_head_input",
    "ensure_snapshot",
    "get_default_pipeline",
    "get_default_registry",
    "inject_diff_notifications",
    "notify_head_mutation",
    "notify_head_mutation_from_tool_context",
    "render_head_messages",
]
