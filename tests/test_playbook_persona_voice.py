"""ペルソナが書いていない文を、本人の発言として記録する段が Playbook に無いこと。

MEMORIZE (役割 assistant) / SPEAK / THINK は、本文をペルソナ本人の発言として
記憶 (SAIMemory) に書く。その本文がツール・EXEC・子 Playbook の結果 (SAIVerse の
コードや別の手順書が組み立てた文) だと、ペルソナは自分が言っていない言葉を
自分の発言として読むことになる — 発話の捏造 (2026-10-14 まはー裁定「ペルソナが
喋ってない言葉が assistant ロールで入るのは重罪」)。

実例: create_building の announce_result が、create_building ツールの返す
「新しいBuilding「…」を作成しました。（作成者: <persona_id>）…」を役割 assistant で
保存していた。機構の結果を記憶に残すときは、役割 user + ``<system>…</system>``
(SAIVerse の記録の名義) で書く。

ペルソナ自身の構造化出力を、記憶に残せる文へ整える段 (例: web_research の
finalize が ``{research_topic}`` / ``{final_overview}`` を見出し付きで並べる) は
対象外 — 中身はペルソナが書いたもので、JSON をそのまま残す方が以後の返事を
JSON に寄せる (まはー 2026-10-14)。この検査は「直前の段が機構の結果を
``state["last"]`` に置いた直後に、その ``{last}`` を本人名義で書く」形だけを拾う。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest

_PLAYBOOK_ROOT = Path(__file__).resolve().parent.parent / "builtin_data" / "playbooks"

# state["last"] を機構の結果で置き換える段
_MECHANISM_RESULT_TYPES = {"tool", "exec", "subplay"}
# 本文を本人の発言として記憶に書く段
_PERSONA_VOICED_TYPES = {"speak", "think", "say"}


def _playbook_files() -> List[Path]:
    return sorted(_PLAYBOOK_ROOT.glob("**/*.json"))


def _predecessors(nodes: List[dict]) -> Dict[str, List[dict]]:
    preds: Dict[str, List[dict]] = {}
    for node in nodes:
        targets: List[str] = []
        nxt = node.get("next")
        if isinstance(nxt, str):
            targets.append(nxt)
        cond = node.get("conditional_next")
        if isinstance(cond, dict):
            targets += [v for v in (cond.get("cases") or {}).values() if isinstance(v, str)]
            if isinstance(cond.get("default"), str):
                targets.append(cond["default"])
        for target in targets:
            preds.setdefault(target, []).append(node)
    return preds


def _writes_last_as_persona(node: dict) -> bool:
    node_type = (node.get("type") or "").lower()
    action = node.get("action") or "{last}"
    if node_type in _PERSONA_VOICED_TYPES:
        return "{last}" in action
    if node_type == "memorize":
        return (node.get("role") or "assistant") == "assistant" and "{last}" in action
    return False


@pytest.mark.parametrize(
    "path", _playbook_files(), ids=lambda p: str(p.relative_to(_PLAYBOOK_ROOT)),
)
def test_mechanism_results_are_not_recorded_as_persona_speech(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = [n for n in data.get("nodes", []) if isinstance(n, dict)]
    preds = _predecessors(nodes)
    offenders = []
    for node in nodes:
        if not _writes_last_as_persona(node):
            continue
        for prev in preds.get(node.get("id"), []):
            if (prev.get("type") or "").lower() in _MECHANISM_RESULT_TYPES:
                offenders.append(f"{prev.get('id')}({prev.get('type')}) -> {node.get('id')}")
    assert not offenders, (
        "機構の結果を本人の発言として記録している段がある "
        "(役割 user + <system>…</system> で書くこと): " + ", ".join(offenders)
    )


def test_the_check_catches_the_original_create_building_shape():
    """検査そのものが、直す前の create_building の形を拾えること。"""
    nodes = [
        {"id": "create", "type": "tool", "action": "create_building", "next": "announce"},
        {"id": "announce", "type": "memorize", "action": "{last}", "role": "assistant"},
    ]
    preds = _predecessors(nodes)
    assert _writes_last_as_persona(nodes[1])
    assert [p["id"] for p in preds["announce"]] == ["create"]
