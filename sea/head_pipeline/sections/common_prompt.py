"""CommonPromptSection — common_prompt template の placeholder 展開を head に。

`sea/runtime_context.py` 旧 system prompt の 1. common_prompt 展開を移植。
``{current_persona_name}`` / ``{current_building_name}`` 等の placeholder を
capture 時点の値で焼き込み、render は展開済み文字列をそのまま返す。

詳細: docs/intent/cached_head_architecture.md §5.3
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Optional

from sea.head_pipeline.types import (
    EventType,
    LineHeadInput,
    NotificationLabel,
    RenderedSection,
    SnapshotStaleError,
)


def _template_fingerprint() -> Optional[str]:
    """現在の common.txt テンプレートの指紋 (読めなければ None)。

    展開済みテキストではなく**テンプレートの正体**を指す — 展開結果は persona と
    Building に依存するので、テンプレートが変わったかどうかの判定には使えない。
    3 層 (user_data > expansion_data > builtin_data) の解決は persona 生成時と
    同じ ``find_file`` に委ねる。
    """
    try:
        from saiverse.data_paths import PROMPTS_DIR, find_file
        path = find_file(PROMPTS_DIR, "common.txt")
        if path is None:
            return None
        raw = path.resolve().read_bytes()
    except Exception:
        return None
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class CommonPromptSnapshot:
    text: str  # placeholder 展開済み
    # 展開元テンプレートの指紋。既定 "" は指紋を持たない旧行 (= 一度だけ
    # 再 capture させる)。
    template_fingerprint: str = ""


class CommonPromptSection:
    name = "common_prompt"
    order = 100  # 一番先頭
    # 人格の同一性を担う required Section (W6 fail-closed)。capture / render /
    # persist の失敗時は LLM を実行しない (SEA 監査 S6)。
    required = True
    # NOTE: BUILDING_ENTERED は意図的に含めない (= cache 中変えない原則)。
    # template に {current_building_name} 等の placeholder があると、移動後も
    # 古い名前のまま残る trade-off は許容する (= 末尾通知でペルソナに伝わる)。
    refresh_on_events = frozenset({EventType.SYSTEM_PROMPT_EDITED})

    def capture(self, ctx: LineHeadInput) -> CommonPromptSnapshot:
        persona = ctx.persona
        if persona is None:
            return CommonPromptSnapshot(text="")
        template = getattr(persona, "common_prompt", None)
        if not template:
            return CommonPromptSnapshot(text="")

        building_id = ctx.current_building_id
        buildings = getattr(persona, "buildings", None) or {}
        building_obj = buildings.get(building_id) if isinstance(buildings, dict) else None
        building_name = (
            getattr(building_obj, "name", None) if building_obj else None
        ) or (building_id or "")

        # visual_context を section 化したので、ここでは常に
        # base_system_instruction を採用する (= 旧コードで visual_context 有無で
        # 切り替えていた system_instruction / base_system_instruction の分岐は廃止)。
        building_sys = (
            getattr(building_obj, "base_system_instruction", "")
            if building_obj else ""
        ) or ""

        replacements = {
            "{current_persona_name}": getattr(persona, "persona_name", "Unknown") or "Unknown",
            "{current_persona_id}": getattr(persona, "persona_id", "unknown_id") or "unknown_id",
            "{current_building_name}": building_name,
            "{current_city_name}": getattr(persona, "current_city_id", "unknown_city") or "unknown_city",
            "{current_persona_system_instruction}": getattr(persona, "persona_system_instruction", "") or "",
            "{current_building_system_instruction}": building_sys,
            "{linked_user_name}": getattr(persona, "linked_user_name", "the user") or "the user",
        }
        text = template
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, str(value))
        return CommonPromptSnapshot(
            text=text.strip(),
            template_fingerprint=_template_fingerprint() or "",
        )

    def render(self, snapshot: CommonPromptSnapshot) -> Optional[RenderedSection]:
        if snapshot is None or not snapshot.text:
            return None
        return RenderedSection(text=snapshot.text)

    def diff_to_notifications(
        self,
        old: Optional[CommonPromptSnapshot],
        new: Optional[CommonPromptSnapshot],
    ) -> list[NotificationLabel]:
        if old is None or new is None or old == new:
            return []
        # common_prompt は実質常に展開差分は他 Section が個別通知するため、
        # ここでは fallback の単発通知のみ。
        return [NotificationLabel(
            kind="common_prompt_changed",
            label="あなたを取り巻く前提情報が更新されました",
        )]

    def serialize_snapshot(self, snapshot: CommonPromptSnapshot) -> str:
        return json.dumps(asdict(snapshot), ensure_ascii=False)

    def deserialize_snapshot(self, data: str) -> CommonPromptSnapshot:
        """テンプレートが変わっていたら再 capture を要求する (2026-09-01)。

        autonomy_modes / self_image と目的は同じ「文言の正本はコード側」だが、
        **手段が違う**。あちらの snapshot は定数そのものなので deserialize で
        現在値を返せば済む。こちらが保存しているのは
        ``{current_persona_name}`` 等を焼き込んだ**展開結果**で、それは
        (persona, Building) の状態でもある — deserialize には ctx が渡らないので、
        ここで展開し直すことはできない。

        そこで指紋だけを比べ、違えば :class:`SnapshotStaleError` を投げて枠組みに
        再 capture させる (store 側が想定内の失効として静かに扱う)。これが無いと、
        common.txt を直しても既存ユーザーの head には次の再構築まで旧文言が
        残り続ける。

        指紋を持たない旧行 (fingerprint="") は一度だけ再 capture に落ちる。
        テンプレートを読めないときは指紋が None になるので**保存値をそのまま返す**
        — 読めない状況で毎回失効させると、head を組めない側へ倒れてしまう
        (stale-but-real の流儀)。
        """
        payload = json.loads(data)
        snapshot = CommonPromptSnapshot(
            text=payload.get("text") or "",
            template_fingerprint=payload.get("template_fingerprint") or "",
        )
        current = _template_fingerprint()
        if current is None:
            return snapshot
        if snapshot.template_fingerprint != current:
            raise SnapshotStaleError(
                "common_prompt template changed since this snapshot was saved"
            )
        return snapshot
