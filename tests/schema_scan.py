"""Gemini へ向く JSON Schema を、リポジトリ全体から機械で拾い集めるヘルパ。

出自: docs/issues/sluice_structured_output_digit_loop.md (2026-08-24)。
スルースの構造化出力が整数欄で数字のループに入り、本番で 7 回連続の失敗を
起こした。JSON の数値リテラルは文法で閉じられない (桁をいくら並べても文法
違反にならない) ので、制約付きデコードがその中でループに入ると何も止められ
ない。この事故は「スルースの型」の事故ではなく、**Gemini に数値欄を持つ型を
向ける限りどこでも起きうる** 事故なので、見張りも型ひとつではなく経路全体に
かける必要がある。

ここは「探す道具」だけを置く場所で、判定 (何を違反とみなすか / 既知の違反は
どれか) は tests/test_response_schema_no_numeric_fields.py が持つ。

型が Gemini の制約付きデコードに届く経路は 3 つある:

経路 A — Playbook JSON の ``response_schema``
    builtin_data/playbooks/**/*.json の LLM ノードに直接書かれた型。
    archive/ は退役済みなので走査しない。

経路 B — Python 側で組み立てる型
    ``sea/sluice.py`` の ``_RESPONSE_SCHEMA``、``saiverse/judgment_points.py``
    の判断点スキーマなど。実行時に enum を注入するものがあり、関数を呼ばないと
    完成した型にならないので、**ソースを構文解析して型の literal を探す**形で
    走査する (呼び出しに必要な manager を偽装しなくても、新しい書き手が増えた
    瞬間に対象へ入る)。

経路 C — スペルの引数の型が response_schema に化ける
    builtin_data/playbooks/public/spell_args_decider.json が
    ``"response_schema_source": "spell:{spell_name}"`` を持ち、ランタイム
    (sea/runtime_llm.py ``_resolve_response_schema_source``) が
    ``SPELL_TOOL_SCHEMAS[spell_name].parameters`` を解決する。つまり
    ``spell=True`` のツールの引数の型は、そのまま構造化出力の型になる。

走査できない範囲 (この道具の穴。設計上の割り切り):

- ``expansion_data/<addon>/`` のアドオンが持ち込むスペル (native tool /
  MCP / 合成アクション) は各利用者の環境ごとに違い、リポジトリからは直せない。
  特に ``saiverse/composite_actions.py`` の ``_action_param_schema`` は
  アドオンの JSON に書かれた ``integer`` / ``number`` をそのまま引数の型に
  写すので、数値欄を持つスペルをアドオンが作れてしまう。
- ``~/.saiverse/user_data/`` の利用者定義の Playbook・ツール (同上)。
- DB の ``playbooks`` テーブルに直接入った型 (builtin の JSON が正本)。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 数字のループに入りうる型。文字列・enum・真偽値は文法で閉じられる
#: (``"`` / 候補 / 二択で終わる) のでこの罠に入らない。
NUMERIC_TYPES = ("integer", "number")

PLAYBOOK_DIR = PROJECT_ROOT / "builtin_data" / "playbooks"
BUILTIN_TOOLS_DIR = PROJECT_ROOT / "builtin_data" / "tools"

#: 経路 B の走査から外すディレクトリ。
#: - ``.`` 始まり: .venv / scripts/.searxng-venv などの持ち込みの環境
#: - ``site-packages``: 同上 (第三者のコードは私たちが直せない)
#: - ``tests``: 検査自身が持つ見本の型で自分が落ちないように
#: - ``builtin_data/tools``: 経路 C が ``spell`` の旗ごと見るので二重に数えない
#:   (ここで一緒に読むと、LLM に型が渡らない spell=False のツールまで違反に
#:    見えてしまう)
_SKIP_DIR_NAMES = frozenset({
    "temp", "node_modules", "test_data", "frontend", "docs", "expansion_data",
    "tests", "__pycache__", "site-packages", "backups", "logs",
})


class Finding(NamedTuple):
    """見つかった数値欄 1 つ。"""

    route: str          # "playbook" / "python" / "spell"
    source: str         # ファイル・ノード・スペルの識別
    field_path: str     # 型の中の位置 ($.core_updates[].memory_ref 形式)
    field_type: str     # "integer" / "number"
    location: str       # 人が開くための場所 (ファイル:行)

    @property
    def key(self) -> str:
        """既知の違反リストと突き合わせるための識別子 (行番号を含めない)。"""
        return f"{self.route}:{self.source} {self.field_path}"


class ScanResult(NamedTuple):
    """1 経路の走査結果。"""

    findings: List[Finding]
    scanned: int          # 走査できた型の数 (見張りが空振りしていないかの目印)
    problems: List[str]   # 走査そのものが失敗した箇所 (= 見張りに開いた穴)


# ---------------------------------------------------------------------------
# 型 (dict) を辿る
# ---------------------------------------------------------------------------


def walk_schema(node: Any, path: str = "$") -> Iterator[Tuple[str, Any]]:
    """JSON Schema の全ノードを ``(パス, ノード)`` で辿る。

    ``properties`` の下も ``items`` の下も、``anyOf`` / ``oneOf`` / ``allOf``
    の分岐の中も、``$defs`` の定義も辿る。分岐の中だけ整数のままだった、と
    いう見落としを作らないため。
    """
    yield path, node
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        for key, child in props.items():
            yield from walk_schema(child, f"{path}.{key}")
    items = node.get("items")
    if isinstance(items, dict):
        yield from walk_schema(items, f"{path}[]")
    elif isinstance(items, list):
        for index, child in enumerate(items):
            yield from walk_schema(child, f"{path}[{index}]")
    prefix_items = node.get("prefixItems")
    if isinstance(prefix_items, list):
        for index, child in enumerate(prefix_items):
            yield from walk_schema(child, f"{path}[{index}]")
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = node.get(keyword)
        if isinstance(branches, list):
            for index, child in enumerate(branches):
                yield from walk_schema(child, f"{path}.{keyword}[{index}]")
    for keyword in ("$defs", "definitions"):
        defs = node.get(keyword)
        if isinstance(defs, dict):
            for key, child in defs.items():
                yield from walk_schema(child, f"{path}.{keyword}.{key}")
    extra = node.get("additionalProperties")
    if isinstance(extra, dict):
        yield from walk_schema(extra, f"{path}.*")


def numeric_type_of(node: Any) -> Optional[str]:
    """ノードが数値型ならその型名を、そうでなければ None を返す。

    ``"type": ["integer", "null"]`` のような並記も数値欄として扱う
    (null を許しても整数の桁を並べる道は開いたまま)。
    """
    if not isinstance(node, dict):
        return None
    declared = node.get("type")
    if isinstance(declared, str):
        return declared if declared in NUMERIC_TYPES else None
    if isinstance(declared, (list, tuple)):
        for entry in declared:
            if entry in NUMERIC_TYPES:
                return str(entry)
    return None


def numeric_fields(schema: Any) -> List[Tuple[str, str]]:
    """型の中の数値欄を ``[(パス, 型名), ...]`` で返す。"""
    found: List[Tuple[str, str]] = []
    for path, node in walk_schema(schema):
        kind = numeric_type_of(node)
        if kind is not None:
            found.append((path, kind))
    return found


# ---------------------------------------------------------------------------
# 経路 A — Playbook JSON の response_schema
# ---------------------------------------------------------------------------


def _playbook_nodes(data: Any) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Playbook の nodes を ``(ノード ID, ノード)`` で返す (dict 形式・list 形式の両方)。"""
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if isinstance(nodes, dict):
        for node_id, node in nodes.items():
            if isinstance(node, dict):
                yield str(node_id), node
    elif isinstance(nodes, list):
        for index, node in enumerate(nodes):
            if isinstance(node, dict):
                yield str(node.get("id") or node.get("name") or index), node


def scan_playbooks() -> ScanResult:
    """経路 A: builtin の Playbook JSON に直接書かれた ``response_schema``。"""
    findings: List[Finding] = []
    problems: List[str] = []
    scanned = 0
    if not PLAYBOOK_DIR.is_dir():
        problems.append(f"Playbook のディレクトリが無い: {PLAYBOOK_DIR}")
        return ScanResult(findings, scanned, problems)
    for path in sorted(PLAYBOOK_DIR.rglob("*.json")):
        rel = path.relative_to(PLAYBOOK_DIR).as_posix()
        if rel.startswith("archive/"):
            continue  # 退役済み — ランタイムは読まない
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — 読めない JSON は穴として報告する
            problems.append(f"Playbook を読めなかった: {rel} ({exc})")
            continue
        for node_id, node in _playbook_nodes(data):
            schema = node.get("response_schema")
            if not isinstance(schema, dict):
                continue
            scanned += 1
            for field_path, field_type in numeric_fields(schema):
                findings.append(Finding(
                    route="playbook",
                    source=f"{rel}#{node_id}",
                    field_path=field_path,
                    field_type=field_type,
                    location=str(path.relative_to(PROJECT_ROOT).as_posix()),
                ))
    return ScanResult(findings, scanned, problems)


# ---------------------------------------------------------------------------
# 経路 C — スペルの引数の型 (spell_args_decider が response_schema に使う)
# ---------------------------------------------------------------------------


def _iter_builtin_tool_modules() -> Iterator[Tuple[str, Path]]:
    """builtin_data/tools/ のツールモジュールを ``(名前, ファイル)`` で返す。

    ``tools/__init__.py`` の autodiscovery と同じ拾い方 (``_`` 始まりは
    共通ヘルパ、サブディレクトリは ``schema.py``)。registry を直接 import
    しないのは、それが ``~/.saiverse/user_data/`` のツールまで実行してしまい、
    検査が各利用者の環境に依存する (かつ本番の持ち物に触る) ため。
    """
    if not BUILTIN_TOOLS_DIR.is_dir():
        return
    for path in sorted(BUILTIN_TOOLS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        yield path.stem, path
    for entry in sorted(BUILTIN_TOOLS_DIR.iterdir()):
        if entry.is_dir() and (entry / "schema.py").exists():
            yield entry.name, entry / "schema.py"


def _load_tool_module(name: str, path: Path) -> Any:
    module_name = f"_schema_scan_tools.{name}"
    spec = importlib.util.spec_from_file_location(
        module_name, path, submodule_search_locations=[str(path.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"モジュール仕様を作れなかった: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _tool_schemas(module: Any) -> List[Any]:
    if hasattr(module, "schemas") and callable(module.schemas):
        return list(module.schemas())
    if hasattr(module, "schema") and callable(module.schema):
        return [module.schema()]
    return []


def scan_spells() -> ScanResult:
    """経路 C: ``spell=True`` のツールの引数の型。

    ``spell=False`` のツールは Playbook の TOOL ノードから ``args_input`` で
    呼ばれるだけで、型が LLM に渡らないので対象外
    (裏取り: sea/runtime_llm.py の ``_resolve_response_schema_source`` は
    ``spell:`` 形式でしか型を作らず、関数呼び出し経路
    (``available_tools``) を使う現役 Playbook は 1 本も無い)。
    """
    findings: List[Finding] = []
    problems: List[str] = []
    scanned = 0
    for name, path in _iter_builtin_tool_modules():
        try:
            module = _load_tool_module(name, path)
        except Exception as exc:  # noqa: BLE001 — 読めないツールは穴として報告する
            problems.append(f"ツールを読み込めなかった: {name} ({exc})")
            continue
        try:
            schemas = _tool_schemas(module)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"ツールの schema() が失敗した: {name} ({exc})")
            continue
        for schema in schemas:
            if not getattr(schema, "spell", False):
                continue
            parameters = getattr(schema, "parameters", None)
            if not isinstance(parameters, dict):
                continue
            scanned += 1
            spell_name = str(getattr(schema, "name", name))
            for field_path, field_type in numeric_fields(parameters):
                findings.append(Finding(
                    route="spell",
                    source=spell_name,
                    field_path=field_path,
                    field_type=field_type,
                    location=str(path.relative_to(PROJECT_ROOT).as_posix()),
                ))
    return ScanResult(findings, scanned, problems)


# ---------------------------------------------------------------------------
# 経路 B — Python の中で組み立てる型 (ソースを構文解析して探す)
# ---------------------------------------------------------------------------


def _iter_first_party_python() -> Iterator[Tuple[Path, str]]:
    for path in PROJECT_ROOT.rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT)
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            continue
        if any(part in _SKIP_DIR_NAMES for part in parts):
            continue
        if parts[:2] == ("builtin_data", "tools"):
            continue  # 経路 C の担当
        yield path, rel.as_posix()


def _dict_entries(node: ast.Dict) -> Dict[str, ast.expr]:
    """dict literal の 「文字列キー → 値の AST」 を返す (非定数キーは無視)。"""
    entries: Dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            entries[key.value] = value
    return entries


def _is_schema_literal(node: ast.AST) -> bool:
    """この dict literal は JSON Schema の形をしているか。

    判定は ``properties`` を持つか ``"type": "object"`` を書いているか。
    このリポジトリで JSON Schema の形をした dict は、response_schema か
    ツール・フェノメノンの引数定義しか無く、いずれも LLM に向く面。
    """
    if not isinstance(node, ast.Dict):
        return False
    entries = _dict_entries(node)
    if "properties" in entries:
        return True
    declared = entries.get("type")
    return isinstance(declared, ast.Constant) and declared.value == "object"


def _walk_schema_ast(
    node: ast.expr, path: str = "$",
) -> Iterator[Tuple[str, Dict[str, ast.expr], ast.Dict]]:
    """AST の dict literal を :func:`walk_schema` と同じ順路で辿る。"""
    if not isinstance(node, ast.Dict):
        return
    entries = _dict_entries(node)
    yield path, entries, node
    props = entries.get("properties")
    if isinstance(props, ast.Dict):
        for key, child in _dict_entries(props).items():
            yield from _walk_schema_ast(child, f"{path}.{key}")
    items = entries.get("items")
    if isinstance(items, ast.Dict):
        yield from _walk_schema_ast(items, f"{path}[]")
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = entries.get(keyword)
        if isinstance(branches, ast.List):
            for index, child in enumerate(branches.elts):
                yield from _walk_schema_ast(child, f"{path}.{keyword}[{index}]")
    for keyword in ("$defs", "definitions"):
        defs = entries.get(keyword)
        if isinstance(defs, ast.Dict):
            for key, child in _dict_entries(defs).items():
                yield from _walk_schema_ast(child, f"{path}.{keyword}.{key}")
    extra = entries.get("additionalProperties")
    if isinstance(extra, ast.Dict):
        yield from _walk_schema_ast(extra, f"{path}.*")


def _numeric_type_of_ast(entries: Dict[str, ast.expr]) -> Optional[str]:
    declared = entries.get("type")
    if isinstance(declared, ast.Constant) and declared.value in NUMERIC_TYPES:
        return str(declared.value)
    if isinstance(declared, (ast.List, ast.Tuple)):
        for element in declared.elts:
            if isinstance(element, ast.Constant) and element.value in NUMERIC_TYPES:
                return str(element.value)
    return None


class SourceHit(NamedTuple):
    """Python ソースの中で見つかった数値欄 1 つ。"""

    qualname: str       # 型を書いている関数・クラス ("<module>" もある)
    field_path: str
    field_type: str
    lineno: int


def numeric_fields_in_python_source(source: str) -> Tuple[List[SourceHit], int]:
    """Python のソース文字列から数値欄を探し、``(見つけた欄, 見た型の数)`` を返す。

    関数を呼ばずにソースを読むので、``manager`` を偽装できない動的な組み立て
    (判断点のスキーマなど) も、新しく書かれた型も、そのまま対象に入る。実行時に
    注入されるのは enum の候補 (文字列) なので、数値欄の有無は literal を見れば
    分かる。
    """
    hits: List[SourceHit] = []
    roots = 0

    def visit(node: ast.AST, qualname: str, inside_schema: bool) -> None:
        nonlocal roots
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = f"{qualname}.{node.name}" if qualname else node.name
        is_root = not inside_schema and _is_schema_literal(node)
        if is_root and isinstance(node, ast.Dict):
            roots += 1
            for field_path, entries, dict_node in _walk_schema_ast(node):
                field_type = _numeric_type_of_ast(entries)
                if field_type is None:
                    continue
                hits.append(SourceHit(
                    qualname=qualname or "<module>",
                    field_path=field_path,
                    field_type=field_type,
                    lineno=dict_node.lineno,
                ))
        for child in ast.iter_child_nodes(node):
            visit(child, qualname, inside_schema or is_root)

    visit(ast.parse(source), "", False)
    return hits, roots


def scan_python() -> ScanResult:
    """経路 B: Python のソースに書かれた JSON Schema の形の dict literal。"""
    findings: List[Finding] = []
    problems: List[str] = []
    scanned = 0

    for path, rel in _iter_first_party_python():
        try:
            # utf-8-sig: BOM つきで保存されたファイルが数本ある。
            source = path.read_text(encoding="utf-8-sig")
            hits, roots = numeric_fields_in_python_source(source)
        except Exception as exc:  # noqa: BLE001 — 読めない = 見張りの穴
            problems.append(f"Python を構文解析できなかった: {rel} ({exc})")
            continue
        scanned += roots
        for hit in hits:
            findings.append(Finding(
                route="python",
                source=f"{rel}::{hit.qualname}",
                field_path=hit.field_path,
                field_type=hit.field_type,
                location=f"{rel}:{hit.lineno}",
            ))

    return ScanResult(findings, scanned, problems)


def scan_all() -> Dict[str, ScanResult]:
    """3 経路をまとめて走査する。"""
    return {
        "playbook": scan_playbooks(),
        "python": scan_python(),
        "spell": scan_spells(),
    }
