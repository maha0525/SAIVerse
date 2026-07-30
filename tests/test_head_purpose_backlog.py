"""PurposeBacklogSection — 判断プロンプトから head へ移した一覧の振る舞い。

移設の前提 (docs/issues/judgment_static_lists_to_head.md、まはー裁定
2026-07-29): head は凍結されるので、**一覧を置くなら変動通知とセット**。
ここで固定するのはその対:

1. render — Track / タスク / やりたいこと候補が ref つきで並ぶ。空でも節を消さない
   (「何も無い」は重複作成の抑止に必要な情報)
2. diff — 増減・状態変化は通知される (本人が増やしたものも。head が凍結して
   いる以上、通知が無ければ head の台帳が嘘になる)
3. diff の沈黙 — 鮮度・再訪回数だけが動いた capture では通知しない
   (通知が一覧の再送に化けるのを防ぐ)
4. capture は取得失敗を握らない — 空リストへ変換すると「全件消えた」という
   嘘の snapshot が保存され、差分通知まで飛ぶ (2026-07-30 Codex 指摘 high1)。
   例外を pipeline へ通せば既存値の据え置き (stale-but-real) が効く

capture (DB からの実収集) は tests/test_judgment_points.py の
``test_day_open_desire_candidate_lines_match_ref_enum`` が実 manager で通す
(表示 ref と enum の整合そのものがそこの主題)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sea.head_pipeline.sections.purpose_backlog import (  # noqa: E402
    DesireItem,
    PurposeBacklogSection,
    PurposeBacklogSnapshot,
    TaskItem,
    TrackItem,
)


def _snapshot(**overrides) -> PurposeBacklogSnapshot:
    base = dict(
        tracks=(TrackItem(ref="track:1", track_type="autonomous", status="running",
                          title="言葉の標本集"),),
        tasks=(TaskItem(ref="task:1", status="pending", title="下絵を描く",
                        has_artifact=False),),
        desires=(DesireItem(ref="task:2", title="散歩したい"),),
        desire_text="やりたいこと候補:\n- task:2 [未分類] 散歩したい (鮮度: 新鮮 / 再訪: 0回)",
    )
    base.update(overrides)
    return PurposeBacklogSnapshot(**base)


class RenderTest(unittest.TestCase):
    def test_lists_refs_types_and_statuses(self):
        text = PurposeBacklogSection().render(_snapshot()).text
        self.assertIn("## 進行中のことと、やりたいこと", text)
        self.assertIn("- track:1 [autonomous/running] 言葉の標本集", text)
        self.assertIn("- task:1 [pending] 下絵を描く (成果物参照: なし)", text)
        self.assertIn("- task:2 [未分類] 散歩したい", text)

    def test_empty_lists_still_render(self):
        """空でも節ごと消さない — 「今は何も無い」が重複作成の抑止の材料。"""
        text = PurposeBacklogSection().render(
            PurposeBacklogSnapshot(desire_text="やりたいこと候補はありません。")
        ).text
        self.assertIn("進行中の Track はありません。", text)
        self.assertIn("バックログのタスクはありません。", text)
        self.assertIn("やりたいこと候補はありません。", text)

    def test_render_depends_only_on_snapshot(self):
        section = PurposeBacklogSection()
        snap = _snapshot()
        self.assertEqual(section.render(snap).text, section.render(snap).text)


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.section = PurposeBacklogSection()

    def _labels(self, old, new):
        return self.section.diff_to_notifications(old, new)

    def test_no_change_is_silent(self):
        self.assertEqual(self._labels(_snapshot(), _snapshot()), [])

    def test_added_task_is_notified(self):
        new = _snapshot(tasks=_snapshot().tasks + (
            TaskItem(ref="task:9", status="pending", title="色を決める",
                     has_artifact=False),
        ))
        labels = self._labels(_snapshot(), new)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].kind, "purpose_backlog_changed")
        self.assertIn("増えたタスク: task:9 [pending] 色を決める", labels[0].label)

    def test_task_leaving_backlog_is_notified(self):
        labels = self._labels(_snapshot(), _snapshot(tasks=()))
        self.assertIn("バックログから外れたタスク: task:1", labels[0].label)

    def test_artifact_reference_change_is_notified(self):
        """成果物が付いたら通知する (head の「なし」を嘘のまま残さない)。

        append_artifact_ref はタスクを完了させずにこの値だけ変えられるので、
        増減・status だけを見ていると凍結 head が「成果物参照: なし」と
        説明し続け、起床判断が出来上がっているものの作り直しを予定する。
        """
        new = _snapshot(tasks=(TaskItem(
            ref="task:1", status="pending", title="下絵を描く", has_artifact=True,
        ),))
        labels = self._labels(_snapshot(), new)
        self.assertIn("成果物参照がつきました: task:1", labels[0].label)

    def test_status_change_is_notified(self):
        new = _snapshot(tracks=(TrackItem(
            ref="track:1", track_type="autonomous", status="pending",
            title="言葉の標本集",
        ),))
        self.assertIn("running → pending", self._labels(_snapshot(), new)[0].label)

    def test_track_rename_is_notified(self):
        new = _snapshot(tracks=(TrackItem(
            ref="track:1", track_type="autonomous", status="running", title="標本集",
        ),))
        labels = self._labels(_snapshot(), new)
        self.assertIn("名前が変わった Track: track:1", labels[0].label)

    def test_desire_added_and_removed(self):
        added = self._labels(
            _snapshot(),
            _snapshot(desires=_snapshot().desires + (
                DesireItem(ref="task:5", title="星を見に行きたい"),
            )),
        )
        self.assertIn("増えたやりたいこと候補: task:5", added[0].label)
        removed = self._labels(_snapshot(), _snapshot(desires=()))
        self.assertIn("消えたやりたいこと候補: task:2", removed[0].label)

    def test_freshness_drift_alone_is_silent(self):
        """鮮度・再訪回数だけの変化では通知しない。

        減衰は毎日ゆるやかに動く。これで通知を出すと、通知が一覧の再送に
        化けて移設の意味が消える (表示上の鮮度は次の節目まで stale-but-real)。
        """
        drifted = _snapshot(
            desire_text="やりたいこと候補:\n- task:2 [未分類] 散歩したい "
                        "(鮮度: 薄れつつある / 再訪: 3回)",
        )
        self.assertEqual(self._labels(_snapshot(), drifted), [])

class CaptureFailureTest(unittest.TestCase):
    """取得失敗を空 snapshot にすり替えない (Codex 指摘 high1 の回帰)。

    握ると「全件消えた」という嘘が snapshot として保存され、差分通知まで
    飛び、復旧時に「増えた」と再通知される。Metabolism の capture_all では
    その嘘が A/B として永続化され、pipeline の stale-but-real を迂回する。
    """

    def test_track_listing_failure_propagates(self):
        def boom(*a, **k):
            raise RuntimeError("db is down")

        manager = SimpleNamespace(
            track_manager=SimpleNamespace(list_for_persona=boom),
            SessionLocal=object(),
        )
        with self.assertRaises(RuntimeError):
            PurposeBacklogSection().capture(
                SimpleNamespace(persona_id="air", manager=manager)
            )

    def test_task_listing_failure_propagates(self):
        def boom():
            raise RuntimeError("db is down")

        manager = SimpleNamespace(
            track_manager=SimpleNamespace(list_for_persona=lambda *a, **k: []),
            SessionLocal=boom,
        )
        with self.assertRaises(RuntimeError):
            PurposeBacklogSection().capture(
                SimpleNamespace(persona_id="air", manager=manager)
            )

    def test_no_manager_is_empty_not_failure(self):
        """manager 不在は「目的の木が無い環境」であって障害ではない。"""
        snap = PurposeBacklogSection().capture(
            SimpleNamespace(persona_id="air", manager=None)
        )
        self.assertEqual(snap, PurposeBacklogSnapshot())


class SerializeTest(unittest.TestCase):
    def test_roundtrip(self):
        section = PurposeBacklogSection()
        snap = _snapshot()
        self.assertEqual(
            section.deserialize_snapshot(section.serialize_snapshot(snap)), snap,
        )

    def test_roundtrip_of_empty(self):
        section = PurposeBacklogSection()
        snap = PurposeBacklogSnapshot()
        self.assertEqual(
            section.deserialize_snapshot(section.serialize_snapshot(snap)), snap,
        )


if __name__ == "__main__":
    unittest.main()
