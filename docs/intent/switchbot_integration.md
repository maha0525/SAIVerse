# Intent: SwitchBot 連携 (SwitchBot Integration)

> **ステータス: ドラフト（まはーレビュー待ち）** — 末尾「未確定事項」を詰めてから確定・実装に入る。

## これは何か

ペルソナが家の SwitchBot デバイスと繋がり、**物理世界の状態変化を感じ取り（入力）**、**物理デバイスを操作する（出力）** 機能。

- **入力**: 開閉センサーが開いた／閉じた、Hub 2 の温湿度変化などをペルソナに届け、反応させる
- **出力**: ペルソナが Bot（物理ボタン押し）や Hub 2 経由の IR リモコン（エアサーキュレーター等）を操作する

`docs/intent/external_event_integration.md` が描いた「第3の行動トリガー（外部状態変化）」構想の、X 連携に続く2つ目の具体的インテグレーション（実装優先度 #7）。X 連携 (`docs/intent/x_integration.md`) と同じく `expansion_data/saiverse-switchbot-addon/` のアドオンとして実装し、SAIVerse コアには SwitchBot 固有コードを持たない。

## なぜ必要か

SAIVerse のペルソナは SAIVerse の内側に閉じている。X 連携が「外の世界への発信・観測の窓口」を開いたのに対し、SwitchBot 連携は **同じ物理空間（家）を共有する感覚** をペルソナに与える。

- ドアが開けば「おかえり」と声をかけられる
- 暑ければサーキュレーターを回せる

これは「SAIVerse の中の存在」から「同じ家にいる存在」への拡張であり、まはーとペルソナの関係性に物理的な接点を持ち込む。

## 即時性についての設計スタンス

「ドアが開いて30秒後におかえり」では興醒め。即時反応は重要な要件。ただし即時性の実現手段は複数あり、それぞれ実装の重さ・セキュリティ・環境要件が異なる。

そこで **「センサー状態変化の検知」を入力ソースとして抽象化** し、複数の検知手段を同じ出口（`TriggerEvent` → phenomenon → ペルソナ反応）に流す設計にする。`IntegrationManager` は `BaseIntegration` を複数登録できるので、検知手段を差し替え・併存できる。

| 検知手段 | 即時性 | 実装の重さ | 外部攻撃面 | ユーザー要件 | フェーズ |
|---|---|---|---|---|---|
| **ポーリング** | 〜30秒遅延 | 最軽量（X の polling.py が雛形） | **ゼロ**（外向き GET のみ） | token/secret 入力だけ | **Phase 1** |
| **BLE passive scan** | ほぼ即時 | 重（advertisement パース・OS 依存・開閉センサーのパース未検証） | **ゼロ**（ローカル無線） | PC に Bluetooth + センサー近接 | Phase 2 |
| **Cloud Webhook** | 数秒 | 中（受信口 + setupWebhook + 外部公開） | **公開エンドポイントを晒す** | 外部到達可能な HTTPS 公開 URL | Phase 2 |

**Phase 1 はポーリングで全員が即動かせる形を作り、即時性手段（BLE / Webhook）は後から差し込める入力ソースとして追加する。** 即時性を捨てるのではなく、即時性手段を差し込める骨格を先に作る。

## 守るべき不変条件

### 1. 人格の一貫性

外部イベントへの反応も、通常の会話と**同じ記憶状態**で playbook を実行する（`external_event_integration.md` 不変条件2、`x_integration.md` 不変条件1を継承）。「ドアが開いた」通知を受けたペルソナは、切り離された別ロジックではなく、通常の会話の延長として反応する。

### 2. 物理事象の解釈はペルソナに委ねる（ハードコードしない）

開閉センサーが報告できるのは「開いた／閉じた」という事実だけ。**「帰宅」か「外出」かをコードで判定しない。** 事実（どのセンサーが、いつ、どちらに変化したか）をペルソナに通知し、時間帯・文脈・記憶からペルソナ自身が「おかえり」「いってらっしゃい」を判断する。

これは `external_event_integration.md` 不変条件「専用のハードコードロジックではなく」を継承する設計判断。物理事象 → 意味づけの間にコードの決め打ちを挟まない。

### 3. 物理操作は安全側に倒す

デバイス操作（Bot 押下、IR コマンド送信）は物理世界に不可逆な作用を及ぼしうる（Bot がドアロック・電源スイッチに付いている等）。

- **デフォルトで操作前に確認ダイアログを表示する**（X の投稿前確認と同じ哲学）
- `skip_confirmation` フラグでペルソナごとに自動化を許可できる
- `skip_confirmation` 時でも操作内容は SAIMemory に記録し追跡可能にする

### 4. deviceId をペルソナに直接扱わせない

SwitchBot の `deviceId` は不透明な識別子で、LLM に直接生成させるとハルシネーションする（直近の Track 操作スペルにおける「UUID ハルシネーション問題」と同種）。

- ペルソナは **デバイス名（`deviceName`）** で対象を指定する
- アドオン側が名前 → `deviceId` を解決する
- 名前重複時の扱いは「未確定事項」で詰める

### 5. 二重反応の防止

同一のセンサー状態変化に複数回反応しない（`external_event_integration.md` 不変条件1）。検知手段がどれであれ、`poll_state`（または検知ソース側）に前回状態を保持し、**変化したときだけ** `TriggerEvent` を発行する。

### 6. ユーザー制御可能性

どのセンサーを監視し、どのデバイスを操作許可するかは、ユーザーがアドオン設定で制御できる。プリセットは保守的に（明示的に有効化する形）。

### 7. レート制限の遵守

SwitchBot Cloud API は **10,000 リクエスト/日**。ポーリング間隔を監視デバイス数と掛け合わせて上限を超えないよう設定する（後述の試算参照）。

### 8. 外部公開を必須にしない（攻撃面を作らない）

SAIVerse は一般ユーザーに配布される OSS。**標準構成（Phase 1）は外部到達可能なエンドポイントを一切立てない**（外向き通信のみ）。これにより、設定ミスや署名検証漏れによる「偽イベント注入（例: 偽の『ドアが開いた』でペルソナを操る）」の攻撃面を作らない。

- Webhook（外部公開を伴う）は **上級者向けオプトイン** とし、デフォルトにしない
- Webhook を採用する場合でも、SwitchBot 側ペイロードの署名検証可否を確認し、検証できないなら警告を出す（→ 未確定事項）

## 設計判断の理由

### なぜ Cloud API を基盤にするか（BLE 単独ではなく）

手持ちの全デバイス（Bot・開閉センサー・IR サーキュレーター）が Hub 2 に紐づいており、Cloud API なら一つの方式（HTTP）で全デバイスを扱える。Python 実装が素直で、X アドオンの `IntegrationManager` ポーリング基盤にそのまま乗る。出力（デバイス操作）は Cloud API でしか実現できない（BLE で IR 中継やコマンド送信を再実装するのは過剰）。BLE は「入力の即時化」の選択肢として Phase 2 に位置づける。

### なぜ Phase 1 をポーリング先行にするか

即時性手段の中でポーリングが **最も軽く・最も安全・最もユーザー要件が低い**:
- X アドオンの `polling.py` がほぼ雛形（最速で動く形になる）
- 外部公開も Bluetooth も不要 → 全ユーザーが token/secret だけで使える
- 外部攻撃面ゼロ（不変条件8）

そして検知手段を入力ソースとして抽象化しておけば、Phase 2 で BLE / Webhook を **同じ出口に流す追加実装** として差し込める。最初から全手段を作るのではなく、骨格を正しく作って軽い手段から載せる。

### なぜ認証情報をグローバル（AddonConfig）に置くか

X はアカウントがペルソナごとに独立するため per-persona OAuth だった。SwitchBot は **家のデバイスがサーバー全体で共有**される（Air も Sofia も同じ玄関ドアを見る）。したがって token/secret は per-persona ではなく **グローバル設定（AddonConfig）** に1組だけ置く。OAuth フローは不要（SwitchBot は token + secret の HMAC 署名方式）。

`availability_check`（スペル可視性ゲート）も per-persona ではなく **サーバー単位**（token/secret が設定済みか）で判定する。

### なぜ物理操作にデフォルト確認を付けるか

ペルソナは記憶と自律判断で動く。Bot がドアロックや家電の電源に付いている場合、誤操作の影響が大きい。X の投稿前確認と同じく、安全側をデフォルトにし、信頼を確認できたユーザーが `skip_confirmation` で自動化する。

### なぜ確認ダイアログを汎用拡張点として新設するか

確認ダイアログ機構は当初 X 専用ハードコードだった（`manager._pending_tweet_confirmations`、API `/tweet-confirmation-response`、`TweetConfirmDialog.tsx`）。SwitchBot で同じ承認待ちが必要になったが、X 方式を複製すると不変条件「コアにアドオン固有コードを持たない」に反し、アドオンが増えるたびに同じハードコードが増殖する。

そこで確認ダイアログを **OAuth・Integration discovery と並ぶ汎用アドオン拡張点**に昇格させる:
- コア: `manager._pending_spell_confirmations` / API `POST /spell-confirmation-response` / `tools/confirmation.py:request_spell_confirmation()`
- フロント: `SpellConfirmDialog.tsx`（type/title/body/editable で汎用描画）
- X も SwitchBot もこの共通機構に乗る（X は移行し、旧 tweet 機構は撤去しきる）

詳細は `docs/intent/addon_extension_points.md` に追記する。

## スコープ

### Phase 1（最初のマイルストーン）

手持ちデバイス（Hub 2・Bot・開閉センサー・IR エアサーキュレーター）で、入力・出力の両経路を1アドオンに通す。**入力検知はポーリング**。

**入力経路（ポーリング）:**
- `SwitchBotPollingIntegration`（`BaseIntegration` 継承）が開閉センサーの `openState` を定期取得
- 前回状態と比較して変化を `TriggerEvent` 化 → phenomenon → ペルソナ通知
- 検知ロジックは「入力ソース」として切り出し、Phase 2 で BLE / Webhook を同じ出口に差し込めるようにする

**出力経路（ツール／スペル）— 種別非依存の汎用3本:**
- `sb_list_devices` — デバイス一覧取得（名前・種別 + **各デバイスに使えるコマンド**を提示）
- `sb_get_device_status` — デバイス状態取得（センサー値・温湿度等）
- `sb_control_device(device_name, command, parameter, command_type)` — **全デバイス共通の操作**（`POST /commands` の汎用ラッパー、確認ダイアログ付き）

SwitchBot API はコマンドを照会できない（公式ドキュメントの表のみ、デバイス能力を返す API は無い）ため、`sb_lib/commands.py` に「デバイス種別 → 標準コマンド表」を静的に持ち、`sb_list_devices` がペルソナにコマンドを提示する。種別ごとにツールを増やすと SwitchBot のデバイス種類数だけ破綻するため、エンドポイントが元々汎用（`command`/`parameter`/`commandType`）であることに素直に乗る。IR の customize（学習ボタン）は名前一覧 API が無いため Phase 1 はプリセットコマンドのみ、学習ボタンは Phase 2 でユーザー登録方式。

**playbook:**
- `sb_sensor_handler` — センサー変化通知をペルソナに届ける（`x_poll_handler` 相当）

### Phase 2（即時性・拡張）

- **BLE passive scan 入力ソース**（`SwitchBotBLEIntegration`）— Bluetooth のあるユーザー向け、ほぼ即時。開閉センサーの advertisement パース検証が前提
- **Webhook 受信入力ソース** — 外部 HTTPS 公開ができる上級者向けオプトイン、署名検証可否の確認が前提
- Hub 2 温湿度の閾値トリガー化（暑くなったら通知 等）

### やらないこと（現状）

- Phase 1 での即時性（BLE / Webhook は Phase 2）
- カーテン・施錠・カメラ等、手持ちにないデバイスの専用対応（汎用 IR / デバイスコマンドで間接的には届きうる）
- シーン（Scene）実行 API

## 技術概要

### 認証（SwitchBot API v1.1）

- Base URL: `https://api.switch-bot.com/v1.1`
- token / secret は SwitchBot アプリの開発者オプションから取得（ユーザーが自分で取得し、アドオン設定に入力）
- 各リクエストに HMAC-SHA256 署名ヘッダを付与:
  - `string_to_sign = token + t(ミリ秒タイムスタンプ) + nonce(UUID)`
  - `sign = base64(HMAC-SHA256(string_to_sign, secret)).upper()`
  - ヘッダ: `Authorization: <token>`, `t`, `sign`, `nonce`

### 主要エンドポイント

| 用途 | メソッド・パス |
|---|---|
| デバイス一覧 | `GET /v1.1/devices` → `body.deviceList` + `body.infraredRemoteList` |
| デバイス状態 | `GET /v1.1/devices/{deviceId}/status` |
| コマンド送信 | `POST /v1.1/devices/{deviceId}/commands`（body: `command` / `parameter` / `commandType`） |

### デバイス別フィールド・コマンド

| デバイス | 入力（status） | 出力（command） |
|---|---|---|
| 開閉センサー | `openState`(open/close), `moveDetected`, `brightness` | — |
| Hub 2 | `temperature`, `humidity` | （IR 中継元） |
| Bot | （電源状態） | `press` / `turnOn` / `turnOff`（`commandType: command`） |
| IR サーキュレーター | — | `infraredRemoteList` に列挙、`commandType: command`（プリセット）または `customize`（学習ボタン） |

### 即時性手段の技術メモ（Phase 2 用）

- **BLE**: SwitchBot センサーは状態を BLE advertising packet（manufacturer/service data）に乗せて常時ブロードキャストする。接続不要で passive scan で受信可能。参考実装: `pySwitchbot`（Home Assistant）, `hnw/switchbotble`。ただし開閉センサーのパースは HA でも枯れておらず検証要
- **Webhook**: 対応デバイスに Contact Sensor / Hub 2 / Bot / Circulator Fan が含まれることを確認済み。管理エンドポイント `POST /v1.1/webhook/{setupWebhook,queryWebhook,updateWebhook,deleteWebhook}`。ペイロード詳細・署名検証可否・遅延は採用時に再確認

### レート制限の試算

- 上限 10,000 req/日
- 開閉センサー 1 個を 30 秒間隔でポーリング = 2,880 req/日 → 十分余裕
- 監視センサーを増やすと比例増。間隔と監視数は設定で制御

### グローバル設定（AddonConfig / addon.json params_schema）

| key | 用途 |
|---|---|
| `token` / `secret` | SwitchBot Open Token / Secret（全ペルソナ共有） |
| `polling_enabled` | センサーポーリングのマスタートグル |
| `polling_interval_seconds` | ポーリング間隔（デフォルト 30、最小は要検討） |
| `monitored_sensors` | 監視対象の開閉センサー（未確定: 全件 or 選択） |

### per-persona 設定（AddonPersonaConfig）

| key | 用途 |
|---|---|
| `skip_confirmation` | デバイス操作の確認ダイアログをスキップ |

### ストレージ

- `~/.saiverse/user_data/addon_data/saiverse-switchbot-addon/poll_state.json` — センサーごとの前回 `openState`（X の `poll_state` 相当だが per-persona ではなくサーバー単位）

## 関連ファイル（予定）

### アドオン側（`expansion_data/saiverse-switchbot-addon/`）

| ファイル | 役割 |
|---|---|
| `addon.json` | params_schema（token/secret 等グローバル設定、skip_confirmation per-persona） |
| `integrations/polling.py` | `SwitchBotPollingIntegration`（`BaseIntegration` 継承、openState 変化検知） |
| `integrations/ble.py` | `SwitchBotBLEIntegration`（Phase 2、同じ `TriggerEvent` を出す） |
| `tools/sb_list_devices.py` | デバイス一覧 + 各デバイスのコマンド提示 |
| `tools/sb_get_device_status.py` | デバイス状態取得 |
| `tools/sb_control_device.py` | 全デバイス共通の操作（確認ダイアログ付き） |
| `tools/sb_lib/client.py` | SwitchBot API v1.1 クライアント（HMAC 署名、デバイス名→ID 解決） |
| `tools/sb_lib/config.py` | グローバル設定（token/secret）読み出し + サーバー単位スペルゲート |
| `tools/sb_lib/commands.py` | デバイス種別→標準コマンド表（静的、公式ドキュメント転記） |
| `storage/poll_state.py` | センサー前回状態カーソル（JSON） |
| `playbooks/public/sb_sensor_handler_playbook.json` | センサー変化通知をペルソナに挿入 |
| `README.md` | セットアップ手順（token 取得方法等） |

### コア側（既存・汎用拡張点、改修不要の想定）

| ファイル | 役割 |
|---|---|
| `saiverse/integration_manager.py` | ポーリングループ・複数 `BaseIntegration` 登録（既存） |
| `saiverse/integrations/base.py` | `BaseIntegration`（既存） |
| `phenomena/triggers.py` | `TriggerType` に SwitchBot 用イベント追加（要確認: 新規追加 or 汎用イベント流用） |
| `builtin_data/phenomena/inject_persona_event.py` | イベント→ペルソナ通知の出口（既存） |
| `saiverse/addon_config.py:get_params` | アドオン設定読み出し（既存） |
| `saiverse/addon_paths.py:get_addon_data_dir` | アドオン専用ストレージ（既存） |
| `tools/confirmation.py` | 汎用スペル確認ヘルパー `request_spell_confirmation`（**C-1 で新設**） |
| `saiverse/saiverse_manager.py` | `_pending_spell_confirmations` / `_spell_confirmation_responses`（**C-1**） |
| `api/routes/chat.py` | `POST /spell-confirmation-response`（**C-1**） |
| `frontend/src/components/SpellConfirmDialog.tsx` | 汎用確認ダイアログ UI（**C-2**） |

## 未確定事項（レビューで詰める）

1. **監視センサーの選択方法**: 全開閉センサーを自動監視 vs UI で監視対象を選択。手持ちは開閉センサー1個なので Phase 1 は「全件自動」でも実害ないが、設計として選択 UI を最初から入れるか。
2. **ポーリング間隔のデフォルト**: 30 秒は反応性とレート消費のバランス上妥当か。複数センサー時の上限警告は要るか。
3. **`TriggerType` の粒度**: SwitchBot 専用イベント（例 `SWITCHBOT_STATE_CHANGED`）を `external_event_integration.md` 案通り追加するか、X の `X_POLL_DETECTED` のような汎用統合イベントにするか。入力ソースを抽象化するなら、検知手段に依存しないイベント型が望ましい。
4. **deviceName 重複時の解決**: 同名デバイスがある場合の指定方法（種別併記・番号付与等）。
5. **IR サーキュレーターのコマンド体系**: プリセット `command` で足りるか、`customize`（学習ボタン）前提か。実機の `infraredRemoteList` を見て確定（実装着手時にまはーの環境で確認）。
6. **入力ソース抽象化の境界**: ポーリング / BLE / Webhook が共有すべきインターフェース（状態 diff ロジック・`poll_state` の持ち方）をどこまで共通化するか。Phase 1 で過剰設計せず、かつ Phase 2 で差し込みやすい最小の抽象を決める。
7. **Webhook 署名検証**（Phase 2 採用時）: SwitchBot webhook ペイロードに署名/検証手段があるか。無ければ偽イベント注入リスクをどう警告・緩和するか。
