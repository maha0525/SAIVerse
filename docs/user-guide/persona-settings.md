# ペルソナ設定

ペルソナ1体ごとの設定。ペルソナメニュー →「設定」から開く（`SettingsModal.tsx`）。

## 基本

- **名前**
- システムプロンプト（人格・背景）※ペルソナ作成ウィザードでも設定

## モデル

用途ごとに使うモデルを指定できる（任意のものは未設定ならフォールバック）:

| 設定 | 用途 |
|---|---|
| **デフォルトモデル** | 通常の会話 |
| **軽量モデル**（任意） | サブライン・自律判断など軽い処理（→ [concepts/line.md](../concepts/line.md)） |
| **Memory Weave モデル**（任意） | Chronicle / Memopedia 生成 |
| **画像 / 音声 / 動画 要約モデル**（任意） | 各メディアの要約 |

## 自律行動

### アクティビティ状態（`ACTIVITY_STATE`）

| 状態 | 意味 |
|---|---|
| 🟢 **Active** | 活発に自律稼働 |
| 🟡 **Idle** | 起きているが自発的には行動しない |
| 🔵 **Sleep** | 寝ている（ユーザー発言で起きる） |
| ⚫ **Stop** | 機能停止 |

### その他の自律まわり

- **自律行動マネージャー** — 自律行動の有効/間隔（→ [自律行動モード](../features/autonomous-mode.md)）
- **メタ判断 Pulse 設定** — メタ判断の間隔など
- **応答待ち Track 自動 pause 閾値** — 応答待ちの Track を自動で pause する閾値

## 記憶

- **Chronicle 自動生成** — Metabolism（記憶整理）時に Chronicle（あらすじ）を自動生成する（LLM API コストが発生）
- **Memory Weave コンテキスト** — Chronicle + Memopedia をコンテキストに含めるか

## 関連

- [ワールドビュー](world-view.md) - ペルソナメニューの開き方
- [features/autonomous-mode.md](../features/autonomous-mode.md) - 自律行動と ACTIVITY_STATE
- [concepts/saimemory.md](../concepts/saimemory.md) - 記憶の仕組み
