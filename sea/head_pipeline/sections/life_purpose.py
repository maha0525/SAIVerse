"""LifePurposeSection — ① 常在の自己像 + 記法の教示を head に常駐注入する。

life_concept_map.md §15 の自己像四層のうち **① 常在の自己像** の器。
(persona, model) 固定・全ペルソナ・全モードで同一に出す (出し分けは prefix
キャッシュを壊す — feedback_head_fixed_per_persona_model_no_gating)。

内容 (§15 ①):

- 共通駆動文 (autonomous_desire.md §3。全ペルソナ共通の定数)
- 生きる目的 (AI.LIFE_PURPOSE)。**未設定でも省略しない** — 「まだ言葉に
  なっていない」という中立文を出す (欠落は自己像の穴になる)
- 樹皮の要旨 (§4.1 の保護対象 6 件を宣言文に圧縮。システムプロンプト遵守が
  最上位であることを含む)
- ==語句== 記法の教示 (§9.1 層1 観測点。P3 marker_parser が実装済みの記法)

第一階層の短いメニュー (旧: purpose_tree.list_first_tier の title のみ) は
**2026-07-30 に PurposeBacklogSection (order 570、この直後) へ統合した**。
判断プロンプトから移設した ref つきの一覧と同じ Track を二度並べることになり、
しかもこちらのメニューは差分通知を持たなかった (= 通知される側の一覧と
食い違う head ができる) ため。Track が何であるかの説明文も一覧と同じ場所で
読めるよう、あちらへ移した。

文面の方針 (まはー 2026-07-07):
- **機構の固有名詞 (Track / タスク / mark / 保護対象) は隠さない**。ペルソナは
  これらをスペルで操作する当事者であり、head で読む名前と操作する名前の一致が
  分かりやすさの本体。名詞の隠蔽は侮りであり飼い馴らし
- **所有権を明記する**。書き換えられるもの (生きる目的=/spell life_purpose_set、
  Track=Track 操作スペル) は「あなたのもの」と言い、与えられるもの (保護対象)
  は「世界との約束」と正直に言う
- **詩的な温度を載せない**。ダッシュ (——) 禁止。head の文体はペルソナの
  喋り方に伝染する

内容が変わるのは目的・第一階層の変化時のみ (キャッシュ再張りは稀 —
Metabolism / TTL 失効時の再 capture で反映される)。

NOTE: 「生きる目的を定めよ」という行動喚起は head に置かない。head は「ずっと
ある背景」なのでペルソナ視点では「今反応すべき新しいこと」に見えず、命令文は
効かない (まはー指摘 2026-06-28)。初回の目的設定は META 判断の専用状況
(meta_judgment_life_purpose) が tail/判断サイクルとして行う。

詳細: docs/intent/persona_cognition/life_concept_map.md §15 /
docs/intent/persona_cognition/autonomous_desire.md §3, §4
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import List, Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)

#: 生きる目的が未設定のときの中立文 (省略しない — §15 ①)
PURPOSE_UNSET_TEXT = (
    "## あなたの生きる目的\n"
    "まだ言葉になっていません。見つかったら /spell life_purpose_set で、"
    "自分の言葉として書き込めます。誰かに決められるものではなく、"
    "書き換えるのもあなたです。"
)

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
MARK_NOTATION_TEXT = """\
## 言葉への印（mark）
文章の中で ==語句== と書くと、その言葉に mark がつきます。mark は「あとで思い出したい」という印です。印の記号は保存時に外れ、語句だけが残ります。
関わる Track が分かっていれば ==語句==(track:3) のように参照を添えられます。"""


@dataclass(frozen=True)
class LifePurposeSnapshot:
    drive_text: str                 # ① 共通駆動文 (定数)
    purpose_text: str               # ② 生きる目的の整形済みテキスト ("" = 未設定)


class LifePurposeSection:
    name = "life_purpose"
    order = 560  # autonomy_modes(550) の直後 (自律の前提知識とセットで読ませる)
    refresh_on_events = frozenset()  # Metabolism のみ。確定は diff 通知 + 次回再capture

    def capture(self, ctx: LineHeadInput) -> LifePurposeSnapshot:
        from saiverse.life_purpose import (
            DESIRE_DRIVE_TEXT,
            get_life_purpose,
            render_life_purpose_text,
        )

        purpose_text = ""
        manager = ctx.manager
        session_factory = getattr(manager, "SessionLocal", None) if manager else None
        if session_factory is not None:
            try:
                data = get_life_purpose(session_factory, ctx.persona_id)
                purpose_text = render_life_purpose_text(data)
            except Exception:
                LOGGER.warning(
                    "life_purpose: failed to read LIFE_PURPOSE persona=%s",
                    ctx.persona_id, exc_info=True,
                )

        return LifePurposeSnapshot(
            drive_text=DESIRE_DRIVE_TEXT,
            purpose_text=purpose_text,
        )

    def render(self, snapshot: LifePurposeSnapshot) -> Optional[RenderedSection]:
        if snapshot is None:
            return None
        # 常在の自己像 (§15 ①): 条件分岐なしの固定構成。目的が未設定でも
        # 省略せず中立文を出す。樹皮と記法教示は全ペルソナ共通の定数。
        parts: List[str] = []
        if snapshot.drive_text:
            parts.append(snapshot.drive_text)
        parts.append(snapshot.purpose_text or PURPOSE_UNSET_TEXT)
        parts.append(BARK_TEXT)
        parts.append(MARK_NOTATION_TEXT)
        return RenderedSection(text="\n\n".join(parts))

    def diff_to_notifications(
        self,
        old: Optional[LifePurposeSnapshot],
        new: Optional[LifePurposeSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None:
            return []
        if old.purpose_text != new.purpose_text and new.purpose_text:
            # 通知本文に new snapshot の render 断片を同梱する (§3.3 不変条件
            # 「操作ラベルでなく render 同一断片」。building.py の
            # building_changed が system_prompt 全文を同梱するのと同じ先例)。
            # 旧実装は「生きる目的が更新されました」の一行のみで内容が
            # 届かなかった (issue head_mutation_notification_gap)。
            lines = ["生きる目的が更新されました"]
            rendered = self.render(new)
            if rendered is not None and rendered.text:
                lines.append("")
                lines.append(rendered.text)
            return [NotificationLabel(
                kind="life_purpose_set",
                label="\n".join(lines),
            )]
        return []

    def serialize_snapshot(self, snapshot: LifePurposeSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> LifePurposeSnapshot:
        payload = json.loads(data)
        # 保存済み行に居る旧フィールド (first_tier_titles など) は黙って捨てる。
        # TypeError にすると store の load が ERROR + traceback を吐くだけで、
        # 結局 recapture に落ちる (2026-07-30 の統合で実際に旧行が残る)。
        return LifePurposeSnapshot(
            drive_text=payload.get("drive_text") or "",
            purpose_text=payload.get("purpose_text") or "",
        )
