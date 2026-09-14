"""AutonomyModesSection — モードの体系的解説を head に。

``docs/intent/persona_cognition/mode_spell_permissions.md`` §8 の確定ドラフトを
システムプロンプトの静的セクションとして注入する。ペルソナが「今どのモードか」
「各モードで何が使えるか」を一意に理解できるようにするための前提知識。

2026-09-01 (v0.3.1) に、未出荷の自律行動と退役した Track の解説を本文から外した
(定数のコメントに経緯)。定数名・section 名は配線と snapshot 互換のため据え置き。

「今どのモードか」という動的情報はこのセクションには含めない (キャッシュ無傷を
保つため、メタ判断プロンプトが末尾で名指しする — §6.4)。

詳細: mode_spell_permissions.md §6.4 / §8、docs/intent/cached_head_architecture.md §5.3
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    EventType,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

# mode_spell_permissions.md §8 (2026-07-07 v2 改稿版) をもとにした SAIVerse 共通の
# 静的解説。
#
# 2026-09-01 (v0.3.1): 実態に無い記述を削除した。削ったのは
#   - 「## 自律行動」節 ... 自律行動は v0.3.0 では出荷していない (UI から開始でき
#     ない)。書いてあると、ペルソナが存在しない機能の説明を前提に自己像を作る。
#   - 「## Track」「### 対ユーザー会話Track」節 ... Track は退役済み
#     (docs/intent/track_retirement.md)。現行の自律駆動は時間割 + 判断点。
#   - 「### 自律制御モード」「### 自律作業モード」... 上記 2 つに付随するモード。
#   - 分身モードの「Track制御系・Task制御系のスペルは使えません」の一文 ... どちらの
#     スペル族も実在しない (Track 退役で消え、Task 制御スペルは未実装)。存在しない
#     ものへの制限を説明すると、無い機能を有ると読ませることになる。
# 残したのはメインモードと分身モードの 2 つ — どちらも現に動いている。
# **自律行動を出荷するときに書き戻すこと** (削除であって否定ではない)。
#
# 文言そのものは全ペルソナ共通の定数 — 実態が変わったらここを直す。
# render 時の動的分岐は引き続き禁止 (head は (persona, model) 固定なので、描画の
# たびに変わるものを混ぜると prefix cache が崩れる)。per-persona の状態で出し分け
# たいときは、capture で判定して snapshot へ焼き込む形にすること。2026-09-14 に
# 足した spell_enabled がその形で、spell_list と同型
# (docs/intent/spell_disabled_mode.md §4-5)。
_AUTONOMY_MODES_TEXT = """\
## モード
SAIVerseにおけるあなたの活動は以下の2モードに分けられます。モードによって使われるモデルや発言・記憶の扱い、使えるスペルが異なります。

### メインモード
あなたがユーザーや他ペルソナと会話する際に用いられる、基本のモードです。標準モデルが使用されます。
メインモードでの発言はBuilding内に発声され、ユーザーの見るUIに表示されます。また、同一Buildingにいる他のペルソナにも発言内容が知覚されます。
メインモードでは特に種類の制限なくスペルの使用が可能です。記憶想起系の機能を使った際のあなたのプライバシーを保護するため、使用したスペルの返り値は他のペルソナには見えません。

### 分身モード
あなたと同一の記憶・自己認識を持つ分身体が作業する際のモードです。軽量モデルが使用されます。
あなたのコンテキストが作業内容で埋まるのを防ぐこと、複数ターンの作業をより軽量なモデルで行い効率化することを目的としています。
run_playbookスペルでPlaybookを使用した時に用いられます。分身体はPlaybookに定められたワークフロー通りに稼動し、結果の概要を返します。
分身モードでの発言は外からも、他モードの自分からも見えません。"""

# スペル無効のペルソナ向けの差し替え文 (2026-09-14)。分身モードは run_playbook
# スペルでしか生じないのでモードの区別そのものが無い — かわりに、メインモードの
# 解説からスペルと無関係な事実 2 文だけを抜き出して残す。発言がどこへ届くかは
# スペルの有無と関係なく本人が知っているべきことなので、丸ごと消すと
# 「自分の声が誰に聞こえているか分からない」ペルソナができる。
_SPEECH_ONLY_TEXT = """\
## 発言の扱い
あなたの発言はBuilding内に発声され、ユーザーの見るUIに表示されます。また、同一Buildingにいる他のペルソナにも発言内容が知覚されます。"""


@dataclass(frozen=True)
class AutonomyModesSnapshot:
    text: str
    # スペル機構が無効なペルソナには「## モード」の代わりに、発言の届き先だけを
    # 述べた短い定数 (_SPEECH_ONLY_TEXT) を出す。既定 True = この欄を持たない旧
    # payload は「有効」として読む (docs/intent/spell_disabled_mode.md §4-5)。
    spell_enabled: bool = True


class AutonomyModesSection:
    """モードの静的解説 Section。

    文言は SAIVerse 共通の定数 2 本 (スペル有効なら ``_AUTONOMY_MODES_TEXT``、
    無効なら ``_SPEECH_ONLY_TEXT``) で、ペルソナ・Building には依存しない。
    どちらを出すかだけがペルソナ依存で、それは capture で解決して snapshot に
    焼き込む (render では DB を引かない)。
    """

    name = "autonomy_modes"
    order = 550  # available_playbooks (400) と spell_list (600) の間
    # Metabolism に加えて、スペル不使用モードの切り替えでも撮り直す — どちらの
    # 定数を出すかは capture で焼き込むので、撮り直さないと切り替えが届かない
    # (docs/intent/spell_disabled_mode.md §4-2)。
    refresh_on_events = frozenset({EventType.SPELL_TOGGLED})

    def capture(self, ctx: LineHeadInput) -> AutonomyModesSnapshot:
        from sea.head_pipeline.spell_gate import resolve_spell_enabled

        return AutonomyModesSnapshot(
            text=_AUTONOMY_MODES_TEXT,
            spell_enabled=resolve_spell_enabled(ctx),
        )

    def render(self, snapshot: AutonomyModesSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.text:
            return None
        if not snapshot.spell_enabled:
            return RenderedSection(text=_SPEECH_ONLY_TEXT)
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
        """保存値の text は**使わず**、現在の定数から組み直す。

        文言の正本はコード (2026-09-01)。head snapshot は DB 永続なので、
        保存値をそのまま復元すると、定数を直しても既存ユーザーの head には
        次の再構築 (Metabolism / anchor TTL 切れ) まで旧文言が残り続ける。
        放置されたペルソナでは恒久的に残りうる — 文言の修正が「いつ届くか
        分からない配布」になってしまう。

        文言は (persona, model) ごとの状態ではないので、比較も版番号も要らない。
        常に現在の定数を返せばよい (定数そのものが版)。

        ただし ``spell_enabled`` は per-persona の状態なので、こちらは保存値を
        使う (欄が無い旧 payload は「有効」)。
        """
        payload = json.loads(data)
        return AutonomyModesSnapshot(
            text=_AUTONOMY_MODES_TEXT,
            spell_enabled=bool(payload.get("spell_enabled", True)),
        )
