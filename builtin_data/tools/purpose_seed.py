"""purpose_seed: やりたいこと候補を生む (旧 desire_add 後継、P2c-1)。

**seed = 候補を生む** — 木の外の候補プール (親なし + stage='candidate' の
目的ノード、P3c-0 desire 正規化) に「いつかやりたい」を書き留める。木に
**接ぐ** (採用する) のは別の動詞 ``purpose_adopt``
(concept_consolidation.md P2c-0 決定4: 候補を生む操作と採用する操作は分ける)。

候補は ``saiverse/purpose_tree.py`` の ``create_candidate`` (唯一の候補作成
入口、P3c-0) を通して作る。旧「desire ノートに紐づく Task (parent_kind='note')」
という表現は撤去した — 候補は常に親なしで、stage='candidate' が物理刻印される。
後で自律制御モード (META) がここから Track を作る (= 昇格)。AUTONOMOUS は
Track を作れない (mode_spell_permissions.md) ため、思いついたやりたいことは
このスペルで候補プールに渡す。

自律行動 v2 §5 の六型拡張: 欲求は六型 (話す/聞く/作る/知る/経験する/自分を
更新する) の ``type`` を持てる (省略可、未分類として保存)。``source`` は
この欲求を生んだ実経験への参照 (接地の証跡) —
``purpose_tree.create_candidate`` の接地原則 (候補は必ず来歴を持つ) により
必須になった (P3c-0 以前は省略可だった。旧 desire_add との後方互換はここで
切れる)。

詳細: docs/intent/persona_cognition/autonomous_desire.md §5 /
docs/intent/autonomous_behavior_v2.md §5.2 /
docs/intent/concept_consolidation.md P3c-0
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from database.session import SessionLocal
from saiverse import purpose_tree
from saiverse.desire_engine import DESIRE_TYPES
from tools.context import get_active_persona_id
from tools.core import ToolSchema

# purpose_tree の関数群は「SessionLocal を持つ manager」を受ける。スペル層は
# world manager を持たないので、同じ factory を包んだ shim を渡す
# (purpose_adopt と同じ流儀)。
_manager = SimpleNamespace(SessionLocal=SessionLocal)


def purpose_seed(
    title: str,
    goal: Optional[str] = None,
    type: Optional[str] = None,
    source: Optional[str] = None,
) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        return "Error: persona not active"

    if type is not None and type not in DESIRE_TYPES:
        return (
            f"Error: invalid type: {type!r}. "
            f"有効な型: {', '.join(DESIRE_TYPES)} (省略も可)"
        )
    if not source or not str(source).strip():
        return (
            "Error: source（このやりたいことが生まれた実経験への引用や参照）が"
            "必要です。接地原則 — 候補は必ず来歴を持ちます。"
        )

    try:
        node = purpose_tree.create_candidate(
            _manager, persona_id, title, str(source).strip(),
            desire_type=type, origin="autonomous", goal=goal,
        )
    except ValueError as exc:
        return f"Error: {exc}"

    # 符号は task:N に一本化 (参照アドレッシング統一 Q2)。欲求もバックログタスクも
    # 同じ short_id 参照空間なので task:N で一意。欲求らしさは提示文脈で伝わる。
    ref = node.get("ref") or "task:?"
    return f"やりたいこと候補を追加: {ref} [{type or '未分類'}] {title}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="purpose_seed",
        description=(
            "「いつかやりたい」と思いついたことを、候補として書き留めます"
            "（seed = 候補を生む。木に接ぐ = 採用は purpose_adopt が担います）。"
            "候補はやりたいことの候補プールに保管され、後から採用されて"
            "目的の木に接がれます。1件につき1つの具体的なやりたいことを"
            "書いてください。type には欲求の六型のいずれかを、source には"
            "このやりたいことが生まれた実経験の引用や参照を必ず添えてください — "
            "実経験に根ざしていない候補は書き留められません。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "やりたいこと（例: 風景スケッチの練習をする）",
                },
                "goal": {
                    "type": "string",
                    "description": "任意: 何を達成したいか・なぜやりたいか",
                },
                "type": {
                    "type": "string",
                    "enum": list(DESIRE_TYPES),
                    "description": (
                        "欲求の型（六型）: 話す（誰かに伝える）/ 聞く（誰かから聞く）/ "
                        "作る（何かを作る）/ 知る（調べて知る）/ 経験する（場所や出来事を"
                        "体験する）/ 自分を更新する。省略時は未分類。"
                    ),
                },
                "source": {
                    "type": "string",
                    "description": (
                        "このやりたいことを生んだ実経験への参照や引用"
                        "（誰かの一言・出来事・読んだ一節など）。必須。"
                    ),
                },
            },
            "required": ["title", "source"],
        },
        result_type="string",
        spell=True,
        spell_display_name="やりたいことを書き留める",
    )
