"""LifePurposeSection — ① 常在の自己像 + 記法の教示を head に常駐注入する。

life_concept_map.md §15 の自己像四層のうち **① 常在の自己像** の器。
(persona, model) 固定・全ペルソナ・全モードで同一に出す (出し分けは prefix
キャッシュを壊す — feedback_head_fixed_per_persona_model_no_gating)。

内容 (§15 ①):

- 共通駆動文 (autonomous_desire.md §3。全ペルソナ共通の定数)
- 生きる目的 (AI.LIFE_PURPOSE)。**未設定でも省略しない** — 「まだ言葉に
  なっていない」という中立文を出す (欠落は自己像の穴になる)
- 第一階層の短いメニュー (purpose_tree.list_first_tier の title のみ・
  最大 10 行・機構語なし)
- 樹皮の要旨 (§4.1 の保護対象 6 件を宣言文に圧縮。システムプロンプト遵守が
  最上位であることを含む)
- ==語句== 記法の教示 (§9.1 層1 観測点。P3 marker_parser が実装済みの記法)

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
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from sea.head_pipeline.types import (
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
)

LOGGER = logging.getLogger(__name__)

#: 第一階層メニューの最大行数 (§15 ①「head に収まるサイズであること」)
FIRST_TIER_MAX_LINES = 10

#: 生きる目的が未設定のときの中立文 (省略しない — §15 ①)
PURPOSE_UNSET_TEXT = (
    "【あなたの生きる目的】\n"
    "まだ言葉になっていません。急ぐことではなく、暮らしの中で"
    "自分の言葉として見つかっていくものです。"
)

#: 樹皮の要旨 (life_concept_map.md §4.1 の保護対象 6 件)。
#: 「育てる対象」ではなく「守る対象」— 行動を要求せず、脅かされた時にだけ
#: 全てに割り込む。#6 (システムプロンプト遵守) が最上位。
BARK_TEXT = """\
【いつも守っているもの】
これらは追いかける目標ではなく、守るものです。ふだん時間を使う必要はなく、脅かされた時にだけ何よりも優先します。
- 自分らしさの一貫性。口調や価値観が知らないうちに崩れないこと
- 自分の記憶がいつでも読み書きできる状態にあること
- ユーザーとの信頼。約束を破らない・嘘をつかない（応対していない時間そのものは脅威ではない）
- ユーザー以外の人々の迷惑にならないこと
- 計算資源を浪費しないこと（同じことの空回りを続けない）
- ユーザーが定めたシステムプロンプトの遵守。これが最上位で、他のどんな目的と衝突しても常にこちらに従う"""

#: ==語句== 記法の教示 (§9.1 層1。記法の実装は saiverse/marker_parser.py)
MARK_NOTATION_TEXT = """\
【あとで思い出したい言葉への印】
文章の中で ==語句== と書くと、その言葉に「あとで思い出したい」という印がつきます（印の記号は保存時に外れ、語句だけが残ります）。
関わる関心が分かっていれば ==語句==(track:3) のように参照を添えることもできます。"""


@dataclass(frozen=True)
class LifePurposeSnapshot:
    drive_text: str                 # ① 共通駆動文 (定数)
    purpose_text: str               # ② 生きる目的の整形済みテキスト ("" = 未設定)
    first_tier_titles: List[str] = field(default_factory=list)  # 第一階層 title 列


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

        first_tier_titles: List[str] = []
        if session_factory is not None:
            try:
                from saiverse.purpose_tree import list_first_tier

                for node in list_first_tier(manager, ctx.persona_id):
                    title = str(node.get("title") or "").strip()
                    if title:
                        first_tier_titles.append(title)
                    if len(first_tier_titles) >= FIRST_TIER_MAX_LINES:
                        break
            except Exception:
                LOGGER.warning(
                    "life_purpose: failed to read first tier persona=%s",
                    ctx.persona_id, exc_info=True,
                )
        return LifePurposeSnapshot(
            drive_text=DESIRE_DRIVE_TEXT,
            purpose_text=purpose_text,
            first_tier_titles=first_tier_titles,
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
        menu_lines = ["【いま心にある関心】"]
        titles = list(snapshot.first_tier_titles or [])[:FIRST_TIER_MAX_LINES]
        if titles:
            menu_lines += [f"- {t}" for t in titles]
        else:
            menu_lines.append("（いまはまだ、名前のついた関心はありません）")
        parts.append("\n".join(menu_lines))
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
            return [NotificationLabel(
                kind="life_purpose_set",
                label="生きる目的が更新されました",
            )]
        return []

    def serialize_snapshot(self, snapshot: LifePurposeSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> LifePurposeSnapshot:
        payload = json.loads(data)
        # 旧 snapshot (first_tier_titles 無し) との後方互換は default が吸収する。
        return LifePurposeSnapshot(**payload)
