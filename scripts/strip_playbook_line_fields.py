#!/usr/bin/env python3
"""認知モデル v0.2 (§10.6): Playbook ノードから line_role / scope / model_type を剥がす。

v0.1 (段階 4-C) では各ノードの memorize に line_role / scope を明示していたが、
v0.2 ではこれらは「呼び出し時のアスペクト (Aspect)」から構造的に導出される
(line_tag_responsibility.md §10)。よって Playbook JSON からは削除し、ノードには
意味タグ (tags) と memorize の有無のみを残す。

対象:
- ``type=llm`` ノードの ``memorize`` (dict) から ``line_role`` / ``scope`` を削除。
  削除後に dict が空になったら ``memorize: true`` (既定タグで保存) に置換。
- ``type=llm`` ノードの ``model_type`` を削除 (アスペクトが tier を決めるため、
  ノード指定は既に no-op)。
- ``type=memorize`` ノードから ``line_role`` / ``scope`` を削除。

usage:
    python scripts/strip_playbook_line_fields.py            # dry-run
    python scripts/strip_playbook_line_fields.py --apply    # 書き戻し
    python scripts/strip_playbook_line_fields.py --filter schedule_management --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOKS_DIR = REPO_ROOT / "builtin_data" / "playbooks"

_LINE_FIELDS = ("line_role", "scope")


def strip_node(node: Dict[str, Any]) -> List[str]:
    """1 ノードを in-place で剥がす。削除したフィールド名のリストを返す。"""
    removed: List[str] = []
    node_type = node.get("type")

    if node_type == "llm":
        # model_type は削除 (アスペクトが tier を決める)
        if "model_type" in node:
            node.pop("model_type")
            removed.append("model_type")
        memorize = node.get("memorize")
        if isinstance(memorize, dict):
            for f in _LINE_FIELDS:
                if f in memorize:
                    memorize.pop(f)
                    removed.append(f"memorize.{f}")
            # line_role/scope 以外が何も残らなければ memorize: true に置換
            # (空 dict は Python で falsy になり memorize 無効化されてしまうため)
            if not memorize:
                node["memorize"] = True
                removed.append("memorize→true")

    elif node_type == "memorize":
        for f in _LINE_FIELDS:
            if f in node:
                node.pop(f)
                removed.append(f)

    return removed


def strip_playbook(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Playbook 全体を剥がす。(新 data, 変更サマリ) を返す。"""
    changes: List[str] = []
    for node in data.get("nodes", []):
        if isinstance(node, dict):
            removed = strip_node(node)
            if removed:
                changes.append(f"{node.get('id')}: {', '.join(removed)}")
    return data, changes


def find_files(filter_name: Optional[str]) -> List[Path]:
    files = sorted(PLAYBOOKS_DIR.rglob("*.json"))
    files = [f for f in files if "archive" not in str(f).lower()]
    if filter_name:
        files = [f for f in files if filter_name in f.stem]
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="実際に書き戻す (既定は dry-run)")
    parser.add_argument("--filter", type=str, default=None, help="Playbook 名 (部分一致) でフィルタ")
    args = parser.parse_args()

    files = find_files(args.filter)
    if not files:
        print(f"No playbooks found under {PLAYBOOKS_DIR}", file=sys.stderr)
        return 1

    total_changed = 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[SKIP] {f}: {exc}", file=sys.stderr)
            continue
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        data, changes = strip_playbook(data)
        after = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if before == after:
            continue
        total_changed += 1
        print(f"=== {data.get('name', f.stem)} ({f.relative_to(REPO_ROOT)}) ===")
        for c in changes:
            print(f"  {c}")
        if args.apply:
            f.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print()
    print("=" * 60)
    print(f"Changed playbooks: {total_changed}")
    if args.apply:
        print("[DONE] 書き戻し完了。`python scripts/import_all_playbooks.py --force` で DB 反映。")
    else:
        print("[DRY-RUN] --apply で書き戻し。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
