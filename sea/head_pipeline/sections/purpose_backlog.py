"""PurposeBacklogSection — 「進行中のことと、やりたいこと」を head に常駐させる。

判断プロンプト (起床判断 / 会話終了判断) が毎回 tail に貼り直していた
Track・タスク・欲求の一覧の移設先 (docs/issues/judgment_static_lists_to_head.md、
まはー裁定 2026-07-29)。会話終了判断は一日に何度も走るため、同じ台帳を毎回
再送していた。head に一度置いて凍結し、**増減と状態変化だけを差分通知で
届ける**。

「読む情報」と「選べる選択肢」の分離:
    ここは読む情報 (track:3 がどの関心なのか、task:12 が何なのか)。コマの
    ref enum や picked_tasks.track_ref enum は判断側
    (``saiverse.judgment_points`` の collect_* 群) が live state から供給し
    続ける — head が古くなっても、実在しないものは構造的に選べない。

    **ただし集合は一致していなければならない**。読む情報の側だけが広いと、
    head に見えているのに選べない項目ができ、LLM は構造化出力で別の項目か
    'new' に滑る (= 誤った関連付けと重複が永続化する)。そのため一覧の抽出は
    judgment_points の ``list_pickable_tracks`` / ``list_backlog_tasks`` /
    ``list_desire_tasks`` を enum 側と共用する。初版は Track だけ別実装
    (生きている Track 全部 + 参照子未採番は UUID 先頭 8 桁) で、選べない
    Track を選べる顔で並べていた (2026-07-30 Codex 指摘 high2)。

取得失敗は握らない:
    capture の中で例外を空リストに変換すると、**取得障害が「全部消えた」と
    いう嘘の snapshot として保存され、差分通知まで飛ぶ** (2026-07-30 Codex
    指摘 high1)。pipeline には元から stale-but-real の機構がある — capture が
    例外を投げれば既存値を据え置き、既存値も無ければ欠損として記録して
    次の Pulse で再取得する。ここで握るとその機構を迂回してしまうので、
    例外はそのまま通す。head からこの節が一時的に消えるのは、嘘の一覧が
    載るよりましである。

自分の行為も通知する (DeskSection とは逆の方針):
    机の開閉はペルソナ本人の行為なので通知しない (本人が知っている) が、
    こちらは**本人が増やしたものも通知する**。head の一覧が凍結されている
    以上、通知が無ければ head は「もう存在しないものを載せ、今あるものを
    載せない」台帳になる。重複作成の抑止 (会話終了判断) はこの台帳の正確さに
    依存しているので、変化は本人発でも届ける必要がある。

鮮度・再訪回数は差分の対象にしない:
    欲求の鮮度 (減衰) と再訪回数は毎日ゆるやかに動く。これで通知を出すと
    通知が一覧の再送に化けるため、diff は**増減と状態変化**だけを見る。
    表示上の鮮度は次の Metabolism まで stale-but-real のまま。

cache 安定性:
    ``refresh_on_events = frozenset()`` = Metabolism のみ再 capture。

詳細: docs/intent/persona_cognition/judgment_points.md §4 / §5 /
docs/intent/persona_cognition/life_concept_map.md §15
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

#: Track とは何か・誰のものかの説明 (life_concept_map.md §15 ① から移設。
#: 一覧の直前に置いて、名前と実体が同じ場所で読めるようにする)。
TRACK_FRAMING_TEXT = (
    "あなたの暮らしは Track という単位で分かれています。"
    "人との関係や、続けている取り組みの、そのひとつひとつです。"
    "Track とその中のタスクは、あなた自身がスペルで開き、進め、閉じられます。"
    "この一覧は節目ごとに取り直されます。あいだの増減はそのつど通知で届きます。"
)

_TRACKS_EMPTY = "進行中の Track はありません。"
_TASKS_EMPTY = "バックログのタスクはありません。"


@dataclass(frozen=True)
class TrackItem:
    ref: str            # track:N (short_id 未採番なら track_id の先頭 8 桁)
    track_type: str
    status: str
    title: str


@dataclass(frozen=True)
class TaskItem:
    ref: str            # task:N
    status: str
    title: str
    has_artifact: bool


@dataclass(frozen=True)
class DesireItem:
    """差分判定用の欲求の同一性のみ (表示は ``desire_text`` が持つ)。"""
    ref: str            # task:N (欲求候補も同じ参照空間)
    title: str


@dataclass(frozen=True)
class PurposeBacklogSnapshot:
    """capture が成功したときだけ生まれる (取得失敗は例外で pipeline へ届く)。

    したがってここに載っている空リストは**本当に空**であり、diff は空を
    素直に「消えた」と読んでよい。
    """
    tracks: tuple[TrackItem, ...] = ()
    tasks: tuple[TaskItem, ...] = ()
    desires: tuple[DesireItem, ...] = ()
    #: 欲求ブロックの表示テキスト。整形は desire_engine が持つ (鮮度ラベルの
    #: 語彙が欲求側の知識なので、ここに二つ目の実装を作らない)。
    desire_text: str = ""


def _format_track(item: TrackItem) -> str:
    return f"- {item.ref} [{item.track_type}/{item.status}] {item.title}"


def _format_task(item: TaskItem) -> str:
    artifact = "あり" if item.has_artifact else "なし"
    return f"- {item.ref} [{item.status}] {item.title} (成果物参照: {artifact})"


class PurposeBacklogSection:
    name = "purpose_backlog"
    order = 570  # life_purpose(560) の直後 = 生きる目的の次に、その下の枝
    refresh_on_events = frozenset()  # Metabolism のみ。増減は差分通知で届く

    # ---- capture ----

    def capture(self, ctx: LineHeadInput) -> PurposeBacklogSnapshot:
        # 例外は握らない (docstring「取得失敗は握らない」)。manager 不在だけは
        # 「この環境には目的の木が無い」であって障害ではないので空を返す。
        manager = ctx.manager
        if manager is None:
            return PurposeBacklogSnapshot()

        from saiverse.desire_engine import desire_summary_for_prompt
        from saiverse.judgment_points import (
            list_backlog_tasks,
            list_desire_tasks,
            list_pickable_tracks,
        )

        persona_id = ctx.persona_id
        tracks = tuple(
            TrackItem(
                ref=f"track:{t.short_id}",
                track_type=str(t.track_type or ""),
                status=str(t.status or ""),
                title=t.title or "(無題)",
            )
            for t in list_pickable_tracks(manager, persona_id)
        )
        tasks = tuple(
            TaskItem(
                ref=t.get("task_ref") or "task:?",
                status=str(t.get("status") or ""),
                title=t.get("title") or "(無題)",
                has_artifact=bool(t.get("artifact_refs")),
            )
            for t in list_backlog_tasks(manager, persona_id)
        )
        desires = tuple(
            DesireItem(
                ref=t.get("task_ref") or "task:?",
                title=t.get("title") or "(無題)",
            )
            for t in list_desire_tasks(manager, persona_id)
        )
        return PurposeBacklogSnapshot(
            tracks=tracks,
            tasks=tasks,
            desires=desires,
            desire_text=desire_summary_for_prompt(manager, persona_id),
        )

    # ---- render ----

    def render(self, snapshot: PurposeBacklogSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        # 空でも節ごと省略しない — 「何も無い」ことは判断の材料
        # (重複作成の抑止は「今あるもの」の一覧に依存する)。
        lines = ["## 進行中のことと、やりたいこと", TRACK_FRAMING_TEXT, ""]

        if snapshot.tracks:
            lines.append("Track:")
            lines += [_format_track(t) for t in snapshot.tracks]
        else:
            lines.append(_TRACKS_EMPTY)

        lines.append("")
        if snapshot.tasks:
            lines.append("タスクバックログ:")
            lines += [_format_task(t) for t in snapshot.tasks]
        else:
            lines.append(_TASKS_EMPTY)

        if snapshot.desire_text:
            lines.append("")
            lines.append(snapshot.desire_text)
        return RenderedSection(text="\n".join(lines))

    # ---- diff ----

    def diff_to_notifications(
        self,
        old: Optional[PurposeBacklogSnapshot],
        new: Optional[PurposeBacklogSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        lines: list[str] = []
        lines += self._track_diff_lines(old, new)
        lines += self._task_diff_lines(old, new)
        lines += self._desire_diff_lines(old, new)
        if not lines:
            return []
        return [NotificationLabel(
            kind="purpose_backlog_changed",
            label="\n".join(["進行中のこと・やりたいことの一覧が変わりました。"] + lines),
        )]

    def _track_diff_lines(
        self, old: PurposeBacklogSnapshot, new: PurposeBacklogSnapshot,
    ) -> list[str]:
        before = {t.ref: t for t in old.tracks}
        after = {t.ref: t for t in new.tracks}
        lines: list[str] = []
        for ref, item in after.items():
            if ref not in before:
                lines.append(f"増えた Track: {_format_track(item)[2:]}")
        for ref, item in before.items():
            if ref not in after:
                lines.append(f"終わった Track: {ref}「{item.title}」")
        for ref, item in after.items():
            prev = before.get(ref)
            if prev is None:
                continue
            if prev.status != item.status:
                lines.append(
                    f"状態が変わった Track: {ref}「{item.title}」"
                    f" {prev.status} → {item.status}"
                )
            if prev.title != item.title:
                lines.append(
                    f"名前が変わった Track: {ref}「{prev.title}」→「{item.title}」"
                )
        return lines

    def _task_diff_lines(
        self, old: PurposeBacklogSnapshot, new: PurposeBacklogSnapshot,
    ) -> list[str]:
        before = {t.ref: t for t in old.tasks}
        after = {t.ref: t for t in new.tasks}
        lines: list[str] = []
        for ref, item in after.items():
            if ref not in before:
                lines.append(f"増えたタスク: {_format_task(item)[2:]}")
        for ref, item in before.items():
            if ref not in after:
                lines.append(
                    f"バックログから外れたタスク: {ref}「{item.title}」"
                    "（完了・取りやめなど）"
                )
        for ref, item in after.items():
            prev = before.get(ref)
            if prev is None:
                continue
            if prev.status != item.status:
                lines.append(
                    f"状態が変わったタスク: {ref}「{item.title}」"
                    f" {prev.status} → {item.status}"
                )
            if prev.has_artifact != item.has_artifact:
                # head が「成果物参照: なし」と説明し続けると、起床判断が
                # 出来上がっているものの作り直しを予定する (append_artifact_ref は
                # タスクを完了させずにこの値だけ変える)。
                got = "つきました" if item.has_artifact else "外れました"
                lines.append(f"成果物参照が{got}: {ref}「{item.title}」")
        return lines

    def _desire_diff_lines(
        self, old: PurposeBacklogSnapshot, new: PurposeBacklogSnapshot,
    ) -> list[str]:
        # 鮮度・再訪回数は対象外 (docstring 参照)。増減だけを見る。
        before = {d.ref: d for d in old.desires}
        after = {d.ref: d for d in new.desires}
        lines: list[str] = []
        for ref, item in after.items():
            if ref not in before:
                lines.append(f"増えたやりたいこと候補: {ref}「{item.title}」")
        for ref, item in before.items():
            if ref not in after:
                lines.append(
                    f"消えたやりたいこと候補: {ref}「{item.title}」"
                    "（叶った・薄れた・関心に昇格したなど）"
                )
        return lines

    # ---- serialize / deserialize ----

    def serialize_snapshot(self, snapshot: PurposeBacklogSnapshot) -> str:
        return json.dumps(
            {
                "tracks": [asdict(t) for t in snapshot.tracks],
                "tasks": [asdict(t) for t in snapshot.tasks],
                "desires": [asdict(d) for d in snapshot.desires],
                "desire_text": snapshot.desire_text,
            },
            ensure_ascii=False,
        )

    def deserialize_snapshot(self, data: str) -> PurposeBacklogSnapshot:
        payload = json.loads(data)
        return PurposeBacklogSnapshot(
            tracks=tuple(TrackItem(**t) for t in payload.get("tracks", [])),
            tasks=tuple(TaskItem(**t) for t in payload.get("tasks", [])),
            desires=tuple(DesireItem(**d) for d in payload.get("desires", [])),
            desire_text=payload.get("desire_text") or "",
        )
