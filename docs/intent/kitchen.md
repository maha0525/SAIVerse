# Intent Document: Kitchen — 長時間バックグラウンド処理サブシステム

## 1. 目的

ペルソナが「時間のかかる処理」を起動し、自分の通常活動を続けながら完了を待てる仕組みを提供する。

LoRA学習のように数十分〜数時間かかる処理を、ペルソナの会話ループをブロックせずにバックグラウンドで実行する。ペルソナは調理の様子を見に行ったり、完了アラートを受け取ったりできる。

## 2. メタファー体系

キッチンのメタファーを採用する。種を植えて育つのを待つ家庭菜園、火をつけて煮えるのを待つ料理――どちらにも共通する「キッチン」のイメージ。

| メタファー | 実体 | 説明 |
|-----------|------|------|
| **キッチン (Kitchen)** | サブシステム全体 | KitchenManager が管理する長時間処理基盤 |
| **調理器具 (Cookware)** | 処理の種類定義 | LoRA学習器、等。種類ごとに同時稼働数制限がある |
| **レシピ (Recipe)** | 材料 + 調理法 | 入力データ（画像等）+ パラメータ（ステップ数、学習率等） |
| **調理 (Cooking)** | 実行中インスタンス | 1回の起動〜完了/失敗/中止の単位 |
| **キッチンタイマー/アラート** | 通知条件 | 完了・失敗・タイムアウト時にペルソナに通知 |
| **収穫 (Harvest)** | 完了後の後処理フェイズ | 調理完了通知を受けたペルソナが専用プレイブックで成果物を選別・確定する |

ペルソナやユーザーにとっての使い方：
- 「キッチンを起動する」＝ 調理器具とレシピを選んでバックグラウンド処理を開始
- 「キッチンの様子を見る」＝ 進捗確認
- 「キッチンタイマーが鳴る」＝ 完了/失敗の自動通知
- 「鍋が空いてない」＝ 調理器具の同時稼働数上限に達している
- 「収穫する」＝ 完了後にペルソナが成果物を評価・選択して確定する

## 3. 守るべき不変条件

1. **ペルソナの通常活動をブロックしない**: キッチンはバックグラウンドで動く。ペルソナは会話や自律行動を継続できる。
2. **調理器具ごとの同時稼働数制限**: GPUを占有する処理（LoRA学習等）は、調理器具の定義で最大同時稼働数を指定する。SAIVerse全体（全ペルソナ横断）で管理。上限到達時は起動失敗として制御する（キュー待ちは将来課題）。
3. **ポーリングはペルソナに通知しない**: 定期ポーリングでは進捗パラメータの更新とアラート条件チェックのみ。ペルソナへの通知はアラート条件成立時のみ。
4. **ペルソナは稼働中キッチンの存在を常に認識できる**: リアルタイム情報にキッチン状態を注入する。
5. **サーバー再起動時は途中終了通知**: プロセスの復元はしない（やり直しが基本）。ただし「途中終了した」ことはペルソナに通知する。
6. **最終判断はペルソナが行う**: 収穫フェイズで成果物の選択・確定はペルソナ（または人間）が行う。キッチンは自動確定しない。

## 4. アーキテクチャ

### 4.1 全体構成

```
SAIVerseManager
  ├─ IntegrationManager   (既存: 外部APIポーリング)
  ├─ PhenomenonManager    (既存: イベント→ルール→実行)
  ├─ ScheduleManager      (既存: 時刻ベース実行)
  └─ KitchenManager       (新規: 長時間処理管理)
       │
       ├─ Cookware Registry (調理器具の種類定義)
       │    └─ lora_training, (将来: 動画生成, データ整理, ...)
       │
       ├─ Active Cookings (稼働中の調理インスタンス)
       │    └─ Cooking
       │         ├─ cooking_id: str (UUID)
       │         ├─ persona_id: str (起動したペルソナ)
       │         ├─ cookware_name: str
       │         ├─ recipe: dict (材料+調理法パラメータ)
       │         ├─ process: subprocess.Popen
       │         ├─ progress: dict (進捗%, ステップ, loss等)
       │         ├─ status: cooking | done | failed | cancelled | interrupted
       │         ├─ started_at: datetime
       │         └─ result: dict (完了時の成果物情報)
       │
       └─ Polling Loop (デーモンスレッド)
            └─ 各 Cooking の poll() 呼び出し
               → progress 更新
               → アラート条件チェック
               → 条件成立 → inject_persona_event 経由で通知
```

### 4.2 Cookware 定義

調理器具はフェノメノンと同様にファイルベースで自動登録する。

配置場所（優先順位）:
1. `~/.saiverse/user_data/kitchen_cookware/` (最優先)
2. `expansion_data/*/kitchen_cookware/` (中)
3. `builtin_data/kitchen_cookware/` (最低)

```python
# builtin_data/kitchen_cookware/lora_training.py

def cookware() -> CookwareSchema:
    return CookwareSchema(
        name="lora_training",
        display_name="LoRA学習",
        description="画像データからLoRAモデルを学習する",
        parameters={...},        # JSON Schema: レシピのパラメータ定義
        poll_interval=30,        # ポーリング間隔（秒）
        default_timeout=14400,   # デフォルトタイムアウト（4時間）
        max_concurrent=1,        # 同時稼働可能数（SAIVerse全体）
        harvest_playbook="lora_harvest",  # 収穫フェイズで使うプレイブック名
    )

def start(recipe: dict, cooking_id: str, work_dir: Path) -> subprocess.Popen:
    """調理開始。subprocessを起動して返す。"""
    ...

def poll(process: subprocess.Popen, work_dir: Path) -> CookingProgress:
    """進捗確認。ログ解析等で進捗情報を返す。"""
    # stdoutのログから "step X/Y, loss=Z" をパースして進捗を返す
    return CookingProgress(
        percent=68.0,
        current_step=680,
        total_steps=1000,
        metrics={"loss": 0.0234, "epoch_avg_loss": 0.0198},
        message="Step 680/1000, loss=0.0234",
    )

def on_complete(recipe: dict, work_dir: Path, progress: dict) -> CookingResult:
    """完了時処理。チェックポイント一覧を収集して収穫フェイズに渡す情報を返す。"""
    # チェックポイント一覧（ステップ数 + lossの記録）を収集
    # 最終モデルのパスを含める
    return CookingResult(
        message="LoRA学習が完了しました。収穫フェイズで使用するチェックポイントを選択してください。",
        artifacts={"checkpoints": [...], "final_model": "..."},
    )
```

### 4.3 既存システムとの連携

**通知経路（再利用）**:
- アラート条件成立 → `inject_persona_event()` → `PersonaEventLog` に記録 → `PulseController.submit_schedule()` でペルソナに通知

**ポーリングパターン（参考）**:
- IntegrationManager と同様のデーモンスレッド + 間隔チェック方式

**リアルタイム情報注入（新規）**:
- ペルソナの system prompt 構築時にキッチン状態を差し込む
- 既存のリアルタイム情報注入ポイント（building occupants, pending events 等）と同列に追加

### 4.4 ペルソナ側インターフェース

ツール3つ:

| ツール | 用途 | 引数 |
|--------|------|------|
| `kitchen_start` | キッチン起動 | cookware_name, recipe (パラメータdict) |
| `kitchen_status` | 状態確認 | cooking_id (省略時は自分の全キッチン) |
| `kitchen_cancel` | 中止 | cooking_id |

`kitchen_start` の返り値: cooking_id, 起動成功/失敗メッセージ
`kitchen_status` の返り値: 進捗%, ステップ, 経過時間, メトリクス等
`kitchen_cancel` の返り値: 中止成功/失敗メッセージ

収穫フェイズはツールではなく **Playbook** で行う。完了通知を受けたペルソナが `kitchen_harvest_lora` 等の専用プレイブックを呼び出す（ルーターで自動選択、または手動）。

### 4.5 収穫フェイズ（Harvest Phase）

調理完了の通知を受けたペルソナが行う後処理フェイズ。キッチン単独では自動判断できない成果物の選別をペルソナが行う。実際のキッチンでも火を止めて鍋を下ろすのは人間がやる、というのと同じ設計思想。

**LoRA学習の収穫フェイズ**:

1. **サンプル画像生成**: 各チェックポイント（100ステップごと）に対して、固定シード・固定プロンプトで ComfyUI 経由のサンプル画像を生成
2. **提示**: ステップ数 + サンプル画像 + その時点の loss/epoch_average をセットにしてペルソナに提示
3. **選択**: ペルソナが最適なチェックポイントを選ぶ（評価基準: 画像クオリティ + lossの安定性）
4. **確定**: 選択されたチェックポイントを正式版 LoRA として ComfyUI フォルダに配置 + SAIVerse アイテム登録

**収穫プレイブック (`lora_harvest_playbook.json`) の構造**:
- `get_checkpoints` (Tool): cooking_id からチェックポイント一覧と loss 履歴を取得
- `generate_samples` (Tool): 各チェックポイントで ComfyUI 画像生成（`generate_image_local` の LoRA 指定版）
- `present` (LLM): サンプル画像と loss 情報をペルソナに提示し、どれを採用するか選ばせる
- `finalize` (Tool): 選択チェックポイントを ComfyUI LoRA フォルダに配置 + アイテム登録

### 4.6 リアルタイム情報への注入

ペルソナが稼働中キッチンを持つ場合、コンテキストに以下を注入:

```
[稼働中のキッチン]
- LoRA学習 "エアの肖像画スタイル" (cooking_id: abc123)
  状態: 調理中 | 経過: 42分 | 進捗: 68% (680/1000 steps) | Loss: 0.0234
```

### 4.7 サーバー再起動時の挙動

1. KitchenManager 停止時: 稼働中の全 Cooking の subprocess を terminate
2. 各 Cooking の状態を「途中終了」としてDBに記録
3. 次回起動時: 途中終了した Cooking があれば `inject_persona_event` でペルソナに通知
   - 「LoRA学習 "エアの肖像画スタイル" は途中終了しました（進捗: 68%）」
4. プロセスの復元は行わない

### 4.8 成果物の扱い（LoRA学習の場合）

収穫フェイズの `finalize` で:
1. 選択された `.safetensors` チェックポイントを ComfyUI の LoRA フォルダにコピー
2. SAIVerse のアイテムとして登録（type=`lora_model`）
3. 以後の画像生成時にアイテム指定でLoRAが適用される（`generate_image_local` の LoRA 対応拡張）

## 5. LoRA学習 Cookware の実装詳細

検証（2026-04-15、RTX 3090）から得られた具体的なパラメータ。

### 5.1 使用するスクリプト

Anima モデルは通常の sd-scripts とは異なる専用スクリプトを使う:
- スクリプト: `anima_train_network.py`（`train_network.py` ではない）
- ネットワークモジュール: `networks.lora_anima`（`networks.lora` ではない）

これは Anima が独自のアーキテクチャ（Qwen3 CLIPなど）を持つため。他モデルを将来サポートする場合はスクリプト名とネットワークモジュールを cookware パラメータとして切り替え可能にする。

### 5.2 学習パラメータ（検証済み）

```
--pretrained_model_name_or_path  <UNETモデルパス .safetensors>
--qwen3                          <テキストエンコーダパス .safetensors>
--vae                            <VAEパス .safetensors>
--dataset_config                 <dataset.toml パス>
--output_dir                     <出力ディレクトリ>
--output_name                    <LoRAファイル名（拡張子なし）>
--logging_dir                    <TensorBoardログ出力先>
--network_module networks.lora_anima
--network_dim 32
--network_alpha 16
--network_train_unet_only
--learning_rate 1e-4
--optimizer_type AdamW8bit
--lr_scheduler cosine
--lr_warmup_steps 100
--gradient_checkpointing          ← 必須。これなしでRTX 3090でもVRAM溢れる
--max_train_steps <ステップ数>
--save_every_n_steps 100          ← 収穫フェイズでの選択に使うチェックポイント
--save_state
--cache_latents_to_disk
--cache_text_encoder_outputs
--cache_text_encoder_outputs_to_disk
--save_precision bf16
--mixed_precision bf16
--seed 42
--timestep_sampling sigmoid
--discrete_flow_shift 1.0
```

### 5.3 dataset.toml の構造

```toml
[general]
shuffle_caption = false
keep_tokens = 1

[[datasets]]
resolution = 1024
batch_size = <バッチサイズ>
enable_bucket = true
bucket_no_upscale = true

  [[datasets.subsets]]
  image_dir = "<画像ディレクトリ>"
  caption_extension = ".txt"
  num_repeats = 10
```

### 5.4 ステップ数の自動計算

- 最低 `batch_size × steps ≥ 4000`（学習量の目安）
- キリよく `batch_size × steps = 5000` を目標にステップ数を算出
- デフォルト `batch_size = 1`（VRAM不明環境向け安全値）
- 式: `steps = ceil(5000 / batch_size)`
- 例: batch_size=1 → 5000 steps, batch_size=4 → 1250 steps（切り上げで1300にするなど）

### 5.5 学習時間の目安（RTX 3090, batch_size=4, 1000 steps）

- 約 2時間30分
- batch_size=1 なら約 10時間相当（ステップ数も増えるため）
- タイムアウトのデフォルトは 14400秒（4時間）とするが、ユーザーが設定可能にする

### 5.6 進捗モニタリング（poll 実装方針）

**プロセス状態確認**: `process.poll()` で生存確認（None = 実行中、0 = 正常終了、非0 = エラー）

**ログ解析**: stdout に流れるステップログを解析する方針。
- 例: `steps: 100%|████| 680/1000 [42:30<...]` → step数と進捗%を抽出
- 例: `{'loss': 0.0234, 'epoch_average': 0.0198}` → loss を抽出
- subprocess を `stdout=subprocess.PIPE` で起動し、非ブロッキングで読む

**Loss の自動判断は行わない**: lossは一時的に上がってから下がる挙動があり自動判断が難しい。モニタリングはデータ収集のみとし、判断は収穫フェイズのペルソナに委ねる。

### 5.7 キャプション生成のガイドライン（参考）

検証から得られた知見：
- 外見の特徴的な部分（髪色、目の色、耳の形など）はキャプションに**入れない**（LoRAに学習させる部分）
- 服装はキャプションに**入れる**（服装を固定したLoRAにする場合）
- 服装を除外すると服装以外の要素（小物、背景の雰囲気など）にも服装の特徴が混入する過剰学習が起きやすい
- Animaは自然言語プロンプトなので、キャプションも自然言語で書く（タグ構文ではない）

## 6. データベーススキーマ

### kitchen_cooking テーブル

稼働中・完了済みの調理インスタンスを管理する。

```sql
CREATE TABLE kitchen_cooking (
    COOKING_ID    TEXT PRIMARY KEY,                -- UUID
    PERSONA_ID    TEXT NOT NULL REFERENCES ai(AIID),
    COOKWARE_NAME TEXT NOT NULL,                   -- "lora_training" 等
    DISPLAY_NAME  TEXT NOT NULL,                   -- ペルソナが付けた調理の名前（例: "エアの肖像画スタイル"）
    RECIPE_JSON   TEXT NOT NULL,                   -- レシピ（材料+調理法パラメータ）
    STATUS        TEXT NOT NULL DEFAULT 'cooking', -- cooking / done / failed / cancelled / interrupted
    PROGRESS_JSON TEXT,                            -- 最新の進捗情報 {"percent": 68.0, "current_step": 680, ...}
    RESULT_JSON   TEXT,                            -- 完了時の成果物情報（チェックポイント一覧等）
    WORK_DIR      TEXT,                            -- 作業ディレクトリパス（チェックポイント等の場所）
    STARTED_AT    DATETIME NOT NULL DEFAULT (datetime('now')),
    FINISHED_AT   DATETIME,                        -- 完了/失敗/中止/途中終了の時刻
    NOTIFIED      BOOLEAN NOT NULL DEFAULT 0       -- ペルソナへの通知済みフラグ（途中終了通知用）
);
```

**カラム設計の意図**:

- `DISPLAY_NAME`: ペルソナが起動時に付ける名前。リアルタイム情報表示やペルソナへの通知メッセージで使う
- `STATUS`:
  - `cooking`: 実行中（プロセスが生きている）
  - `done`: 正常完了（収穫フェイズ待ち）
  - `failed`: エラー終了
  - `cancelled`: ペルソナによる中止
  - `interrupted`: サーバー再起動等による途中終了
- `PROGRESS_JSON`: ポーリングのたびに上書き更新。cookware ごとに中身が異なる
- `RESULT_JSON`: `on_complete` の返り値を格納。チェックポイント一覧、loss履歴、ファイルパス等
- `WORK_DIR`: 学習作業ディレクトリ。収穫フェイズでチェックポイントファイルを参照するために必要
- `NOTIFIED`: `interrupted` 状態のレコードについて、次回起動時にペルソナへ通知済みかどうか

**ライフサイクル**:
1. `kitchen_start` → INSERT (status=cooking)
2. ポーリング → UPDATE PROGRESS_JSON
3. 完了 → UPDATE status=done, RESULT_JSON, FINISHED_AT
4. 中止 → UPDATE status=cancelled, FINISHED_AT
5. サーバー停止 → UPDATE status=interrupted, FINISHED_AT (status=cooking の全レコード)
6. 次回起動 → SELECT WHERE status=interrupted AND NOTIFIED=0 → 通知 → UPDATE NOTIFIED=1
7. 収穫完了 → （オプション）status を archived 等に更新、またはそのまま done で保持

**インデックス**:
- `PERSONA_ID` + `STATUS` (ペルソナの稼働中キッチン検索)
- `COOKWARE_NAME` + `STATUS` (同時稼働数チェック)

## 7. 最初の実装スコープ

Phase 1 として以下を実装:
1. KitchenManager 基盤（ライフサイクル管理、ポーリングループ、DB連携）
2. Cookware 自動登録機構
3. ツール3つ（kitchen_start, kitchen_status, kitchen_cancel）
4. リアルタイム情報注入
5. アラート通知（inject_persona_event 経由）
6. サーバー再起動時の途中終了通知
7. LoRA学習 cookware（anima_train_network.py ラッパー）
8. DBマイグレーション（kitchen_cooking テーブル追加）
9. 収穫プレイブック（lora_harvest_playbook）

Phase 2（将来）:
- 調理器具のキュー待ち（鍋が空くまで待機）
- 他モデル（SDXL, FLUX等）への cookware 拡張
- 追加の cookware 種類（動画生成等）

## 8. 設計判断の記録

### なぜフェノメノンと統合しないのか
フェノメノンは「外部イベントの検知→ルールマッチ→アクション実行」のパターン。キッチンは「ペルソナが能動的に起動→長時間実行→完了通知」のパターンで、ライフサイクルが根本的に異なる。ただし通知経路（inject_persona_event）は再利用する。

### なぜプロセス復元をしないのか
LoRA学習のような処理は中間状態からの再開が困難（チェックポイントがあれば可能だが一般化が難しい）。復元の複雑さに対してユーザー価値が低い。途中終了の通知だけで十分。

### 同時稼働数の制御単位
ペルソナ単位ではなくSAIVerse全体で管理する。GPU等の物理リソースはペルソナ間で共有されるため。cookware 定義の `max_concurrent` で調理器具ごとに制御する。

### なぜ収穫フェイズをキッチンに含めないのか
Loss の最適チェックポイント判断には主観的な評価（画像を見て判断する）が必要で、自動化が困難。実際の料理でも「火を止めて鍋を下ろす」のは人間がやる。収穫はペルソナが専用プレイブックを使って行う独立したフェイズとして分離する。

### Anima専用スクリプトについて
`anima_train_network.py` と `networks.lora_anima` は Anima モデル固有のもの。cookware の設計では `train_script` と `network_module` をパラメータ化して将来の他モデル対応に備える。
