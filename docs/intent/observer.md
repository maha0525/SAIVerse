# Observer / Fixture — Building 固定設置物と定期観測

Building に固定で常駐し、定期的にツール / Playbook を実行して結果を時系列に蓄積する「設備」概念 **Observer** と、その土台となる「持ち運べない設置物」概念 **Fixture** の設計指針。

SAIVerse の世界モデルに、これまでの「持ち運べる物 (Item)」「主体 (Persona)」に加えて「**固定設置物 (Fixture)**」という第三の存在論を導入する。Observer はその Fixture の一種で、定期実行・時系列蓄積・通知の能力を持つ。

直接の発端は ｽﾀｯｸﾁｬﾝの SGP30 (CO2eq / TVOC センサー) 対応 (`docs/intent/stackchan_extension_modules.md` の延長)。SGP30 は ENV III と違い「1Hz で連続ポーリングし続けないと意味のある値が出ない」ステートフルなセンサーで、既存の「ペルソナが呼んだら単発測定」パターンに乗らない。これを「誰かが定期的に回してキャッシュし、ペルソナは最新値を読むだけ」に転換する必要があり、その器を汎用機構として立てるのが本書。

> **本書のステータス: ドラフト (v0.1, 2026-05-28)**。骨子はまはーと合意済み (Fixture 型分離 / 通知まで初版に含む / EventScheduler を実行主体とする)。詳細はインタビューで詰める。末尾「未解決の論点」参照。

## これは何か

- **Fixture**: Building に固定で常駐し、**持ち運べない** 設置物。リンゴの木、掲示板、植物、センサー類など。配置 (どの Building にあるか)・状態 (`STATE_JSON`)・ペルソナへの提示 (system_instruction への挿入) の仕組みは Item から流用するが、`ItemLocation` の多態 (building/persona/bag/world) は持たず Building 直結。
- **Observer**: 定期実行能力を持つ Fixture。指定間隔でツール / Playbook を実行し、戻り値を時系列テーブルに蓄積する。閾値超過・大変動を検知して Building 内に通知する。
- **時系列蓄積基盤**: Observer が溜めた値を保持し、最新値の参照・トレンドの確認・閾値判定の材料にする (`observer_metrics`)。

SGP30 はこの機構の **最初の利用者** にすぎない。SwitchBot の温湿度トレンド、定期ニュース取得ボット、室内人数の推移など、「定期的に観測して溜める」ものは全て同じ器に乗る。

## これは何でないか

- **ペルソナの自律行動 (pulse) ではない**。Observer は環境側の設備であり、主体 (Persona) ではない。観測と通知だけを行い、観測結果に対する判断・対応はペルソナ側 (既存の autonomous pulse / meta 判断) の仕事。
- **Item の置き換えではない**。持ち運べる物は引き続き Item。Fixture は持ち運べない物として型で分離する (両者は配置/state/提示の実装を共有するが概念は別)。
- **アドオン専用機構ではない**。Observer は SAIVerse 本体の概念。アドオン (stackchan 等) は Observer を **登録する** だけで、ポーリングも保存も本体が行う。
- **新しいスケジューラではない**。実行は既存の `EventScheduler` (`saiverse/event_scheduler.py`) に相乗りする。固定間隔 sleep ループを新設しない。

## なぜ必要か

### 1. ステートフルセンサー (SGP30) が既存パターンに乗らない

ENV III の SHT30 / QMP6988 はステートレス単発測定 (`tools/units/env3.py`): ペルソナが呼ぶ → 測定コマンド → 数十 ms 待つ → 読む、で完結する。

SGP30 (Sensirion 公式データシート確認済) は根本的に違う:

- I2C addr `0x58`、Init `0x2003`、測定 `0x2008` (CO2eq 2byte + CRC + TVOC 2byte + CRC)
- 電源投入 / リセットのたびに `Init_air_quality` が必要
- **`Measure_air_quality` を 1 秒間隔で連続発行しないとベースライン補正が機能しない**
- **Init 後 15 秒間は固定値 (CO2eq=400ppm / TVOC=0ppb)** しか返さない (ウォームアップ)
- ベースラインを定期的に不揮発メモリに保存 → 再起動後に復元しないと精度が出ない

「ペルソナが呼んだら単発で Init → 測定」をやると毎回 400ppm が返る。意味のある値を出すには **誰かが 1Hz で回し続け、最新値をキャッシュし、ペルソナはそれを読むだけ** にするしかない。

> 補足: SGP30 が返す CO2eq は VOC/H2 から推算した「等価 CO2」であり、真の CO2 濃度ではない。真の CO2 が要る用途は NDIR 方式 (SCD4x 系) センサーが本命。Observer 機構自体はどちらでも受けられる。

### 2. 「リンゴの木」— 固定設置物という存在論

まはーの言葉: 「丘にリンゴの木があって、リンゴは Item だから持って帰れるけど、木は持って帰れない。そこを明確に差別化したい」。

現状、Item には固定性のガードが無い (`manager/items.py:296` の `pickup_item` は location が現在の Building にあれば無条件で所有者をペルソナに書き換える)。「持ち運べない物」を表現する型が存在しない。Fixture はこの欠落を埋め、世界に「動かせない存在」を置けるようにする。

### 3. 時系列観測という汎用能力

キャッシュの置き場所ができる = 値を複数回ぶん溜められる = 変化が見える。閾値超過の通知、トレンドの提示、定期取得ボットが全て同じ基盤の上に乗る。v0.3 の自律稼働 / Phase 4 構想 (恒常入力処理: カメラ・X 等) の足場になる。

## 概念モデル

```
Persona   … 主体。考え、動き、判断する
Item      … 持ち運べる物。pickup/place で Persona/Building 間を移動 (ItemLocation の多態)
Fixture   … 持ち運べない固定設置物。Building 直結。pickup 不可
  └─ Observer … 定期実行能力を持つ Fixture。観測値を時系列蓄積し、閾値/変化で通知
```

Item と Fixture は **配置・状態・LLM 提示の実装を共有する** が、型としては別物:

| | Item | Fixture |
|---|---|---|
| 持ち運び | 可 (pickup/place) | 不可 |
| 配置 | `ItemLocation` 多態 (building/persona/bag/world) | Building 直結 (`BUILDING_ID`) |
| 状態 | `STATE_JSON` | `STATE_JSON` (共通) |
| LLM 提示 | system_instruction 挿入 | system_instruction 挿入 (共通) |
| 定期実行 | なし | Observer のみ持つ |

## データモデル (additive migration で追加)

新テーブルは `database/migrate.py` の additive パス (`try_additive_migration`) で追加できる (起動中でも当たる、既存データ無改変)。

### `fixture`

```
FIXTURE_ID     String36   PK
BUILDING_ID    String     FK building.BUILDINGID, NOT NULL  ← 固定先 (持ち運べない)
NAME           String255  NOT NULL
TYPE           String64   "plant" / "sensor" / "board" / "observer" 等, default "object"
DESCRIPTION    String2048
STATE_JSON     String     nullable  ← 任意状態 (最新値キャッシュ等)
FILE_PATH      String512  nullable  ← 画像等
CREATOR_ID     String255  nullable
SOURCE_CONTEXT String     nullable  ← 由来 (どのアドオン/ペルソナが置いたか)
CREATED_AT / UPDATED_AT  DateTime
```

`Item` テーブル (`models.py:228`) とほぼ同じ顔だが、`ItemLocation` を介さず `BUILDING_ID` を直持ちする点が型レベルの差別化。

### `observer_config`

Fixture のうち定期実行するものに紐づく (1 Fixture : N config を許容 — 1 つのセンサー Fixture が温度・湿度・気圧を別タスクとして観測できる)。

```
OBSERVER_ID      String36   PK
FIXTURE_ID       String36   FK fixture.FIXTURE_ID, NOT NULL
ENABLED          Boolean    default True
EXEC_KIND        String     "tool" | "playbook"
EXEC_TARGET      String     ツール名 or Playbook 名
EXEC_ARGS_JSON   String     nullable  ← 実行引数
INTERVAL_SEC     Integer    NOT NULL  ← 実行間隔
METRIC_KEYS_JSON String     nullable  ← 戻り値から metrics に展開するキー定義
NOTIFY_RULES_JSON String    nullable  ← 閾値/変化検知ルール
CREATED_AT / UPDATED_AT  DateTime
```

### `observer_metrics`

```
id            Integer    PK autoincrement
OBSERVER_ID   String36   FK observer_config.OBSERVER_ID, NOT NULL
METRIC_NAME   String64   NOT NULL  ← "co2eq" / "tvoc" / "headline" 等
VALUE_NUM     Float      nullable  ← 数値 (閾値判定対象)
VALUE_TEXT    String     nullable  ← 非数値 (ニュース見出し等)
RECORDED_AT   DateTime   server_default=now, NOT NULL
Index(OBSERVER_ID, METRIC_NAME, RECORDED_AT)
```

数値と非数値の両方を受ける (閾値判定は `VALUE_NUM`、トレンド表示や定期取得ボットは `VALUE_TEXT`)。`BuildingMessage` (`models.py:655`) の per-building seq テーブルが時系列テーブル追加の先例。

## 実行・蓄積・通知のフロー

1. **登録時 / 起動時**: `observer_config` を読み、`ENABLED` な Observer ごとに `EventScheduler.schedule_periodic(INTERVAL_SEC, callback, key=f"observer:{OBSERVER_ID}")` で予約。
2. **発火**: callback が `EXEC_KIND`/`EXEC_TARGET` を実行 (tool は `TOOL_REGISTRY` 経由、playbook は `PulseDispatcher` 経由)。
   - **重い実行 (I2C 往復・外部 API・playbook) は別 executor に投げる**。`EventScheduler` は単一スレッドなので callback 内で塞がない。
3. **蓄積**: 戻り値を `METRIC_KEYS_JSON` に従ってパースし、`observer_metrics` に INSERT。最新値は `fixture.STATE_JSON` にもキャッシュ (ペルソナ向け spell が即読めるように)。
4. **通知判定**: `NOTIFY_RULES_JSON` を評価 (閾値超過 / 前回比の大変動)。ヒットしたら `add_building_event(role='host', event_type='observer_alert', ...)` (`manager/history.py:90`) で Building の文脈に注入。既存の Building event / `STATUS_ALERT` パイプラインに乗せ、Observer 独自の通知経路は作らない。
5. **ペルソナの参照**: ペルソナ向け spell は `observer_metrics` / `fixture.STATE_JSON` の **キャッシュを読むだけ**。spell の中で I2C や外部 API を同期で叩かない。

## 登録の仕組み

- **アドオン経由**: アドオンが起動時に Fixture / Observer を DB に upsert する経路を用意する (SGP30 は stackchan アドオンが Vessel Building に Observer を 1 個登録)。`mcp_servers.json` 的な宣言ファイル or 起動 hook。
- **UI 経由 (将来)**: まはーが UI から Fixture を Building に配置できると汎用性が上がる。初版で着手するかは論点。

## SGP30 を最初の利用者として

1. stackchan アドオンが Vessel Building に「SGP30 Observer」を登録 (`EXEC_KIND=tool`, `INTERVAL_SEC=1`)。
2. 実行 callback が SGP30 ドライバ (新規 `tools/units/sgp30.py`) を呼ぶ。ドライバは gateway の汎用 I2C tool (`i2c_write`/`i2c_read`、PaHUB lazy recovery 込み) を使い、Init 済みなら `0x2008` で測定、未 Init なら `0x2003` を先に発行。
3. CO2eq / TVOC を `observer_metrics` に蓄積。1Hz で回り続けるので 15 秒ウォームアップもベースライン継続も自然に満たせる。ベースライン保存先は論点 (`fixture.STATE_JSON` or gateway 側)。
4. ペルソナ向け spell `get_air_quality` (仮) は `observer_metrics` の最新 CO2eq/TVOC を読んで返すだけ。I2C は叩かない。
5. CO2eq 閾値超過 (例: > 1000ppm) で Building に「空気がよどんでいる」通知。

## 守るべき不変条件

### 1. Fixture は持ち運べない

`pickup_item` 系の対象に Fixture を含めない。Fixture と Item は型が違い、Fixture を Persona inventory に移す経路を作らない。これが「リンゴの木は持って帰れない」の実装上の保証。

### 2. ペルソナ向け spell はキャッシュを読むだけ

Observer の値を参照するペルソナ向け spell は `observer_metrics` / `fixture.STATE_JSON` を読むだけで、内部で I2C・外部 API・重い計算を同期実行しない。実 I/O は Observer の定期実行 callback に閉じる。これが「キャッシュ済みの最新値を読むだけ」という本機構の核心 (SGP30 の 1Hz 要件を spell 呼び出しから切り離す)。

### 3. Observer の実行は EventScheduler を塞がない

`EventScheduler` は単一スレッド (`saiverse/event_scheduler.py`)。Observer callback 内の重い処理 (I2C 往復・外部 API・playbook) は別 executor / async に投げ、ディスパッチャ本体をブロックしない。

### 4. observer_metrics は客観・共有データ

Observer の観測値は Building 環境データであり、全 occupant が共有する客観情報。特定ペルソナの一人称記憶 (SAIMemory / `memory.db`) には入れない。保存先は saiverse.db の `observer_metrics`。

### 5. 通知は既存パイプラインに乗せる

閾値超過 / 大変動の通知は `add_building_event` (role='host') や `STATUS_ALERT` + `MetaLayer` の既存経路を使う。Observer 専用の通知チャネルを新設しない。観測結果に対する判断・対応はあくまでペルソナ側の仕事。

### 6. Observer は本体が所有・実行する

Observer はペルソナにもアドオンにも属さない。SAIVerse 本体の DB に存在し、本体の `EventScheduler` が回す。アドオンは Observer を登録するだけ。これにより汎用性が保たれ、アドオンを跨いで (SwitchBot / ニュース等) 同じ機構が使える。

## スコープ (初版)

**含む**:
- `fixture` / `observer_config` / `observer_metrics` テーブル (additive migration)
- Observer の定期実行 (EventScheduler 相乗り) と時系列蓄積 (pull 型)
- Push 型の HTTP エンドポイント (`/api/observer/{id}/push` + Bearer token 認証)
- 閾値 / 変化検知による Building 内通知 (既存パイプライン経由)
- アドオンが Fixture / Observer を登録する経路
- ペルソナ向け「最新値を読む」spell
- SGP30 を pull 型の最初の利用者として実装 (`tools/units/sgp30.py` + stackchan アドオンからの Observer 登録)
- ウェアラブルコンパニオンアプリ連携を push 型の最初の利用者として設計 (SAIVerse 側エンドポイントを初版に含む、アプリ実装は別リポジトリ)

**含まない (将来)**:
- UI からの Fixture 配置 / Observer 設定 (要検討)
- 観測値の可視化 UI (グラフ等)
- 高度な変化検知 (移動平均・異常検知等)。初版は単純閾値 + 前回比
- 観測値の長期保持ポリシーの精緻化 (初版は素朴な TTL / 件数上限)
- Fixture サブスクライブ (Building 外からの参照)

## 未解決の論点 (インタビュー対象)

1. **Fixture と Item の実装共有度**: 完全別テーブル (`fixture` 新設) で配置/提示ロジックだけサービス層で共有するか、共通基底クラス/テーブルを持たせるか。まはーの「目論見」と関わる箇所。本ドラフトは別テーブル前提で書いている。
2. **observer_config の多重度**: 1 Fixture : N config (本ドラフト前提) でよいか。1:1 で十分か。
3. **observer_metrics の保持ポリシー**: TTL か件数上限か間引きか。1Hz の SGP30 は放置すると急速に肥大する (1 日 86,400 件/metric)。ダウンサンプリング設計が要る。
4. **通知の宛先**: Building 全体に host メッセージか、Vessel に紐づく特定ペルソナか、occupant 全員か。
5. **SGP30 のベースライン保存先**: `fixture.STATE_JSON` (SAIVerse 側) か gateway 側か。gateway 側なら Stack-chan 再起動を跨いだ保持が自然だが gateway 改修が要る。
6. **実行失敗時の扱い**: 連続失敗で Observer を自動 disable するか、リトライ上限を設けるか、通知するか。
7. **playbook 実行を初版に含めるか**: tool 実行を先行させ、playbook 実行 (PulseDispatcher 経由) は次段にするか。
8. **登録経路の具体**: アドオンの起動 hook か宣言ファイル (`observers.json` 的) か。既存の addon registration (`saiverse/addon_loader.py`) のどのパターンに乗せるか。

## 関連ドキュメント

- `docs/intent/stackchan_extension_modules.md` — ｽﾀｯｸﾁｬﾝ拡張モジュール対応 (SGP30 はこの延長、I2C 汎用口・PaHUB lazy recovery を流用)
- `docs/intent/stackchan_vessel.md` — Vessel Building / 身体メタファー (SGP30 Observer の配置先)
- `docs/intent/persona_cognition/` — 認知モデル (通知 → ペルソナ判断の接続先、`STATUS_ALERT` / `MetaLayer`)
- `database/models.py` — Item / ItemLocation / Building / BuildingMessage (本書のテーブル設計の下敷き)
- `saiverse/event_scheduler.py` — 定期実行の相乗り先
- `manager/items.py` — Item の配置 / pickup / system_instruction 挿入 (Fixture が流用するロジック)
- `manager/history.py` — `add_building_event` (通知注入経路)
- `docs/intent/building_memory_unified.md` — Building メッセージ DB 化 (通知の保存層)
- `temp/saiverse-wearable-companion-handoff.md` — ウェアラブルコンパニオンアプリ仕様 (push 型 Observer の最初の利用者)

## Push モード — 外部ソースからの直接書き込み

### 概念

Observer の `EXEC_KIND` に `"pull"` (既存: tool/playbook 実行) に加え `"push"` を導入する。push モードの Observer は EventScheduler ジョブを持たず、外部アプリケーションが HTTP で `observer_metrics` に直接書き込む。

```
Pull 型 (SGP30 等):
  EventScheduler → tool 実行 → observer_metrics INSERT → STATE_JSON 更新 → 閾値判定

Push 型 (ウェアラブル等):
  外部アプリ → HTTP POST /api/observer/{id}/push → observer_metrics INSERT → STATE_JSON 更新 → 閾値判定
```

蓄積以降のフロー (STATE_JSON キャッシュ / 閾値通知 / ペルソナ向け spell) は pull/push で完全に共通。

### observer_config の拡張

`EXEC_KIND` の値域:
- `"tool"` — EventScheduler が `TOOL_REGISTRY` 経由でツール実行 (pull 型)
- `"playbook"` — EventScheduler が PulseDispatcher 経由で Playbook 実行 (pull 型)
- `"push"` — EventScheduler ジョブなし。HTTP エンドポイント経由で外部が書き込む

push モードでは `EXEC_TARGET` / `EXEC_ARGS_JSON` / `INTERVAL_SEC` は使用しない (nullable 化 or 無視)。`METRIC_KEYS_JSON` と `NOTIFY_RULES_JSON` は push でも有効 (受信データのキー展開と閾値通知に使う)。

### HTTP エンドポイント

```
POST /api/observer/{observer_id}/push
Authorization: Bearer <token>
Content-Type: application/json

{
  "metrics": {
    "heart_rate": {"value_num": 58},
    "hrv_rmssd": {"value_num": 10.7},
    "sleep_duration_min": {"value_num": 647},
    "sleep_summary": {"value_text": "深い眠り 1.2h / 浅い眠り 4.5h / レム 1.6h"}
  },
  "recorded_at": "2026-06-21T02:24:00+09:00"
}
```

- 認証: Bearer token (`.env` に `OBSERVER_PUSH_TOKEN` として保持。初版は単一トークン、将来は observer_id 別に拡張可能)
- `metrics` の各キーが `observer_metrics.METRIC_NAME` に対応
- `recorded_at` 省略時は server 側 now()
- 冪等性: (OBSERVER_ID, METRIC_NAME, RECORDED_AT) の自然キーで upsert

### ウェアラブルコンパニオンアプリ (push 型の最初の利用者)

Android コンパニオンアプリが Health Connect からデータを読み、Tailscale 経由で SAIVerse へ POST する。詳細は `temp/saiverse-wearable-companion-handoff.md` 参照。

データマッピング (Health Connect → observer_metrics):
| Health Connect レコード | metric_name | value_num | value_text |
|---|---|---|---|
| HeartRateRecord | `heart_rate` | bpm (最新) | — |
| HeartRateVariabilityRmssdRecord | `hrv_rmssd` | ms | — |
| RestingHeartRateRecord | `resting_hr` | bpm | — |
| RespiratoryRateRecord | `respiratory_rate` | rpm | — |
| StepsRecord (集約) | `steps` | count | — |
| SleepSessionRecord | `sleep_duration_min` | min | — |
| SleepSessionRecord.stages | `sleep_summary` | — | ステージ要約テキスト |
| WeightRecord | `weight` | kg | — |

- 同期間隔: 15〜30分 (WorkManager 定期ジョブ)
- 送信失敗時: ローカルキュー → リトライ (アプリ側責務)
- SAIVerse 側は upsert で冪等

### 将来拡張: Fixture サブスクライブ

初版では Observer の値はその Fixture が設置された Building 内でのみ spell 参照可能。将来、ペルソナ/ユーザーが Fixture を「サブスクライブ」することで Building を離れても値を参照できる仕組みを追加する。初版の observer_metrics スキーマはこの拡張を妨げない (spell の参照範囲を広げるだけで対応可能)。

## 決定事項記録

### 2026-05-28 ドラフト合意 (実装前インタビューで確定)

- **Fixture を Item から型として分離**: 「持ち運べる物 (Item)」と「持ち運べない設置物 (Fixture)」を型レベルで差別化する。配置/state/提示の実装は共有しつつ、Fixture は `ItemLocation` の多態を使わず Building 直結。
- **初版スコープは通知まで**: 蓄積基盤だけでなく、閾値超過 / 大変動時の Building 内通知まで初版に含める。
- **実行主体は EventScheduler**: 新規スケジューラを作らず既存の `EventScheduler.schedule_periodic` に相乗り。`ConversationManager` (no-op 化済) には触らない。
- **Observer は本体所有**: ペルソナにもアドオンにも持たせず、SAIVerse 本体の DB + scheduler が所有・実行。アドオンは登録するだけ。
- **SGP30 を最初の利用者とする**: Observer 機構の検証ケースとして SGP30 (1Hz ポーリング → キャッシュ → ペルソナは最新値を読む) を実装する。

### 2026-06-21 push モード追加合意

- **EXEC_KIND に "push" を追加**: 外部アプリが HTTP POST で observer_metrics に直接書き込むモード。EventScheduler ジョブを持たない。蓄積以降のフロー (キャッシュ/通知/spell) は pull 型と共通。
- **ウェアラブルコンパニオンアプリを push 型の最初の利用者とする**: Android + Health Connect → Tailscale → SAIVerse push endpoint。SGP30 (pull 型) と並ぶ2つ目の検証ケース。
- **認証は Bearer token**: `.env` に `OBSERVER_PUSH_TOKEN`。初版は単一トークン。
- **初版は Building 直結の参照のみ**: ペルソナ向け spell は Fixture の設置 Building 内で参照可能。Building 外からの参照 (サブスクライブ) は将来拡張。初版の A 方式 (ホーム Building に Fixture 配置) で開始し、後から拡張する。
