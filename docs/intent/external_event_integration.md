# Intent Document: 外部イベント統合 (External Event Integration)

## 目的

ペルソナが「ユーザー発話」「自律パルス」以外の第3のトリガーで行動できるようにする。
外部サービス（X、SwitchBot等）からの情報をペルソナに届け、反応させる仕組み。

## 背景

### 現状の行動トリガー（2種類）

1. **ユーザー発話** → `meta_user` playbook
2. **自律パルス**（定時/間隔） → `meta_auto` playbook / `ScheduleManager`

### 必要になった第3のトリガー

- Xメンション受信 → ペルソナがリプライ
- SwitchBot開閉センサー → 帰宅検知でペルソナが「おかえり」
- メール着信 → ペルソナが内容を確認・報告

共通点: **外部の状態変化がペルソナの行動を引き起こす**

## 設計方針

### 既存フェノメノンシステムとの統合

新フレームワークを作らず、既存の `PhenomenonManager` に外部イベントトリガーを追加する。

**理由**: フェノメノンシステムには既に以下が揃っている:
- `TriggerType` — イベント種別の定義
- `TriggerEvent` — イベントデータ (type + data dict)
- `PhenomenonRule` — DB上の条件ルール (TRIGGER_TYPE + CONDITION_JSON → PHENOMENON_NAME + ARGUMENT_MAPPING_JSON)
- `PhenomenonManager.emit()` — イベント受信 → 条件マッチ → アクション実行
- `_matches_condition()` — JSON条件による柔軟なフィルタリング
- `_resolve_arguments()` — `$trigger.field_name` 変数解決

### アーキテクチャ

```
IntegrationManager (ポーリングループ)
  │
  ├── X Integration: メンション/タイムラインをポーリング
  ├── SwitchBot Integration: デバイス状態をポーリング
  └── (将来の連携)
  │
  ↓ 状態変化検出
  │
PhenomenonManager.emit(TriggerEvent(type="x_mention_received", data={...}))
  │
  ↓ PhenomenonRule で条件マッチ
  │  例: {"author_username": "maha0525"} → 特定ユーザーのみ
  │
  ↓ フェノメノン実行
  │
persona_event_log に書き込み → PulseController → ペルソナが行動
```

### コンポーネント別の役割

| コンポーネント | 役割 | 既存/新規 |
|---|---|---|
| `IntegrationManager` | 外部APIポーリング、状態変化検出、TriggerEvent発行 | **新規** |
| `PhenomenonManager` | イベント受信、ルールマッチング、フェノメノン発火 | 既存（拡張） |
| `PhenomenonRule` (DB) | イベント→アクションのマッピングルール | 既存（ルール追加） |
| `TriggerType` | 外部イベント種別の追加 | 既存（enum追加） |
| `persona_event_log` (DB) | ペルソナ向けイベントキュー | 既存（`event_type`/`payload`拡張） |
| `PulseController` | ペルソナへのプロンプト注入 | 既存 |

## 詳細設計

### 1. IntegrationManager（新規）

`ScheduleManager` と同格の独立マネージャー。独自スレッドでポーリングループを回す。

- 登録されたインテグレーションごとにポーリング間隔を管理
- 状態変化を検出したら `PhenomenonManager.emit()` にイベントを送る
- 各インテグレーションはポーリング間隔が異なる（Xメンション: 5分、SwitchBot: 30秒 等）
  - 共通ループの1ティックごとにカウンターで間隔管理

### 2. TriggerType 追加

```python
# phenomena/triggers.py に追加
X_MENTION_RECEIVED = "x_mention_received"
X_TIMELINE_UPDATE = "x_timeline_update"
SWITCHBOT_STATE_CHANGED = "switchbot_state_changed"
EXTERNAL_WEBHOOK = "external_webhook"  # 汎用Webhook受信
```

### 3. persona_event_log テーブル拡張

現状は `CONTENT` (文字列) のみ。以下を追加:
- `EVENT_TYPE` — イベント種別（"x_mention", "switchbot_open" 等）
- `PAYLOAD` — JSON形式の構造化データ

### 4. フェノメノンの出口拡張

現状の `PHENOMENON_REGISTRY` は関数を直接実行するだけ。
ペルソナにプロンプトを注入する汎用フェノメノン（例: `inject_persona_prompt`）を追加し、
`PulseController.submit_schedule()` 相当の処理を行う。

### 5. イベントフィルタリングの柔軟性

`PhenomenonRule.CONDITION_JSON` で対応:

```json
// 特定ユーザーからのメンションのみ
{"author_username": "maha0525"}

// SwitchBotの特定デバイスのみ
{"device_type": "contact_sensor", "device_name": "玄関"}

// 条件なし（全イベントにマッチ）
null
```

ユーザーはDB/UI経由でルールを追加・編集できる。プリセットルールも提供。

## イベント取得方式

### ポーリング型（基本）

| 連携 | 理由 | 推奨間隔 |
|---|---|---|
| Xメンション | 基本プランにWebhookなし | 5分 |
| SwitchBot | Webhook受信にHTTPS必須、ローカル環境では非現実的 | 30秒 |

### プッシュ型（将来、環境が整えば）

- SwitchBot Webhook: HTTPS公開URL必須（ngrok/VPS運用時）
- 受け口: `POST /api/events/incoming` → TriggerEvent変換 → PhenomenonManager.emit()

**ポーリング型が基本。** プッシュ型は環境が整ったときにオプションとして追加。

## X リプライに関する規約上の制約

### X API 自動リプライルール
- 受信者が**事前にコンタクトを求めている**か**明確に意図を示している**必要がある
- キーワード検索ベースの自動リプライは**明確に禁止**
- 一度のユーザーインタラクションにつき**自動リプライは1回のみ**
- AIリプライボットの運用には**Xからの事前書面承認**が必要

### 安全策: x_reply_log テーブル

```sql
CREATE TABLE x_reply_log (
    id INTEGER PRIMARY KEY,
    tweet_id TEXT UNIQUE,       -- 二重リプ防止（UNIQUE制約）
    persona_id TEXT NOT NULL,
    reply_tweet_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

- `tweet_id` の UNIQUE 制約でDB層で物理的に二重リプライを防止
- ペルソナの SAIMemory とは独立（メモリリセットの影響を受けない）
- サーバー共通（複数ペルソナが同一アカウント共有時も安全）

## ScheduleManager との関係

**統合しない。** ランナーは別、出口は共通。

- `ScheduleManager` — 時刻ベースのトリガー（「毎朝9時」「30分ごと」）、既に安定稼働
- `IntegrationManager` — 状態変化ベースのトリガー（「新メンション到着」「ドアが開いた」）
- 両方とも最終的に `PulseController` 経由でペルソナにプロンプトを渡す

## 不変条件

1. **二重反応の防止**: 同一の外部イベント（同じtweet_id、同じセンサーイベント）に複数回反応してはならない
2. **ペルソナの人格一貫性**: 外部イベントへの反応時も、通常の会話と同じメモリ状態でplaybookを実行する（X連携の不変条件1を継承）
3. **ユーザー制御可能性**: どのイベントにどう反応するかは、PhenomenonRuleを通じてユーザーが設定・変更できる
4. **安全なデフォルト**: プリセットルールは保守的に設定し、ユーザーが明示的に有効化する形にする
5. **外部APIレート制限の遵守**: ポーリング間隔は各APIのレートリミットを超えないよう設定

## 実装優先度

1. `persona_event_log` テーブル拡張（event_type, payload追加）
2. `TriggerType` に外部イベント種別追加
3. `IntegrationManager` 基盤（ポーリングループ、インテグレーション登録）
4. Xメンションポーリング（最初の具体的インテグレーション）
5. `x_reply_log` テーブル + リプライツール
6. フェノメノンルールUI（ルールの追加・編集画面）
7. SwitchBot連携
