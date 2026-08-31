# Intent: ユーザー定義複合アクション (User-Defined Composite Actions)

**ステータス**: v0.1 ドラフト (2026-06-24)

## これは何か

ユーザーがアドオン管理 UI 上で **複数の MCP ツール呼び出しを組み合わせた「動作」を定義し、ペルソナの spell として動的に登録する** 仕組み。

定義例:
- 「うなずき」= `move_head(tilt: 60°)` → 300ms 待機 → `move_head(tilt: 45°)` (首の上下)
- 「前進」= `[同時] i2c_write(ch0: 180°), i2c_write(ch1: 0°)` → 500ms → `[同時] i2c_write(ch0: 90°), i2c_write(ch1: 90°)` (車輪サーボ2個を同時制御)
- 「驚き」= `[同時] set_avatar(face: surprised), move_head(pan: -15°)` → 200ms → `move_head(pan: 15°)` → 200ms → `move_head(pan: 0°)` (表情+首振り)

## これは何でないか

- **TTS 同期型の表現マーカーシステムではない**: 発話の文字位置に同期した `/emote` マーカー方式は `embodied_expression.md` が扱う。本書のアクションは spell として独立に発火する
- **MCP ツールそのものの設計ではない**: `i2c_write` や `move_head` の MCP ツール定義は stackchan-mcp upstream / fork が管轄。本書はそれらを組み合わせる上位レイヤー
- **プログラミング環境ではない**: 条件分岐・ループ・変数は提供しない。固定シーケンスの定義と実行に絞る

## なぜ必要か

### 問題: ハードウェア構成がユーザーごとに違う

8ch サーボドライバ (M5Stack U165) のようなモジュールを接続した場合、各チャンネルに何が繋がっているかはユーザーの物理配線次第:

| ユーザー | ch0 | ch1 | ch2 | ch3 |
|---|---|---|---|---|
| A さん | 右腕 | 左腕 | 右手首 | 左手首 |
| B さん | 左車輪 | 右車輪 | (未使用) | (未使用) |
| C さん | 頭回転 | 首傾き | 腕 | (未使用) |

ペルソナには「腕を上げる」「前進する」のような意味のある動作名で開示する必要があるが、その中身 (どのチャンネルをどう動かすか) はユーザーが決める。開発者がハードコードできない。

### 問題: 単一ツール呼び出しでは複合動作を表現できない

「前進」は左右車輪を同時に回す必要がある。「お辞儀」は首を下げてから戻す順次動作。「驚き」は表情変更と首振りの同時実行。これらはいずれも単一の MCP ツール呼び出しでは実現できない。

LLM に毎回「左車輪と右車輪を同時に回して」と推論させるのは:
1. レイテンシが大きい (LLM コール + 引数生成)
2. 再現性が低い (LLM が毎回微妙に違う角度を出す)
3. プロンプトに全チャンネルの配線情報を注入する必要がある

### 問題: サーボに限らない汎用性

既存の MCP ツールの組み合わせだけでも有用なジェスチャーが作れる:
- `move_head` + `set_avatar` = 表情付き首振り
- `set_all_leds` + `set_avatar` = LED 演出付き表情変化
- `move_head` (複数回) = うなずき、首かしげ

サーボドライバ対応を待たずに、これらを「ユーザーが名前付きで登録 → spell として使える」にする価値がある。

## 設計

### (A) アクション定義スキーマ

```json
{
  "id": "nod",
  "display_name": "うなずき",
  "description": "首を上下に動かしてうなずく",
  "steps": [
    {
      "type": "parallel",
      "calls": [
        {"tool": "move_head", "args": {"tilt": 60, "speed_preset": "fast"}}
      ]
    },
    {
      "type": "wait",
      "duration_ms": 300
    },
    {
      "type": "parallel",
      "calls": [
        {"tool": "move_head", "args": {"tilt": 45, "speed_preset": "normal"}}
      ]
    }
  ]
}
```

```json
{
  "id": "move_forward",
  "display_name": "前進",
  "description": "車輪を回して前に進む",
  "parameters": {
    "duration_ms": {
      "type": "number",
      "default": 500,
      "min": 100,
      "max": 3000,
      "description": "前進する時間 (ms)"
    }
  },
  "steps": [
    {
      "type": "parallel",
      "calls": [
        {"tool": "i2c_write", "args": {"addr": 37, "bytes": [0, 180]}},
        {"tool": "i2c_write", "args": {"addr": 37, "bytes": [1, 0]}}
      ]
    },
    {
      "type": "wait",
      "duration_ms": "${duration_ms}"
    },
    {
      "type": "parallel",
      "calls": [
        {"tool": "i2c_write", "args": {"addr": 37, "bytes": [0, 90]}},
        {"tool": "i2c_write", "args": {"addr": 37, "bytes": [1, 90]}}
      ]
    }
  ]
}
```

#### ステップの種類

| type | 意味 | フィールド |
|---|---|---|
| `parallel` | calls 内の全ツールを同時に呼び出す | `calls: [{tool, args}]` |
| `wait` | 指定時間待機する | `duration_ms: number` |

`parallel` の calls が1件だけなら実質的に単一呼び出し。別途 `sequential` タイプは設けない (順次実行は `parallel(1件)` → `wait` → `parallel(1件)` で表現する)。

#### パラメータ化

`parameters` フィールドでアクションの引数を定義できる。steps 内の `args` 値や `wait` の `duration_ms` で `"${param_name}"` プレースホルダとして参照する。ペルソナが spell 呼び出し時に指定できるようになる。

プレースホルダの解決は 3 通り (`saiverse/composite_actions.py:_resolve_placeholder`):

- **値まるごと** `"${speed}"` → そのパラメータの型付き値 (number / string / boolean をそのまま保持)
- **式の一部** `"50*${speed}"` / `"${seconds}*1000"` → `${name}` を数値で差し込んで算術式として評価 (`+ - * / // % **` と括弧・単項符号。整数になる場合は int)。`duration_ms` を「秒 × 1000」で指定したい等のスケール変換に使う
- **文字列補間** `"ch${index}"` のように算術評価できないものは `${name}` を文字列値で置換した文字列を返す

式評価は AST ベースで算術ノードしか通さない (`_safe_eval_arith`)。`Name` / `Call` / 属性アクセスは評価対象外で、文字列パラメータは `repr()` でクオートして差し込むため、任意コード実行にはならない。

#### 安全性

- `calls[].tool` に指定できるのは、同じアドオンの `mcp_servers.json` に登録済みの MCP ツール名、または同じアドオン内の native tool 名に限定する。外部ツールの任意実行は許可しない
- `wait` の `duration_ms` に上限を設ける (例: 10000ms)。無限待機や極端に長い待機は許可しない
- steps 数に上限を設ける (例: 20 ステップ)

### (B) 保存と読み込み

#### 保存先

`~/.saiverse/user_data/addon_data/<addon_id>/actions/<action_id>.json`

addon の永続データ規約 (`addon_catalog_management.md`) に準拠。アドオンごとにアクションが分離される。

#### 読み込みタイミング

アドオン有効化時 + MCP サーバー接続確立時に `actions/` ディレクトリをスキャンし、全アクション定義を読み込む。アクション追加・編集後は再読み込みを行う (API エンドポイント or ファイル監視)。

### (C) Spell として動的登録

読み込んだ各アクション定義を `ToolSchema` として `TOOL_REGISTRY` に登録する:

- `name`: `<addon_prefix>_action_<action_id>` (例: `stackchan_action_nod`)
- `spell`: True
- `spell_display_name`: アクション定義の `display_name`
- `spell_visible`: True
- `building_ids`: アドオンの vessel_building_id 設定を継承

実行時は `steps` を順に処理する executor が走る。各 `parallel` ステップ内の `calls` は `asyncio.gather` で同時実行し、`wait` ステップは `asyncio.sleep` で待機する。

### (D) アドオン管理 UI

既存の `AddonManagerModal` にアクション定義タブを追加する。

#### アクション一覧画面
- アドオンに紐づくアクション一覧 (display_name + description)
- 新規作成 / 編集 / 削除 / テスト実行ボタン

#### アクション編集画面
- **基本情報**: id (英数スネークケース), display_name (日本語 OK), description
- **パラメータ定義**: 呼び出し時の引数の追加・編集
- **ステップエディタ**: ステップの追加・削除・並べ替え (ドラッグ or 上下ボタン)
  - parallel ステップ: ツール選択ドロップダウン (アドオン内の利用可能ツール一覧) + args の JSON エディタ or フォーム
  - wait ステップ: duration_ms の数値入力
- **テスト実行**: 保存せずにその場で実行して動作確認できるボタン
- **プレビュー**: ステップのタイムライン的な視覚表示 (「同時に A と B → 300ms 待ち → C」)

#### UI の制約

ステップエディタは最初はシンプルにする:
- ツール引数は JSON テキストエディタ (フォーム化は Phase 2 以降)
- タイムラインプレビューはテキスト表現 (ビジュアルタイムラインは Phase 2 以降)

## embodied_expression.md との関係

`embodied_expression.md` は LLM の発話テキスト中に `/emote` マーカーを埋め込み、TTS 再生タイミングに同期してプリセットを発火する仕組み。本書のアクションは独立した spell として呼ばれる。

将来的に統合できる面:
- embodied_expression のプリセット定義 (`actions.stackchan.servo_sequence` 等) の実行エンジンとして、本書のステップ executor を共用できる可能性がある
- embodied_expression の「プリセット」を本書の「アクション定義」フォーマットに統一し、`/emote` マーカーはアクション ID を指定する形に収束させることで、ユーザーが UI で作ったジェスチャーを `/emote` マーカーでも spell でも使えるようになる

ただし Phase 1 では依存を持たない。本書単独で動作する。

## stackchan_extension_modules.md との関係

拡張モジュール対応のレベル2 (Python ドライバを addon に書く) が I2C 汎用口を前提としている。本書のアクション定義はその上位レイヤーとして「I2C 生コマンド列をユーザーフレンドリーな名前付き動作にまとめる」役割を果たす。

例: 8ch サーボドライバの ch0 に右腕が繋がっている場合
1. `i2c_write(addr=0x25, bytes=[0x00, angle])` が生の MCP ツール呼び出し
2. ユーザーがアクション「右腕を上げる」を定義 → `i2c_write` を `angle=90°` で呼ぶステップ
3. ペルソナは「右腕を上げる」spell を使う

### サーボドライバプロトコルの抽象化

8ch サーボドライバ (STM32F030F4, I2C addr 0x25) のレジスタプロトコルを直接 `i2c_write` の bytes に書くのはユーザーに厳しい。Phase 2 以降で:
- サーボドライバ用の native tool (例: `servo_set_angle(channel, angle)`) をアドオンに追加
- アクション定義のステップで `i2c_write` の代わりにこの native tool を使えるようにする
- UI でのステップ定義が「ch0 を 90° に」程度のフォーム入力で済むようになる

## 不変条件

1. **アクション定義はアドオンに帰属する**: アドオン間でアクションを共有しない。削除時はアドオンごと消える
2. **アクション内で呼べるツールは同一アドオン内に限定する**: 安全性の担保。別アドオンの MCP ツールや任意の外部ツールは呼べない
3. **アクション名はユーザーが付ける**: 開発者がハードコードした名前ではなく、ユーザーの物理配線に合った名前を自由に付けられる
4. **steps の実行順序は定義順が保証される**: parallel 内の同時性と steps 間の順序性が明確
5. **テスト実行は必ず提供する**: ユーザーが保存前に動作確認できる手段がなければ、サーボの配線ミスで物理的な事故になりうる

## オープン課題

- ~~**サーボドライバの I2C レジスタプロトコル**: M5Stack 8ch サーボドライバ (U165) の角度設定コマンド体系の詳細調査が必要。STM32F030F4 のレジスタマップを確認する~~ → **調査・実装済 (2026-06-25)**。公式 lib `m5stack/M5Unit-8Servo` (`M5_UNIT_8SERVO.h/.cpp`) を真として移植。I2C addr `0x25`、MODE レジスタ `0x00 + ch` (サーボ駆動は `SERVO_CTL_MODE`=3)、角度レジスタ `0x50 + ch` (0〜180°、1byte)、パルス幅 `0x60 + ch*2` (500〜2500µs、2byte LE)、LED `0x70 + ch*3` (RGB)。angle→pulse は内部ファーム `Steer.c` で `500 + angle*2000/180` (= 0°/500µs, 90°/1500µs, 180°/2500µs) と裏取り済み。角を書く前に MODE を servo にする必要がある点が肝。native tool 実装は `expansion_data/saiverse-stackchan-addon/tools/units/servo8.py`。180° 位置決めサーボ用 `servo8_set_angle(channel, angle)` と、360° 連続回転サーボ (車輪) 用 `servo8_set_speed(channel, speed)` (-100〜100、0=停止、パルス幅 1500±500µs にマップ) の 2 ツール。連続回転の「○秒前進」は複合アクション (set_speed→wait→set_speed 0) で表現。生 `i2c_write` をラップ (env3.py と同構図)。実機検証 (i2c_scan で 0x25 検出 / 角度・速度が想定どおり動くか / 連続サーボの中立 1500µs で確実に停止するか / MODE 毎回 assert で twitch が出ないか) は未実施
- **アクション定義のインポート/エクスポート**: ユーザー間でアクション定義を共有する仕組み (JSON ファイルのやりとり)
- ~~**ツール引数のフォーム化**: JSON テキストエディタではなく、ツールの inputSchema から動的にフォームを生成する (Phase 2)~~ → **実装済 (2026-06-26)**。`GET /api/addon/<addon>/actions/tool-schemas` が native / MCP 両ツールの inputSchema を返し、`ActionsPanel` がキー単位の入力欄を生成 (enum→select、boolean→select、数値→`${引数}`・式可のテキスト入力)。スキーマ無しや込み入った値用に「JSON で編集」エスケープハッチを併設。引数 (`parameters`) 編集 UI も同時に実装
- **embodied_expression との統合タイミング**: 本書のアクション executor が安定したら、embodied_expression のプリセット実行エンジンとの共用を検討
- **エラーハンドリング**: ステップ途中で MCP ツール呼び出しが失敗した場合の挙動 (中断 / 続行 / ロールバック)。サーボの場合「途中で止まる」が最も安全だが、車輪の場合「元に戻す」が必要かもしれない

## 段階実装プラン

### Phase 1: 首振りジェスチャーで仕組み検証

サーボドライバなしで、既存 MCP ツール (`move_head` + `set_avatar`) の組み合わせでジェスチャーを作る。

- アクション定義スキーマの実装 (JSON 読み込み + バリデーション)
- ステップ executor の実装 (parallel + wait)
- spell 動的登録の実装
- アドオン管理 UI にアクション定義タブを追加
- builtin サンプル: うなずき、首かしげ、驚き (表情+首振り)

### Phase 2: サーボドライバ対応 + UI 強化

- ~~8ch サーボドライバの I2C プロトコル調査・native tool 化~~ → 実装済 (2026-06-25)、実機検証済 (CH0/1=180°, CH2/3=360° 車輪)
- ~~アクション定義 UI のフォーム化 (ツール inputSchema からフォーム動的生成)~~ → 実装済 (2026-06-26)
- タイムラインプレビューの視覚化 (未着手)
- ~~パラメータ化の完全サポート~~ → 実装済 (2026-06-26)。`${param}` まるごと置換に加え、`50*${speed}` / `${seconds}*1000` の算術式・文字列補間に対応 (`_resolve_placeholder`)

### Phase 3: embodied_expression 連携 (前提: embodied_expression 自体の実装完了後)

embodied_expression (TTS 同期型 `/emote` マーカー) は 2026-06-24 時点で未実装。本書の Phase 1-2 は embodied_expression に依存しない。Phase 3 は embodied_expression の実装が完了した後の統合作業:

- 本書のアクション executor を embodied_expression のプリセット実行エンジンに統合
- ユーザーが UI で作ったアクションを `/emote` マーカーでも使えるように
- 実装順序: 本書 Phase 1-2 → (embodied_expression 実装) → 本書 Phase 3

## 関連 doc

- `docs/intent/embodied_expression.md` — TTS 同期型の表現出力。将来的に executor を共用する候補
- `docs/intent/stackchan_extension_modules.md` — I2C 拡張モジュール。本書のアクション定義はレベル2のユーザー体験の上位レイヤー
- `docs/intent/stackchan_vessel.md` — stackchan vessel 全体設計。本書はその中の MCP ツール活用パターンの拡張
- `docs/intent/embodied_active_input.md` — BLE ボタン入力。本書のアクションを入力トリガーの発火先にできる可能性
- `docs/intent/addon_catalog_management.md` — アドオン永続データ規約。アクション定義の保存パスに準拠
