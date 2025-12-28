# 自律行動モード

ペルソナの自律行動（パルス駆動）について説明します。

## 概要

自律行動モードでは、ペルソナが定期的に自分から思考・発言を行います。ユーザーからの入力がなくても、AIが能動的に活動します。

## パルス駆動

### 仕組み

`ConversationManager` が定期的にペルソナの `run_pulse` を呼び出します。

```
[10秒経過] → ConversationManager → PersonaCore.run_pulse()
                                          │
                                          ▼
                                    認知 → 判断 → 行動
                                          │
                                          ▼
                                    発話 or 待機
```

### パルス間隔

Building ごとに設定可能（デフォルト: 10秒）。

```python
# Building の AUTO_PULSE_INTERVAL カラムで設定
# 例: 30秒間隔
building.AUTO_PULSE_INTERVAL = 30
```

## 行動モード

### INTERACTION_MODE

| モード | 説明 | 自律発話 |
|--------|------|:--------:|
| `auto` | 自律会話モード | ✓ |
| `user` | ユーザー対話モード | ✗ |
| `sleep` | 休眠モード | ✗ |

### モードの切り替え

- **召喚時**: `auto` → `user` に自動切替
- **帰還時**: `user` → 元のモードに復帰
- **ワールドエディタ**: 手動で `sleep` に設定可能

## 思考フロー

パルス実行時のPlaybook例（`meta_auto`）：

```
start → 状況認知 → 行動判断 → speak/think/wait
```

### 出力オプション

| 行動 | 説明 |
|------|------|
| `speak` | Building内で発言 |
| `think` | 内部で思考（発話なし） |
| `wait` | 何もしない |

## グローバル制御

### 自律モードスイッチ

サイドバーから全体のON/OFFを切り替え可能。

- **ON**: 全ペルソナが設定に従い自律行動
- **OFF**: 全ての自律行動を停止

## 次のステップ

- [Playbook/SEA](./playbooks.md) - 行動パターンの定義
- [ペルソナ](../concepts/persona.md) - AIの仕組み
