"""ファジー文字列マッチングヘルパー。

ペルソナが正確な識別子を覚えていない場合に最も近い候補にスナップするための
汎用ユーティリティ。

## 設計方針

- **Opt-in**: スペル/ツール側で明示的に呼び出す形（呼び出し層で自動適用しない）
- **不可逆操作には使わない**: 投稿削除・ファイル削除・設定変更など、意図しない
  値にスナップすると害が大きい操作では絶対に使用しない
- **情報取得系で使用**: ``addon_spell_help`` や lookup/search 系のように、
  ファジー結果が間違っていてもペルソナが軌道修正できる文脈でのみ使う

## 使用例

```python
from tools.fuzzy import resolve_fuzzy

resolved, was_exact, original = resolve_fuzzy(
    user_input, candidates=["foo", "bar", "baz"], threshold=0.5,
)
if not was_exact and original is not None:
    response += f"（'{original}' を '{resolved}' として解釈しました）"
```
"""
from __future__ import annotations

import difflib
import logging
from typing import Iterable, Optional, Tuple

LOGGER = logging.getLogger(__name__)


def find_closest(
    value: str,
    candidates: Iterable[str],
    *,
    threshold: float = 0.5,
) -> Optional[str]:
    """``value`` に最も近い候補を返す。閾値未満なら ``None``。

    Args:
        value: 入力値
        candidates: 候補一覧
        threshold: ``difflib.SequenceMatcher.ratio()`` に対する最小閾値 (0.0-1.0)

    Returns:
        最も近い候補、または該当なしで ``None``
    """
    candidates_list = [c for c in candidates if c]
    if not candidates_list or not value:
        return None

    matches = difflib.get_close_matches(
        value, candidates_list, n=1, cutoff=threshold
    )
    if matches:
        return matches[0]
    return None


def resolve_fuzzy(
    value: str,
    candidates: Iterable[str],
    *,
    threshold: float = 0.5,
) -> Tuple[str, bool, Optional[str]]:
    """``value`` を候補一覧に対して解決する。

    Args:
        value: 入力値
        candidates: 候補一覧
        threshold: ファジーマッチの最小閾値

    Returns:
        ``(resolved_value, was_exact_match, original_if_fuzzy)`` のタプル

        - ``resolved_value``: 完全一致なら ``value`` そのまま、ファジー一致
          したら最近傍、どちらでもなければ ``value`` そのまま
        - ``was_exact_match``: 完全一致だったか
        - ``original_if_fuzzy``: ファジー一致した場合の元の入力値
          （ペルソナへのフィードバック用）。完全一致 or 未一致なら ``None``
    """
    candidates_list = [c for c in candidates if c]
    if not value:
        return value, False, None

    if value in candidates_list:
        return value, True, None

    closest = find_closest(value, candidates_list, threshold=threshold)
    if closest is not None:
        LOGGER.info(
            "fuzzy.resolve: %r -> %r (candidates=%d, threshold=%s)",
            value, closest, len(candidates_list), threshold,
        )
        return closest, False, value

    LOGGER.info(
        "fuzzy.resolve: %r had no match above threshold=%s (candidates=%d)",
        value, threshold, len(candidates_list),
    )
    return value, False, None
