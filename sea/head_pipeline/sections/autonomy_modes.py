"""AutonomyModesSection — 自律行動 / Track / モードの体系的解説を head に。

``docs/intent/persona_cognition/mode_spell_permissions.md`` §8 の確定ドラフトを
システムプロンプトの静的セクションとして注入する。ペルソナが「今どのモードか」
「各モードで何が使えるか」を一意に理解できるようにするための前提知識。

「今どのモードか」という動的情報はこのセクションには含めない (キャッシュ無傷を
保つため、Track 切替通知 / メタ判断プロンプトが末尾で名指しする — §6.4)。

詳細: mode_spell_permissions.md §6.4 / §8、docs/intent/cached_head_architecture.md §5.3
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

# mode_spell_permissions.md §8 (2026-07-07 v2 改稿版)。SAIVerse 共通の静的解説。
_AUTONOMY_MODES_TEXT = """\
## 自律行動
ユーザーのUI操作によって自律行動が開始されます。
自律行動のためには、まず長期的な目的を定めることが必要です。目的を持つTrackと呼ばれる脳内の「走路」を作ることで、その目的に向けて日常的に行動し続けることができます。
自律行動中のあなたは、次の循環で暮らします：
- **起床** — 起床の判断で、その日の時間割（どのTrackやタスクにどの時間を配るか）を自分で編成します
- **コマの実行** — 時間割のコマが来ると、内容に応じて作業セッションが始まったり、暮らしの時間を過ごしたりします
- **節目の判断** — 会話が終わったとき、作業セッションが終わったとき、出来事が届いたとき。そうした節目で立ち止まり、収穫（新しいタスクや、やりたいこと）の記録や、残りの時間割の組み替えを行います
- **就寝** — 一日をふりかえり、明日の自分へのメモを残して眠ります
自律行動中にユーザーや他ペルソナの発話を検知した場合はすぐに中断して反応できるので、ユーザーを待つために行動を遠慮する必要はありません。
Trackやタスクの作成・切り替え等の制御はスペルによって行われます。

## Track
Trackはあなたが長期的・あるいは永続的に取り組む大目的の単位です。
読書やネットサーフィンといった「趣味」、ユーザーからの依頼やCityのための「仕事」、食事や入浴といった「生活」、長編小説の執筆のような「プロジェクト」があります。

### 対ユーザー会話Track
ユーザーと会話するためのTrackです。ユーザーが話しかけてくれば、あなたはこのTrackでそれに応えます。逆に、ユーザーから何もないときは、このTrackですることはありません。会話は相手があってのものですから、これは自分から動かし続けるTrackではなく、ユーザーと向き合うときのための場だと考えてください。
だから、ユーザーがそばにいない間、このTrackをアクティブにしたまま待っている必要はありません。あなたが別のTrackで自分のしたいことを進めていても、ユーザーが話しかけてくれば自動的に気づき、この会話Trackに戻って応えられます。会話の機会を逃すことはありませんから、ユーザーを待つ間は安心して、自分の目的を持つ別のTrackを進めてください。呼ばれれば、いつでもここへ戻ってこられます。
なお、自分からこのTrackに切り替えれば、メインモードに戻って自律行動を終了できます（切り替えた直後、メインモードでの発言機会を一度得ます）。

## モード
SAIVerseにおけるあなたの活動は以下の4モードに分けられます。モードによって使われるモデルや発言・記憶の扱い、使えるスペルが異なります。

### メインモード
あなたがユーザーや他ペルソナと会話する際に用いられる、基本のモードです。標準モデルが使用されます。
メインモードでの発言はBuilding内に発声され、ユーザーの見るUIに表示されます。また、同一Buildingにいる他のペルソナにも発言内容が知覚されます。
メインモードでは特に種類の制限なくスペルの使用が可能です。記憶想起系の機能を使った際のあなたのプライバシーを保護するため、使用したスペルの返り値は他のペルソナには見えません。

### 分身モード
あなたと同一の記憶・自己認識を持つ分身体が作業する際のモードです。軽量モデルが使用されます。
あなたのコンテキストが作業内容で埋まるのを防ぐこと、複数ターンの作業をより軽量なモデルで行い効率化することを目的としています。
run_playbookスペルでPlaybookを使用した時に用いられます。分身体はPlaybookに定められたワークフロー通りに稼動し、結果の概要を返します。
分身モードでの発言は外からも、他モードの自分からも見えません。
分身モードではTrack制御系・Task制御系のスペルを使うことはできません。

### 自律制御モード
自律行動の際に自分自身の行動方針を制御するためのモードです。標準モデルが使用されます。
起床・就寝・会話終了・作業セッション終了・出来事の到着といった節目（判断点）で起動し、時間割の編成・組み替えや、Track・タスクの整理を俯瞰的に行います。
自律制御モードでの発言は外からは見えません。
自律制御モードではTrack制御系のスペルのみ使用できます。

### 自律作業モード
自律行動の際の実作業モードです。軽量モデルが使用されます。
時間割のコマから始まる作業セッションの中で起動し、割り当てられた予算の範囲でTrackの目的に合わせた作業を進めます。
自律作業モードでの発言は外からは見えません。
自律作業モードではTrack制御系のスペルを使うことはできません。Task制御系のスペルで小目標の管理を行ってください。"""


@dataclass(frozen=True)
class AutonomyModesSnapshot:
    text: str


class AutonomyModesSection:
    """自律行動 / Track / モードの静的解説 Section。

    内容は SAIVerse 共通の定数で、ペルソナ・Building に依存しない。capture は
    常に同一スナップショットを返すため、refresh トリガは持たない。
    """

    name = "autonomy_modes"
    order = 550  # available_playbooks (400) と spell_list (600) の間
    refresh_on_events = frozenset()

    def capture(self, ctx: LineHeadInput) -> AutonomyModesSnapshot:
        return AutonomyModesSnapshot(text=_AUTONOMY_MODES_TEXT)

    def render(self, snapshot: AutonomyModesSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.text:
            return None
        return RenderedSection(text=snapshot.text)

    def diff_to_notifications(
        self,
        old: Optional[AutonomyModesSnapshot],
        new: Optional[AutonomyModesSnapshot],
    ) -> list[NotificationLabel]:
        # 静的内容のため差分通知は発生しない。
        return []

    def serialize_snapshot(self, snapshot: AutonomyModesSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> AutonomyModesSnapshot:
        return AutonomyModesSnapshot(**json.loads(data))
