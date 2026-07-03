#!/usr/bin/env python
"""Generate the "reference" docs that mirror concrete code artifacts.

これらのドキュメントは **コードから自動生成** される。手で編集しても次回生成で
上書きされる。内容がずれたら、対象のコード側 (下記) を直してから再生成すること。

生成対象と源:
    docs/reference/tool-catalog.md     ← tools.TOOL_SCHEMAS (builtin_data/tools/*.py)
    docs/reference/api-endpoints.md    ← api.main.api_router (api/routes/*.py)
    docs/reference/database-schema.md  ← database.models.Base.metadata

使い方:
    python scripts/gen_reference_docs.py            # 生成 (上書き)
    python scripts/gen_reference_docs.py --check    # 生成せず、現状とのズレを検査 (CI 用)

大量・高頻度変化の列挙物 (ツール ~90 / モデル ~120 / エンドポイント 100+) は手書き
markdown だと必ず腐るため、この生成方式に寄せている。小さく安定した一覧 (プロバイダ /
Phenomena / Playbook / saiverse:// URI 等) は docs/reference/ に手書きで置く。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
from pathlib import Path

# リポジトリルートを import path に載せる (scripts/ の1つ上)
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REFERENCE_DIR = REPO_ROOT / "docs" / "reference"

# ツールロード等の警告ノイズを抑える (生成には無関係)
logging.disable(logging.WARNING)


def _banner(source: str) -> str:
    """各生成ファイル冒頭の「自動生成・手編集禁止」バナー。"""
    return (
        "<!-- 🤖 AUTO-GENERATED — 手で編集しない。次回生成で上書きされる。 -->\n"
        f"<!-- 源: {source} / 再生成: python scripts/gen_reference_docs.py -->\n"
    )


# ---------------------------------------------------------------------------
# tool-catalog.md
# ---------------------------------------------------------------------------

def _render_tool_params(parameters: dict) -> str:
    """JSON Schema の parameters を 1 行の要約に整形する。"""
    if not isinstance(parameters, dict):
        return ""
    props = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])
    if not props:
        return "(なし)"
    parts = []
    for pname, spec in props.items():
        ptype = spec.get("type", "any") if isinstance(spec, dict) else "any"
        mark = "*" if pname in required else ""
        parts.append(f"`{pname}{mark}`: {ptype}")
    return ", ".join(parts)


def generate_tool_catalog() -> str:
    import tools  # noqa: import 時に _autodiscover_tools() が走り TOOL_SCHEMAS が埋まる

    schemas = sorted(tools.TOOL_SCHEMAS, key=lambda s: (getattr(s, "addon_name", None) or "", s.name))
    total = len(schemas)
    spell_count = sum(1 for s in schemas if getattr(s, "spell", False))

    lines = [
        _banner("tools.TOOL_SCHEMAS (builtin_data/tools/*.py)"),
        "# ツールカタログ",
        "",
        "SAIVerse に登録されている全ツールの一覧（自動生成）。概念は [concepts/tool.md](../concepts/tool.md)、",
        "作り方は [開発者ガイド: ツールの追加](../developer-guide/adding-tools.md)、",
        "平文から呼ぶ Spell 化は [concepts/spell.md](../concepts/spell.md) を参照。",
        "",
        f"**登録ツール数**: {total}（うち Spell 化: {spell_count}）",
        "",
        "- `*` 付きの引数は必須。",
        "- **Spell** 列に表示名があるものは、ペルソナが平文応答から `/spell <名> ...` で呼べる。",
        "",
        "| ツール名 | 説明 | 引数 | Spell |",
        "|---|---|---|---|",
    ]
    for s in schemas:
        desc = (s.description or "").replace("\n", " ").replace("|", "\\|").strip()
        if len(desc) > 120:
            desc = desc[:117] + "…"
        params = _render_tool_params(getattr(s, "parameters", {})).replace("|", "\\|")
        if getattr(s, "spell", False):
            spell = getattr(s, "spell_display_name", "") or "✓"
            if not getattr(s, "spell_visible", True):
                spell += "（非表示）"
        else:
            spell = "—"
        lines.append(f"| `{s.name}` | {desc} | {params} | {spell} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# api-endpoints.md
# ---------------------------------------------------------------------------

def generate_api_endpoints() -> str:
    from api.main import api_router
    from starlette.routing import Route

    # tag ごとにルートをまとめる
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for route in api_router.routes:
        if not isinstance(route, Route):
            continue
        path = "/api" + route.path
        methods = sorted(m for m in (route.methods or set()) if m not in {"HEAD", "OPTIONS"})
        method_str = ", ".join(methods) or "—"
        tags = getattr(route, "tags", None) or ["(untagged)"]
        tag = str(tags[0])
        doc = (route.endpoint.__doc__ or "").strip().split("\n")[0] if route.endpoint else ""
        doc = doc.replace("|", "\\|")
        grouped.setdefault(tag, []).append((method_str, path, doc))

    total = sum(len(v) for v in grouped.values())
    lines = [
        _banner("api.main.api_router (api/routes/*.py)"),
        "# API エンドポイント",
        "",
        "REST API 全エンドポイントの一覧（自動生成）。すべて `/api` 配下にマウントされる。",
        "",
        f"**エンドポイント数**: {total}（tag グループ: {len(grouped)}）",
        "",
    ]
    for tag in sorted(grouped):
        rows = sorted(grouped[tag], key=lambda r: r[1])
        lines.append(f"## {tag}")
        lines.append("")
        lines.append("| メソッド | パス | 説明 |")
        lines.append("|---|---|---|")
        for method_str, path, doc in rows:
            lines.append(f"| {method_str} | `{path}` | {doc} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# database-schema.md
# ---------------------------------------------------------------------------

def _col_constraints(col) -> str:
    marks = []
    if col.primary_key:
        marks.append("PK")
    for fk in col.foreign_keys:
        marks.append(f"FK→{fk.target_fullname}")
    if not col.nullable:
        marks.append("NOT NULL")
    if col.default is not None and getattr(col.default, "arg", None) is not None and not callable(col.default.arg):
        marks.append(f"default={col.default.arg!r}")
    return ", ".join(marks) or "—"


def generate_database_schema() -> str:
    from database.models import Base

    tables = Base.metadata.sorted_tables
    lines = [
        _banner("database.models.Base.metadata"),
        "# データベーススキーマ",
        "",
        "SAIVerse の全テーブル・カラム定義（自動生成）。SQLite。本番 DB は",
        "`~/.saiverse/user_data/database/saiverse.db`。概念的な位置づけは",
        "[concepts/](../concepts/README.md) 各ページを参照。",
        "",
        f"**テーブル数**: {len(tables)}",
        "",
    ]
    for table in tables:
        comment = f" — {table.comment}" if table.comment else ""
        lines.append(f"## {table.name}{comment}")
        lines.append("")
        lines.append("| カラム | 型 | 制約 | 説明 |")
        lines.append("|---|---|---|---|")
        for col in table.columns:
            ctype = str(col.type).replace("|", "\\|")
            constraints = _col_constraints(col).replace("|", "\\|")
            doc = (col.comment or col.doc or "").replace("\n", " ").replace("|", "\\|")
            lines.append(f"| `{col.name}` | {ctype} | {constraints} | {doc} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

_TARGETS = {
    "tool-catalog.md": generate_tool_catalog,
    "api-endpoints.md": generate_api_endpoints,
    "database-schema.md": generate_database_schema,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="生成せずズレを検査 (差分あれば exit 1)")
    args = parser.parse_args()

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    drift = False
    for filename, fn in _TARGETS.items():
        target = REFERENCE_DIR / filename
        try:
            content = fn().rstrip("\n") + "\n"
        except Exception as exc:  # 1 つが失敗しても他は生成する
            print(f"[FAIL] {filename}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            drift = True
            continue

        if args.check:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != content:
                drift = True
                print(f"[DRIFT] {filename} は最新のコードとズレている（要再生成）")
            else:
                print(f"[ok] {filename}")
        else:
            target.write_text(content, encoding="utf-8")
            n = content.count("\n")
            print(f"[gen] {filename} ({n} 行)")

    if args.check and drift:
        print("\n再生成: python scripts/gen_reference_docs.py", file=sys.stderr)
        return 1
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
