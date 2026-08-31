#!/usr/bin/env python3
"""Phase 3 段階 4-C: 既存 Playbook を line_role / scope ベースに半自動翻訳する。

詳細仕様: docs/intent/persona_cognition/line_tag_responsibility.md §段階 4-C
        docs/intent/persona_cognition/handoff_phase3_impl.md §段階 4-C

変換対象 (Y 案、2026-05-01 確定):

1) **LLM ノードから `context_profile` キー削除**
   旧仕様の値 (`conversation` / `worker_light` / `worker` / `router`) はすべて削除。
   4-A で `_prepare_context` を line ベースに切り替えた時点で LLM ノード単位の
   context_profile は無効化済み (記述として残ってるだけで効果なし)。

2) **`memorize.tags` の整理 (LLMNodeDef.memorize dict + MemorizeNodeDef 両方)**
   - `internal` タグ → `line_role: "sub_line"` + `scope: "volatile"` に置換
   - `conversation` タグ → `line_role: "main_line"` + `scope: "committed"` に置換
   - `event_message` タグ → 意味分類として **残置** + `line_role: "main_line"` + `scope: "committed"` を併記
     (Chronicle 連携で event_message の意味分類が必要なため、handoff §段階 4-C 参照)
   - `pulse:{uuid}` タグ → 残置 (Phase 2.5 で pulse_id カラム化済みだが書き込み併存中、4-D で廃止)
   - 残りの意味分類タグ (`creation`, `web_research`, `playbook_result` 等) → そのまま保持

3) **保留 (Y 案)**: `model_type` フィールド (`lightweight`)
   Y 案により、`/run_playbook` Spell 実装まで保留する。スクリプトは触らない。

usage:
    # dry-run (既定): 全 Playbook の変換差分を表示するだけ、書き戻しなし
    python scripts/migrate_playbooks_to_lines.py

    # apply: 実際に書き戻し
    python scripts/migrate_playbooks_to_lines.py --apply

    # 特定の Playbook のみ
    python scripts/migrate_playbooks_to_lines.py --filter autonomy_creation
    python scripts/migrate_playbooks_to_lines.py --filter autonomy_creation --apply

書き戻しは JSON のキー順序とインデントを保持しない (json.dump で再シリアライズ)。
git diff で意味のある差分のみ確認できるよう、書き戻し前後で json.dumps の結果が
同一であれば書き込みをスキップする (= --apply でも何も変わらない Playbook は touch しない)。
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOKS_DIR = REPO_ROOT / "builtin_data" / "playbooks"

# ---------------------------------------------------------------------------
# タグ → line_role / scope マッピング
# ---------------------------------------------------------------------------

# 純粋に context 制御用で、line_role/scope に置換するタグ
CONTEXT_CONTROL_TAGS_TO_LINE = {
    "internal": ("sub_line", "volatile"),
    "conversation": ("main_line", "committed"),
}

# context 制御 + 意味分類の両方の役割を持ち、両方残すタグ
HYBRID_TAGS_TO_LINE = {
    "event_message": ("main_line", "committed"),
}


def split_memorize_tags(
    tags: List[str],
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """memorize.tags を (line_role, scope, remaining_tags) に分解する。

    優先順位:
    - internal (sub_line/volatile) > conversation (main_line/committed) > event_message
      が混在する場合、より制約が強い方 (sub_line/volatile) を優先
    - event_message は意味分類タグとして残しつつ line メタデータも併記
    - "pulse:{uuid}" タグは残置 (4-D で廃止)
    """
    line_role: Optional[str] = None
    scope: Optional[str] = None
    remaining: List[str] = []

    # 優先順位: internal > conversation > event_message
    # internal が含まれていれば sub_line/volatile を採用 (より制約が強い)
    has_internal = "internal" in tags
    has_conversation = "conversation" in tags
    has_event_message = "event_message" in tags

    if has_internal:
        line_role, scope = CONTEXT_CONTROL_TAGS_TO_LINE["internal"]
    elif has_conversation:
        line_role, scope = CONTEXT_CONTROL_TAGS_TO_LINE["conversation"]
    elif has_event_message:
        line_role, scope = HYBRID_TAGS_TO_LINE["event_message"]

    for t in tags:
        if t in CONTEXT_CONTROL_TAGS_TO_LINE:
            # 純粋な context 制御タグは破棄
            continue
        # event_message は意味分類として残置
        # 意味分類タグ + pulse:{uuid} はそのまま保持
        remaining.append(t)

    return line_role, scope, remaining


# ---------------------------------------------------------------------------
# ノード変換
# ---------------------------------------------------------------------------


def migrate_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """1 ノードを変換して新しい dict を返す。元の dict は変更しない。"""
    new_node = deepcopy(node)
    node_type = new_node.get("type")

    # (1) LLM ノードの context_profile 削除
    if node_type == "llm":
        if "context_profile" in new_node:
            new_node.pop("context_profile")

    # (2) LLMNodeDef.memorize (dict) の tags 整理
    if node_type == "llm":
        memorize = new_node.get("memorize")
        if isinstance(memorize, dict):
            tags = memorize.get("tags") or []
            if isinstance(tags, list):
                line_role, scope, remaining = split_memorize_tags(tags)
                if line_role is not None and "line_role" not in memorize:
                    memorize["line_role"] = line_role
                if scope is not None and "scope" not in memorize:
                    memorize["scope"] = scope
                if remaining != list(tags):
                    if remaining:
                        memorize["tags"] = remaining
                    else:
                        memorize.pop("tags", None)

    # (3) MemorizeNodeDef (type=memorize) の tags 整理
    if node_type == "memorize":
        tags = new_node.get("tags") or []
        if isinstance(tags, list):
            line_role, scope, remaining = split_memorize_tags(tags)
            if line_role is not None and "line_role" not in new_node:
                new_node["line_role"] = line_role
            if scope is not None and "scope" not in new_node:
                new_node["scope"] = scope
            if remaining != list(tags):
                if remaining:
                    new_node["tags"] = remaining
                else:
                    new_node.pop("tags", None)

    return new_node


def migrate_playbook(data: Dict[str, Any]) -> Dict[str, Any]:
    """Playbook 全体を変換して新しい dict を返す。元の dict は変更しない。"""
    new_data = deepcopy(data)
    new_nodes: List[Any] = []
    for node in new_data.get("nodes", []):
        if isinstance(node, dict):
            new_nodes.append(migrate_node(node))
        else:
            new_nodes.append(node)
    new_data["nodes"] = new_nodes
    return new_data


# ---------------------------------------------------------------------------
# Diff レンダリング
# ---------------------------------------------------------------------------


def render_diff(name: str, before: Dict[str, Any], after: Dict[str, Any]) -> str:
    """変換前後の差分を unified diff 形式で返す。"""
    import difflib

    before_text = json.dumps(before, indent=2, ensure_ascii=False, sort_keys=False)
    after_text = json.dumps(after, indent=2, ensure_ascii=False, sort_keys=False)
    if before_text == after_text:
        return ""
    diff_lines = list(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            n=3,
        )
    )
    return "".join(diff_lines)


# ---------------------------------------------------------------------------
# レビュー補助情報の集計
# ---------------------------------------------------------------------------


def summarize_changes(name: str, before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, int]:
    """1 Playbook の変更を要約 (diff レビュー用)。"""
    counts = {
        "context_profile_removed": 0,
        "memorize_tags_internal_to_subline": 0,
        "memorize_tags_conversation_to_mainline": 0,
        "memorize_tags_event_message_kept": 0,
        "memorize_node_internal_to_subline": 0,
        "memorize_node_conversation_to_mainline": 0,
        "memorize_node_event_message_kept": 0,
    }
    before_nodes = {n.get("id"): n for n in before.get("nodes", []) if isinstance(n, dict)}
    after_nodes = {n.get("id"): n for n in after.get("nodes", []) if isinstance(n, dict)}

    for nid, b in before_nodes.items():
        a = after_nodes.get(nid)
        if a is None:
            continue
        if b.get("type") == "llm":
            if "context_profile" in b and "context_profile" not in a:
                counts["context_profile_removed"] += 1
            b_mem = b.get("memorize") if isinstance(b.get("memorize"), dict) else None
            a_mem = a.get("memorize") if isinstance(a.get("memorize"), dict) else None
            if b_mem and a_mem:
                b_tags = set(b_mem.get("tags") or [])
                if "internal" in b_tags and a_mem.get("line_role") == "sub_line":
                    counts["memorize_tags_internal_to_subline"] += 1
                elif "conversation" in b_tags and a_mem.get("line_role") == "main_line":
                    counts["memorize_tags_conversation_to_mainline"] += 1
                if "event_message" in b_tags:
                    counts["memorize_tags_event_message_kept"] += 1
        elif b.get("type") == "memorize":
            b_tags = set(b.get("tags") or [])
            if "internal" in b_tags and a.get("line_role") == "sub_line":
                counts["memorize_node_internal_to_subline"] += 1
            elif "conversation" in b_tags and a.get("line_role") == "main_line":
                counts["memorize_node_conversation_to_mainline"] += 1
            if "event_message" in b_tags:
                counts["memorize_node_event_message_kept"] += 1
    return counts


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def find_playbook_files(filter_name: Optional[str]) -> List[Path]:
    files = sorted(PLAYBOOKS_DIR.rglob("*.json"))
    files = [f for f in files if "archive" not in str(f).lower()]
    if filter_name:
        files = [f for f in files if filter_name in f.stem]
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="実際に書き戻す。指定なしは dry-run")
    parser.add_argument("--filter", type=str, default=None, help="Playbook 名 (部分一致) でフィルタ")
    parser.add_argument(
        "--no-diff", action="store_true",
        help="diff 表示を抑制 (要約のみ表示)。長い diff を避けたい時に。",
    )
    args = parser.parse_args()

    files = find_playbook_files(args.filter)
    if not files:
        print(f"No playbooks found under {PLAYBOOKS_DIR}", file=sys.stderr)
        return 1

    total_changed = 0
    total_unchanged = 0
    aggregate_counts: Dict[str, int] = {}

    for f in files:
        try:
            before = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[SKIP] {f}: failed to load JSON: {exc}", file=sys.stderr)
            continue
        after = migrate_playbook(before)
        diff = render_diff(f.name, before, after)
        if not diff:
            total_unchanged += 1
            continue
        total_changed += 1
        name = before.get("name", f.stem)
        counts = summarize_changes(name, before, after)
        for k, v in counts.items():
            aggregate_counts[k] = aggregate_counts.get(k, 0) + v

        print(f"=== {name} ({f.relative_to(REPO_ROOT)}) ===")
        change_summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v > 0)
        print(f"  {change_summary}")
        if not args.no_diff:
            print(diff)
            print()

        if args.apply:
            new_text = json.dumps(after, indent=2, ensure_ascii=False)
            f.write_text(new_text + "\n", encoding="utf-8")

    print()
    print("=" * 60)
    print(f"Changed Playbooks    : {total_changed}")
    print(f"Unchanged Playbooks  : {total_unchanged}")
    print("Aggregate counts:")
    for k, v in sorted(aggregate_counts.items()):
        print(f"  {k}: {v}")
    if args.apply:
        print()
        print("[DONE] Applied changes. Run `python scripts/import_all_playbooks.py --force` to reflect in DB.")
    else:
        print()
        print("[DRY-RUN] No files changed. Use --apply to write changes back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
