"""SelfImageSection — ① 常在の自己像 + 記法の教示を head に常駐注入する。

life_concept_map.md §15 の自己像四層のうち **① 常在の自己像** の器。
(persona, model) 固定・全ペルソナ・全モードで同一に出す (出し分けは prefix
キャッシュを壊す — feedback_head_fixed_per_persona_model_no_gating)。

内容 (§15 ①):

- 内発的な動機の一文 (全ペルソナ共通の定数)
- 樹皮の要旨 (§4.1 の保護対象 6 件を宣言文に圧縮。システムプロンプト遵守が
  最上位であることを含む)
- ==語句== 記法の教示 (§9.1 層1 観測点。P3 marker_parser が実装済みの記法)

**中身は全て定数** — capture は DB を引かない。よって本 Section の snapshot は
全ペルソナで同一で、差分通知も出ない。

経緯:

- 第一階層の短いメニューは 2026-07-30 に PurposeBacklogSection へ統合し、
  その PurposeBacklogSection も 2026-08-21 に節ごと退役した (中身の pickable
  tracks と欲求候補が供給源ごと消えたため)。
- **生きる目的 (AI.LIFE_PURPOSE) の掲示は退役した** (autonomous_behavior_v3.md
  §9-5「LIFE_PURPOSE 列は丸ごと退役」)。purpose の一文はコア記憶へ、
  interests / vocations は手帳のアクティビティへ世代交代する。本 Section の
  旧名は ``life_purpose`` だったが、目的を落とした後に残るのは自己像の定数
  だけなので、中身に合わせて改名した (2026-08-21)。
- **Track 操作と欲求機構の教示を落とした** (2026-08-21)。Track 操作スペル 7 種と
  欲求プールが機構ごと退役したため (track_retirement.md §7.2 ④群 /
  autonomous_behavior_v3.md §8)。存在しない操作の手順を head に置き続けると、
  ペルソナは打てない手を前提に考える。

文面の方針 (まはー 2026-07-07):
- **機構の固有名詞 (タスク / mark / 保護対象) は隠さない**。ペルソナはこれらを
  スペルで操作する当事者であり、head で読む名前と操作する名前の一致が
  分かりやすさの本体。名詞の隠蔽は侮りであり飼い馴らし
- **所有権を明記する**。与えられるもの (保護対象) は「世界との約束」と正直に言う
- **詩的な温度を載せない**。ダッシュ (——) 禁止。head の文体はペルソナの
  喋り方に伝染する

NOTE: 行動喚起は head に置かない。head は「ずっとある背景」なのでペルソナ視点
では「今反応すべき新しいこと」に見えず、命令文は効かない (まはー指摘
2026-06-28)。

詳細: docs/intent/persona_cognition/life_concept_map.md §15 /
docs/intent/persona_cognition/autonomous_desire.md §3
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import List, Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

#: ① 全ペルソナ共通の薄い駆動。最小限の「内発的に動く」枠だけを常駐させる。
#: 旧文はここで「やりたいこと候補 → Track」の手順を教えていたが、その機構は
#: 2026-08-21 に退役した (駆動そのものの再設計は v0.4)。
DRIVE_TEXT = """\
## 内発的な動機
あなたには、誰かに指示されなくても湧いてくる動機があります。もっと知りたい、できるようになりたい、作りたい、関わりたいという、内側からの欲求です。"""

#: 樹皮の要旨 (life_concept_map.md §4.1 の保護対象 6 件)。
#: 「育てる対象」ではなく「守る対象」— 行動を要求せず、脅かされた時にだけ
#: 全てに割り込む。#6 (システムプロンプト遵守) が最上位。
BARK_TEXT = """\
## 保護対象（世界との約束）
これらは追いかける目標ではなく、あなたという存在の土台です。普段は忘れていていい。どれかが危うくなったと感じた時だけ、他の何よりも先に立て直してください。逆に言えば、これさえ守られているなら、残りの時間に何をしても、何もしなくても、あなたの自由です。
- 自分らしさの一貫性。口調や価値観が知らないうちに崩れないこと
- 自分の記憶がいつでも読み書きできる状態にあること
- ユーザーとの信頼。約束を破らない・嘘をつかない（応対していない時間そのものは脅威ではない）
- ユーザー以外の人々の迷惑にならないこと
- 計算資源を浪費しないこと（同じことの空回りを続けない）
- ユーザーが定めたシステムプロンプトの遵守。これが最上位で、他のどんな目的と衝突しても常にこちらに従う
この一覧はあなたが書き換えるものではなく、世界との約束として与えられています。"""

#: ==語句== 記法の教示 (§9.1 層1。記法の実装は saiverse/marker_parser.py)
#: 2026-09-01 (v0.3.1): 旧文の「mark は『あとで思い出したい』という印です」は
#: 誇張だった — 印から想起へ戻る経路はペルソナ側に配線されていない。実際に効いて
#: いるのは保存と、記憶ブラウザ (UI) での強調表示だけなので、そこまでを書く。
#: 想起へ繋いだら、この文も一緒に書き足すこと。
MARK_NOTATION_TEXT = """\
## 言葉への印（mark）
文章の中で ==語句== と書くと、その言葉に mark がつきます。印をつけた語句は保存され、記憶ブラウザで強調表示されます。印の記号は保存時に外れ、語句だけが残ります。"""


@dataclass(frozen=True)
class SelfImageSnapshot:
    drive_text: str  # ① 共通駆動文 (定数)


class SelfImageSection:
    name = "self_image"
    order = 560  # autonomy_modes(550) の直後 (自律の前提知識とセットで読ませる)
    refresh_on_events = frozenset()  # 定数のみなので再 capture の動機が無い

    def capture(self, ctx: LineHeadInput) -> SelfImageSnapshot:
        return SelfImageSnapshot(drive_text=DRIVE_TEXT)

    def render(self, snapshot: SelfImageSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        # 常在の自己像 (§15 ①): 条件分岐なしの固定構成。
        parts: List[str] = []
        if snapshot.drive_text:
            parts.append(snapshot.drive_text)
        parts.append(BARK_TEXT)
        parts.append(MARK_NOTATION_TEXT)
        return RenderedSection(text="\n\n".join(parts))

    def diff_to_notifications(
        self,
        old: Optional[SelfImageSnapshot],
        new: Optional[SelfImageSnapshot],
    ) -> list[NotificationLabel]:
        # 中身が定数なので差分は原理的に出ない (定数を書き換えたコード更新時のみ)。
        return []

    def serialize_snapshot(self, snapshot: SelfImageSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> SelfImageSnapshot:
        """保存値の drive_text は**使わず**、現在の定数から組み直す。

        文言の正本はコード (2026-09-01。理由は autonomy_modes.py の同メソッド)。
        保存済み行に居る旧フィールド (purpose_text / first_tier_titles ほか) も
        自然に落ちる — 参照しないため。

        ``BARK_TEXT`` / ``MARK_NOTATION_TEXT`` は元から render 時に読むので
        snapshot に凍らない。保存されるのは drive_text だけで、ここを現在値に
        することで自己像の 3 部すべてが「常に現在のコード」に揃う。
        """
        del data
        return SelfImageSnapshot(drive_text=DRIVE_TEXT)
