"""AvailablePlaybooksSection の差分通知は種別ごとに 1 ラベル (2026-09-07)。

spell_list と同型の束ね (docs/issues/perception_state_pushed_at_event_time.md)。
ラベルごとに [システム通知] の見出しが付くので、能力 1 件ごとに分けると
一度の変化で見出しが何個も並ぶ。
"""
from __future__ import annotations

from sea.head_pipeline.sections.available_playbooks import (
    AvailablePlaybooksSection,
    AvailablePlaybooksSnapshot,
    PlaybookEntry,
)


def _snapshot(*names: str) -> AvailablePlaybooksSnapshot:
    return AvailablePlaybooksSnapshot(
        entries=tuple(PlaybookEntry(name=n, description="") for n in names),
    )


def test_single_change_keeps_previous_wording():
    section = AvailablePlaybooksSection()
    labels = section.diff_to_notifications(_snapshot("a"), _snapshot("a", "b"))
    assert [(label.kind, label.label) for label in labels] == [
        ("playbook_added", "新しい能力「b」が使えるようになりました"),
    ]


def test_multiple_changes_bundle_into_one_label_per_kind():
    section = AvailablePlaybooksSection()
    labels = section.diff_to_notifications(
        _snapshot("a", "b"), _snapshot("c", "d"),
    )
    assert [(label.kind, label.label) for label in labels] == [
        ("playbook_added", "新しい能力「c」「d」が使えるようになりました"),
        ("playbook_removed", "能力「a」「b」が使えなくなりました"),
    ]


def test_no_change_yields_no_labels():
    section = AvailablePlaybooksSection()
    assert section.diff_to_notifications(_snapshot("a"), _snapshot("a")) == []
