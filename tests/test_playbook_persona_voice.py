"""ペルソナが書いていない文を、本人の発言として記録する段が Playbook に無いこと。

MEMORIZE (役割 assistant) / SPEAK / THINK / SAY は、本文をペルソナ本人の発言として
記憶 (SAIMemory) に書く。その本文がツール・EXEC・子 Playbook の結果 (SAIVerse の
コードや別の手順書が組み立てた文) だと、ペルソナは自分が言っていない言葉を
自分の発言として読むことになる — 発話の捏造 (2026-10-14 まはー裁定「ペルソナが
喋ってない言葉が assistant ロールで入るのは重罪」)。

実例: create_building の announce_result が、create_building ツールの返す
「新しいBuilding「…」を作成しました。（作成者: <persona_id>）…」を役割 assistant で
保存していた。機構の結果を記憶に残すときは、役割 user + ``<system>…</system>``
(SAIVerse の記録の名義) で書く。

ペルソナ自身の構造化出力を、記憶に残せる文へ整える段 (例: public の
web_research の finalize が ``{research_topic}`` / ``{final_overview}`` を見出し付きで
並べる) は対象外 — 中身はペルソナが書いたもので、JSON をそのまま残す方が以後の
返事を JSON に寄せる (まはー 2026-10-14)。

検査が拾う形は二つ:

1. 機構の段 (tool / tool_call / exec / subplay) が ``state["last"]`` に置いた結果を、
   その後の本人名義の段が読む。間に pass / set を挟んでも辿る。SPEAK / THINK /
   SAY は文面に関係なく ``state["last"]`` を出し、MEMORIZE は action に ``{last}``
   を書いたときに読む。
2. 機構の段が ``output_key`` / ``output_keys`` に置いた結果を、本人名義の
   MEMORIZE が ``{key}`` (または ``{key.field}``) で読む。

決まり文句 (例: 「フラグメント整理完了」) を本人名義で書く形は、整形の例外と
機械的に区別できないので、この検査では拾わない (2026-10-14 に archive を手で
直した)。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set

import pytest

_PLAYBOOK_ROOT = Path(__file__).resolve().parent.parent / "builtin_data" / "playbooks"

# state["last"] や出力キーを機構の結果で埋める段
_MECHANISM_TYPES = {"tool", "tool_call", "exec", "subplay"}
# state["last"] を書き換えずに素通しする段 (遡るときに辿り抜ける)
_TRANSPARENT_TYPES = {"pass", "set"}
# 文面に関係なく state["last"] を本人の発言として出す段
_LAST_VOICED_TYPES = {"speak", "think", "say"}


def _playbook_files() -> List[Path]:
    return sorted(_PLAYBOOK_ROOT.glob("**/*.json"))


def _targets(node: dict) -> List[str]:
    out: List[str] = []
    nxt = node.get("next")
    if isinstance(nxt, str):
        out.append(nxt)
    cond = node.get("conditional_next")
    if isinstance(cond, dict):
        out += [v for v in (cond.get("cases") or {}).values() if isinstance(v, str)]
        if isinstance(cond.get("default"), str):
            out.append(cond["default"])
    return out


def _predecessors(nodes: List[dict]) -> Dict[str, List[dict]]:
    preds: Dict[str, List[dict]] = {}
    for node in nodes:
        for target in _targets(node):
            preds.setdefault(target, []).append(node)
    return preds


def _type(node: dict) -> str:
    return (node.get("type") or "").lower()


def _last_sources(node_id: str, preds: Dict[str, List[dict]]) -> List[dict]:
    """``node_id`` に届く ``state["last"]`` を最後に書いた段 (pass / set を辿り抜ける)。"""
    found: List[dict] = []
    seen: Set[str] = set()
    stack = list(preds.get(node_id, []))
    while stack:
        prev = stack.pop()
        pid = prev.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        if _type(prev) in _TRANSPARENT_TYPES:
            stack.extend(preds.get(pid, []))
        else:
            found.append(prev)
    return found


def _mechanism_output_keys(nodes: List[dict]) -> Set[str]:
    keys: Set[str] = set()
    for node in nodes:
        if _type(node) not in _MECHANISM_TYPES:
            continue
        if isinstance(node.get("output_key"), str):
            keys.add(node["output_key"])
        for key in node.get("output_keys") or []:
            if isinstance(key, str):
                keys.add(key)
    return keys


def _referenced_keys(action: str) -> Set[str]:
    return {m.split(".", 1)[0] for m in re.findall(r"\{([\w.]+)\}", action)}


def find_offenders(nodes: List[dict]) -> List[str]:
    preds = _predecessors(nodes)
    mech_keys = _mechanism_output_keys(nodes)
    offenders: List[str] = []
    for node in nodes:
        node_type = _type(node)
        if node_type in _LAST_VOICED_TYPES:
            reads_last, refs = True, set()
        elif node_type == "memorize" and (node.get("role") or "assistant") == "assistant":
            # action を省略した MEMORIZE は実行時に "{last}" を保存する
            # (sea/runtime_engine.py の lg_memorize_node: action or "{last}")。
            refs = _referenced_keys(node.get("action") or "{last}")
            reads_last = "last" in refs
        else:
            continue
        if reads_last:
            for src in _last_sources(node.get("id"), preds):
                if _type(src) in _MECHANISM_TYPES:
                    offenders.append(f"{src.get('id')}({_type(src)}) -> {node.get('id')}")
        for key in sorted((refs - {"last"}) & mech_keys):
            offenders.append(f"{{{key}}} -> {node.get('id')}")
    return offenders


def test_playbooks_are_found():
    """glob が空になって全件が黙って通る、を防ぐ。"""
    assert len(_playbook_files()) > 10


@pytest.mark.parametrize(
    "path", _playbook_files(), ids=lambda p: str(p.relative_to(_PLAYBOOK_ROOT)),
)
def test_mechanism_results_are_not_recorded_as_persona_speech(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = [n for n in data.get("nodes", []) if isinstance(n, dict)]
    offenders = find_offenders(nodes)
    assert not offenders, (
        "機構の結果を本人の発言として記録している段がある "
        "(役割 user + <system>…</system> で書くこと): " + ", ".join(offenders)
    )


class TestTheCheckItself:
    """検査そのものが、拾うべき形を拾い、例外を咎めないこと。"""

    def test_original_create_building_shape(self):
        nodes = [
            {"id": "create", "type": "tool", "action": "create_building", "next": "announce"},
            {"id": "announce", "type": "memorize", "action": "{last}", "role": "assistant"},
        ]
        assert find_offenders(nodes) == ["create(tool) -> announce"]

    def test_memorize_without_action_saves_last(self):
        """action を省略した MEMORIZE (役割も既定の assistant) は {last} を保存する。"""
        nodes = [
            {"id": "create", "type": "tool", "action": "create_building", "next": "keep"},
            {"id": "keep", "type": "memorize"},
        ]
        assert find_offenders(nodes) == ["create(tool) -> keep"]

    def test_tool_call_through_pass_into_speak(self):
        nodes = [
            {"id": "gather", "type": "tool_call", "call_source": "fc", "next": "gate"},
            {"id": "gate", "type": "pass",
             "conditional_next": {"field": "ok", "cases": {"default": "say_it"}}},
            {"id": "say_it", "type": "speak"},
        ]
        assert find_offenders(nodes) == ["gather(tool_call) -> say_it"]

    def test_named_mechanism_output_in_assistant_memorize(self):
        nodes = [
            {"id": "gather", "type": "tool", "action": "x", "output_key": "tool_result",
             "next": "echo"},
            {"id": "echo", "type": "memorize", "action": "作成完了: {tool_result.text}",
             "role": "assistant"},
        ]
        assert find_offenders(nodes) == ["{tool_result} -> echo"]

    def test_mechanism_result_recorded_as_system_is_fine(self):
        nodes = [
            {"id": "create", "type": "tool", "action": "x", "next": "record"},
            {"id": "record", "type": "memorize", "action": "<system>{last}</system>",
             "role": "user"},
        ]
        assert find_offenders(nodes) == []

    def test_formatting_the_personas_own_output_is_fine(self):
        """整形の例外: ペルソナ自身の構造化出力を見出し付きで残すのは咎めない。"""
        nodes = [
            {"id": "decide", "type": "llm", "output_key": "plan", "next": "finalize"},
            {"id": "finalize", "type": "memorize",
             "action": "## 概要\n{plan.overview}\n{last}", "role": "assistant"},
        ]
        assert find_offenders(nodes) == []
