"""モード (aspect) 別スペル権限のゲート。

``docs/intent/persona_cognition/mode_spell_permissions.md`` (確定 v1.0) の実装。

権限マトリクス (§5):

| モード (aspect)        | Task 操作 |
|------------------------|-----------|
| メインモード (CONVERSATION) | ✅        |
| 自律制御モード (META)       | ❌        |
| 自律作業モード (AUTONOMOUS) | ✅        |
| 分身モード (WORKER)         | ❌        |

Track 操作の列は 2026-08-21 に消えた — ``track_create`` 以下 7 種のスペルが
機構ごと退役したため (track_retirement.md §7.2 ④群)。Task 操作の列も
2026-08-23 に中身を失った — 目的の木が手帳に後を譲って退役し、``purpose_*``
三種が削除されたため (purpose_tree_vs_pocketbook_succession.md の裁定 A)。

- 読み取り系 (``get_task_summary`` 等) と汎用スペル
  (recall / note / memopedia / image / web 等) は全モード無制限 (= ここに載せない)。
- 生産手段の ``document_*`` スペル (create / read / edit / search) も汎用スペル
  扱いで全モード無制限。特に分身モード (WORKER) の
  作業セッションがアーティファクト生成に使う前提なので、ここに載せて
  制限しないこと (autonomous_behavior_v2.md §2.2 / §11)。
- 旧 ``task_*`` 系は廃止予定でゲート対象外。
- 判定は ``aspect`` をキーに行い、ゲット文の表示のみ ``mode_display_name`` を使う。
"""
from __future__ import annotations

from typing import Optional

from sea.pulse_context import Aspect

# Task 操作スペル (mutating)。タスク一本化 (unified_task_model.md) 後の統合スペル群。
# 2026-08-21: 欲求プールの退役 (autonomous_behavior_v3.md §8) で purpose_seed /
# purpose_adopt がスペルごと消えたため、ゲート対象から外した。
# 2026-08-23: 目的の木そのものが手帳に後を譲って退役し、残っていた
# purpose_decompose / purpose_step / purpose_close の三本も削除された
# (docs/issues/purpose_tree_vs_pocketbook_succession.md の裁定 A、一段目)。
# その結果このゲート表は空になっている — 表が空でも判定関数は素通しなので
# 動作は変わらない。機構ごと畳むかどうかは v0.4 の運転設計で問う。
TASK_CONTROL_SPELLS: frozenset = frozenset()

# 旧「自己定義スペル」カテゴリ (life_purpose_set 1 件) は、LIFE_PURPOSE 列の退役
# (autonomous_behavior_v3.md §9-5) でスペルごと消えたためゲート対象から外した。
# 旧「Track 操作」カテゴリ (track_create ほか 6 件) も同様に、Track 操作スペルの
# 退役 (track_retirement.md §7.2 ④群) でゲート対象から外した。

# カテゴリを使える aspect (§4 能力レイヤーから導出 / §5 マトリクス)。
_TASK_ALLOWED_ASPECTS = frozenset({Aspect.CONVERSATION, Aspect.AUTONOMOUS})


def check_spell_permission(spell_name: str, aspect: Optional[Aspect]) -> Optional[str]:
    """スペルが現在の ``aspect`` で実行可能か判定する。

    Args:
        spell_name: 実行しようとしているスペル名。
        aspect: アクティブなラインのアスペクト。``None`` (legacy frame /
            アスペクト不明) のときは制限しない。

    Returns:
        ``None``: 許可 (ゲート対象外のスペル、または許可された aspect)。
        ``str``: 不許可。ペルソナに返すゲット文 (§6.3)。
    """
    if aspect is None:
        return None
    if spell_name in TASK_CONTROL_SPELLS and aspect not in _TASK_ALLOWED_ASPECTS:
        return _build_block_message(spell_name, aspect)
    return None


def _build_block_message(spell_name: str, aspect: Aspect) -> str:
    """ゲット文 (§6.3 確定文面)。"""
    return (
        f"{spell_name}スペルは現在のモード（{aspect.mode_display_name}）では実行できません。"
    )
