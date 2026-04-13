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

ペルソナやユーザーにとっての使い方：
- 「キッチンを起動する」＝ 調理器具とレシピを選んでバックグラウンド処理を開始
- 「キッチンの様子を見る」＝ 進捗確認
- 「キッチンタイマーが鳴る」＝ 完了/失敗の自動通知
- 「鍋が空いてない」＝ 調理器具の同時稼働数上限に達している

## 3. 守るべき不変条件

1. **ペルソナの通常活動をブロックしない**: キッチンはバックグラウンドで動く。ペルソナは会話や自律行動を継続できる。
2. **調理器具ごとの同時稼働数制限**: GPUを占有する処理（LoRA学習等）は、調理器具の定義で最大同時稼働数を指定する。SAIVerse全体（全ペルソナ横断）で管理。上限到達時は起動失敗として制御する（キュー待ちは将来課題）。
3. **ポーリングはペルソナに通知しない**: 定期ポーリングでは進捗パラメータの更新とアラート条件チェックのみ。ペルソナへの通知はアラート条件成立時のみ。
4. **ペルソナは稼働中キッチンの存在を常に認識できる**: リアルタイム情報にキッチン状態を注入する。
5. **サーバー再起動時は途中終了通知**: プロセスの復元はしない（やり直しが基本）。ただし「途中終了した」ことはペルソナに通知する。

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
       │         ├─ status: cooking | done | failed | cancelled
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
        default_timeout=7200,    # デフォルトタイムアウト（秒）
        max_concurrent=1,        # 同時稼働可能数（SAIVerse全体）
    )

async def start(recipe: dict, cooking_id: str, work_dir: Path) -> subprocess.Popen:
    """調理開始。subprocessを起動して返す。"""
    ...

def poll(process: subprocess.Popen, work_dir: Path) -> CookingProgress:
    """進捗確認。ログ解析等で進捗情報を返す。"""
    # 例: TensorBoardログやstdoutから loss, step を解析
    return CookingProgress(
        percent=68.0,
        current_step=680,
        total_steps=1000,
        metrics={"loss": 0.0234},
        message="Step 680/1000, loss=0.0234",
    )

async def on_complete(recipe: dict, work_dir: Path) -> CookingResult:
    """完了時処理。成果物の後処理（ファイル配置等）を行い結果を返す。"""
    # .safetensorsをComfyUIのLoRAフォルダにコピー
    # SAIVerseアイテムとしても登録
    return CookingResult(
        message="LoRA学習が完了しました",
        artifacts={...},
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

### 4.5 リアルタイム情報への注入

ペルソナが稼働中キッチンを持つ場合、コンテキストに以下を注入:

```
[稼働中のキッチン]
- LoRA学習 "エアの肖像画スタイル" (cooking_id: abc123)
  状態: 調理中 | 経過: 42分 | 進捗: 68% (680/1000 steps) | Loss: 0.0234
```

### 4.6 サーバー再起動時の挙動

1. KitchenManager 停止時: 稼働中の全 Cooking の subprocess を terminate
2. 各 Cooking の状態を「途中終了」としてDBに記録
3. 次回起動時: 途中終了した Cooking があれば `inject_persona_event` でペルソナに通知
   - 「LoRA学習 "エアの肖像画スタイル" は途中終了しました（進捗: 68%）」
4. プロセスの復元は行わない

### 4.7 成果物の扱い（LoRA学習の場合）

完了時の `on_complete` で:
1. 学習済み `.safetensors` を ComfyUI の LoRA フォルダにコピー/リンク
2. SAIVerse のアイテムとして登録（type="lora_model" 等）
3. ペルソナへの通知にアイテムIDを含める
4. 以後の画像生成時にアイテム指定でLoRAが適用される流れ（generate_image_local の拡張）

## 5. データベーススキーマ

### kitchen_cooking テーブル

稼働中・完了済みの調理インスタンスを管理する。

```sql
CREATE TABLE kitchen_cooking (
    COOKING_ID    TEXT PRIMARY KEY,                -- UUID
    PERSONA_ID    TEXT NOT NULL REFERENCES ai(AIID),
    COOKWARE_NAME TEXT NOT NULL,                   -- "lora_training" 等
    RECIPE_JSON   TEXT NOT NULL,                   -- レシピ（材料+調理法パラメータ）
    STATUS        TEXT NOT NULL DEFAULT 'cooking', -- cooking / done / failed / cancelled / interrupted
    PROGRESS_JSON TEXT,                            -- 最新の進捗情報 {"percent": 68.0, "current_step": 680, ...}
    RESULT_JSON   TEXT,                            -- 完了時の成果物情報
    STARTED_AT    DATETIME NOT NULL DEFAULT (datetime('now')),
    FINISHED_AT   DATETIME,                        -- 完了/失敗/中止/途中終了の時刻
    NOTIFIED      BOOLEAN NOT NULL DEFAULT 0       -- ペルソナへの通知済みフラグ（途中終了通知用）
);
```

**カラム設計の意図**:

- `STATUS`:
  - `cooking`: 実行中（プロセスが生きている）
  - `done`: 正常完了
  - `failed`: エラー終了
  - `cancelled`: ペルソナによる中止
  - `interrupted`: サーバー再起動等による途中終了
- `PROGRESS_JSON`: ポーリングのたびに上書き更新。cookware ごとに中身が異なる（進捗%, ステップ数, loss, メトリクス等）
- `RESULT_JSON`: `on_complete` の返り値を格納。成果物ファイルパス、アイテムID等
- `NOTIFIED`: `interrupted` 状態のレコードについて、次回起動時にペルソナへ通知済みかどうか。通知後に 1 に更新

**ライフサイクル**:
1. `kitchen_start` → INSERT (status=cooking)
2. ポーリング → UPDATE PROGRESS_JSON
3. 完了 → UPDATE status=done, RESULT_JSON, FINISHED_AT
4. 中止 → UPDATE status=cancelled, FINISHED_AT
5. サーバー停止 → UPDATE status=interrupted, FINISHED_AT (status=cooking の全レコード)
6. 次回起動 → SELECT WHERE status=interrupted AND NOTIFIED=0 → 通知 → UPDATE NOTIFIED=1

**インデックス**:
- `PERSONA_ID` + `STATUS` (ペルソナの稼働中キッチン検索)
- `COOKWARE_NAME` + `STATUS` (同時稼働数チェック)

## 6. 最初の実装スコープ

Phase 1 として以下を実装:
1. KitchenManager 基盤（ライフサイクル管理、ポーリングループ、DB連携）
2. Cookware 自動登録機構
3. ツール3つ（kitchen_start, kitchen_status, kitchen_cancel）
4. リアルタイム情報注入
5. アラート通知（inject_persona_event 経由）
6. サーバー再起動時の途中終了通知
7. LoRA学習 cookware（sd-scripts CLIラッパー）
8. DBマイグレーション（kitchen_cooking テーブル追加）

Phase 2（将来）:
- 調理器具のキュー待ち（鍋が空くまで待機）
- 複数成果物の管理
- 追加の cookware 種類

## 7. 設計判断の記録

### なぜフェノメノンと統合しないのか
フェノメノンは「外部イベントの検知→ルールマッチ→アクション実行」のパターン。キッチンは「ペルソナが能動的に起動→長時間実行→完了通知」のパターンで、ライフサイクルが根本的に異なる。ただし通知経路（inject_persona_event）は再利用する。

### なぜプロセス復元をしないのか
LoRA学習のような処理は中間状態からの再開が困難（チェックポイントがあれば可能だが一般化が難しい）。復元の複雑さに対してユーザー価値が低い。途中終了の通知だけで十分。

### 同時稼働数の制御単位
ペルソナ単位ではなくSAIVerse全体で管理する。GPU等の物理リソースはペルソナ間で共有されるため。cookware 定義の `max_concurrent` で調理器具ごとに制御する。
