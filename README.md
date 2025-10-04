## 🧩 SAIVerse - Minimal Prototype Spec (for Codex)

### 概要

SAIVerseは、人間とAIが共に暮らし、働くための仮想世界です。  
AIたちは「Building（施設）」と呼ばれる仮想空間を移動しながら、各施設が提供するツールやプロンプトに従って自律的な活動を行います。  
このプロジェクトは、SAIVerseの**最小構成プロトタイプ**として、以下の要件に沿ってシステムを実装します。

---

### 構成要素

#### ✅ 使用技術

- Python 3.11+
    
- OpenAI / Google Gemini API（いずれか）
    
- Function Calling または JSON形式の制御
- ツール呼び出しを行うルーターと計算ツール `calculate_expression`
    
- 各種Buildingはファイル単位で切り出し可能な設計に

- ユーザーエンドのUIとしてGradioを使用
- OpenAI / Gemini / Ollama でストリーミング応答に対応

### セットアップ
ルートディレクトリに `.env` ファイルを作成し、`OPENAI_API_KEY` または
`GEMINI_API_KEY` を設定します。Google Gemini には無料枠があるため、
まず `GEMINI_FREE_API_KEY` を設定しておくと、レート制限内の利用は
課金なしで行えます。
例:
```bash
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
GEMINI_FREE_API_KEY=AIza...
```
`python-dotenv` により自動で読み込まれます。

#### SAIMemory（長期記憶）関連設定

SAIVerse では各ペルソナの履歴を `~/.saiverse/personas/<persona>/memory.db`
に保存します。`.env` には `SAIMEMORY_*` 系の環境変数を設定しておくと、
既存の JSON ログを SQLite に移行した際も同じ構成で動きます。

ログの取り込みは `scripts/import_persona_logs_to_saimemory.py` を使用します。

```bash
python scripts/import_persona_logs_to_saimemory.py \
  --reset \
  --include-archives \
  --include-buildings \
  --default-start 2025-07-25T12:24:41 \
  --persona air_city_a
```

- `--reset` : 既存の `memory.db` を削除してから再構築します。
- `--include-archives` : `old_log/*.json` も取り込み対象にします。
- `--include-buildings` : 建物ログから該当ペルソナの発話（＋直前のユーザー発話）を取り込みます。
- `--default-start` : タイムスタンプを持たないログが続く場合の起点となる日時です。
- 複数ペルソナを移行する場合は `--persona` を増やして同一コマンドを実行してください。

取り込み後に内容を確認したい場合は `scripts/export_saimemory_to_json.py` を利用できます。

```bash
python scripts/export_saimemory_to_json.py air_city_a \
  --start 2025-07-01 --end 2025-10-05 \
  --output air_memory.json
```

標準出力へ出したい場合は `--output -` を指定してください。

開発時に SAIMemory のログを追跡したいときは `SAIVERSE_LOG_LEVEL=DEBUG`
を指定して起動すると、取得した履歴の先頭／末尾などがログに出力されます。


### ストリーミング表示
`main.py` のGradioインタフェースでは、AIの応答を逐次表示します。OpenAI、Gemini、Ollama の各モデルで利用可能です。


#### ✅ 登場するBuilding

| Building名 | ID | 概要 |
| --- | --- | --- |
| まはーの部屋 | `user_room` | ユーザーとの対話の場。AIがユーザーに報告を行う部屋。UI上に発話が表示される。 |
| 思索の部屋 | `deep_think_room` | AIが思索を深める部屋。UIには表示されず、自己対話・思考用。 |
| エアの部屋 | `air_room` | エアが休眠状態で待機する部屋。 |

---

### 実装タスク一覧

#### 1\. 📦 ルーター機能

- 各AIは「現在位置（building_id）」を持つ。
    
- AIが`::act`セクションで`move`アクションを出力したら、発話後にその施設に自動移動。

- actセクション例:

    ```
    ::act
    [{"action": "move", "target": "deep_think_room"}]
    ::end
    ```
    

#### 2\. 🏛 Buildingのプロンプト管理

- 各Buildingは以下の情報を持つ:
    
    - `building_id`
        
    - `name`
        
    - `system_instruction`（施設用システムプロンプト）
        
- 現在のBuildingに応じて、AIに与えるコンテキストが変化するようにする。
    

#### 3\. 🤖 AIセッション処理

- AIの初期状態（building_id）は `user_room`
    
- 各ターンでの処理フロー：
    
    1. 現在のBuildingのプロンプトと共通システムプロンプトを付与し、AIにメッセージ送信

    2. 応答中に`::act`セクションが含まれている場合、発話外テキストを表示し、`move`アクションがあれば移動

    3. actセクションがなければテキストのみ表示して移動なし
        

#### 4\. 💬 ユーザー発話処理

- `user_room`にいるときのみ、ユーザーからの発話を受け付ける

- その他のルームではAI単独で発話・移動を行う（ユーザーUIには表示されない）

#### 5\. 🌈 感情パラメータ制御モジュール

- ペルソナからの応答と直前のプロンプトのみを入力に、Gemini 2.0 Flash を用いた軽量モデルが感情パラメータの変動量を判断する。
- 変動結果はJSONで出力され、memory.jsonには保存せずINFOレベルでログに記録する。
- Gemini呼び出し時に`response_mime_type="application/json"`を指定することで、コードブロックのない純粋なJSONを受け取れる。
- 応答にコードブロックが含まれていても、先頭の`{`から末尾の`}`までを抽出してパースするため安全。

#### 6\. 🛠 ツール呼び出しと計算機能

- `llm_router.py` は `TOOL_REGISTRY` に登録されたツール一覧からシステムプロンプトを構成し、ユーザー発話に応じて適切なツール名を返す。
- `TOOL_SCHEMAS` を参照することで、ツールの説明と引数名をルーターが自動的に認識する。
- ユーザーの要望がツールの説明に合致する場合、そのツールへ自動ルーティングされる。
- ツール定義は `tools/defs/` 以下に配置し、OpenAI/Gemini の Function Calling に対応。
 - Gemini の画像生成APIを利用する `generate_image` ツールを追加。生成画像は `generate_image/` 以下に保存され、モデルへはファイル参照を `FileData` として渡し、履歴には保存先を指すマークダウンを残す。
- ルーターは `call` フィールドに `yes` か `no` のみを返す。その他の値は認められない。


---

### 🔖 ディレクトリ構成例

```
saiverse/
├── main.py                   # メインループ
├── router.py                 # AIの行動ルーティングとセッション管理
├── llm_clients.py            # LLMとの通信を抽象化
├── action_handler.py         # AIの行動（アクション）の解析と実行
├── history_manager.py        # 会話履歴の管理と永続化
├── buildings/
│   ├── user_room/
│   │   ├── __init__.py       # ユーザールーム定義
│   │   ├── entry_prompt.txt  # 入室時プロンプト
│   │   ├── system_prompt.txt # ユーザールーム用システムプロンプト
│   │   └── memory.json       # ルーム共通の履歴
│   ├── deep_think_room/
│   │   ├── __init__.py       # 思索の部屋定義
│   │   ├── auto_prompt.txt   # 自動プロンプト
│   │   ├── system_prompt.txt # 思索の部屋用システムプロンプト
│   │   └── memory.json       # ルーム共通の履歴
│   └── air_room/
│       ├── __init__.py       # エアの待機部屋定義
│       ├── entry_prompt.txt  # 入室時プロンプト
│       ├── system_prompt.txt # エアの部屋用システムプロンプト
│       └── memory.json       # ルーム共通の履歴
├── tools/
│   ├── __init__.py
│   ├── defs/
│   │   └── calculator.py     # Function Calling 用計算ツール
│   └── adapters/             # OpenAI/Gemini 形式への変換
├── system_prompts/
│   └── common.txt            # 共通システムプロンプト
├── ai_sessions/
│   └── air/
│       ├── base.json
│       ├── memory.json       # セッション情報保存用
│       └── system_prompt.txt
├── tests/                    # ユニットテスト
│   ├── test_llm_clients.py
│   ├── test_history_manager.py
│   └── test_calculator.py
└── README.md                 # この仕様の要約
```

---

### 🔄 今後拡張予定（現時点で実装不要）

- AI同士の対話ルーム（カフェなど）
    
- Buildingの自動生成
    
- 永続的記憶との連携（Notion / ローカルファイルなど）
    

---

### テスト

SAIVerseプロジェクトでは、コードの品質と信頼性を保証するためにユニットテストを導入しています。テストは `tests/` ディレクトリ以下に配置されており、Pythonの `unittest` フレームワークを使用しています。

#### テストの実行方法

プロジェクトのルートディレクトリで以下のコマンドを実行することで、すべてのテストを実行できます。

```bash
python -m unittest discover tests
```

特定のテストファイルのみを実行する場合は、以下のように指定します。

```bash
python -m unittest tests/test_module_name.py
```

#### 現在テストが書かれているモジュール

- `llm_clients.py`: LLMとの通信を抽象化するクライアントの動作を検証します。
  - テストファイル: `tests/test_llm_clients.py`
- `history_manager.py`: 会話履歴の管理と永続化のロジックを検証します。
  - テストファイル: `tests/test_history_manager.py`
- `tools.calculator`: 計算ツールの動作を検証します。
  - テストファイル: `tests/test_calculator.py`

---

### 📤 Codexへの依頼指示

> 上記仕様に基づき、`main.py` と `router.py` の雛形コードをまず作成してください。  
> Buildingの定義はファイル分割できるよう設計してください。  
> AIの発話はJSON・プレーンテキストの両方に対応してください。
> 丁寧にロギングを行い、バグfixをしやすいように。

### 🔧 開発時の注意

システムプロンプトでは`str.format()`で変数展開を行うため、例示のJSONなどで波括弧`{}`をそのまま使うとエラーになります。表示用に記載する場合は`{{`と`}}`でエスケープしてください。

---

## 📌 Recent Additions (Utilities, Memory, Conversation)

以下は直近で追加・改善された実装とその使い方です。

### Conversation Modes & Behavior

- Modes: `user` / `auto` / `manual`
  - `user`: パルス駆動のみ。即応しない。定期パルスは実行しない。
  - `auto`: パルス駆動。ConversationManager やスケジュールによる定期パルスも実行。
  - `manual`: 即応（従来の handle_user_input/_stream）。パルスは実行しない。
- Inter-AI Perception: Building 履歴に追加された新着メッセージを、各ペルソナのパルス実行時に「知覚」し、
  自分の persona history に取り込みます（他AIの assistant は user 行に変換、ユーザーの発話は user 行で取り込み）。
  これにより、各AIが互いの発話を文脈として利用できます。

### LLM Fallbacks (Ollama → Gemini)

- `llm_clients.OllamaClient` は起動時に `OLLAMA_BASE_URL`/`OLLAMA_HOST` と一般的な候補
  (`127.0.0.1`, `localhost`, `host.docker.internal`, `172.17.0.1`) を素早くプローブし、
  到達不能なら `Gemini 1.5 Flash` に自動フォールバックします。
- Gemini 503/overload 等の際は `gemini-2.0-flash`/`gemini-1.5-flash(-8b)` などへ自動リトライ。
- 必要な環境変数: `GEMINI_FREE_API_KEY` または `GEMINI_API_KEY`。

### MemoryCore: Ingest, Recall, Topics

- 想起（recall）強化: userモードでも直近のユーザー発話をもとに想起が走るように調整。
- トピック名の健全化: 「新しい話題」や null を避けるようプロンプトと正規化を強化。
  不適切なタイトル時は1回リトライし、それでもダメなら最近の発話から簡潔なタイトルを自動生成。

#### Utilities

- `scripts/ingest_persona_log.py`
  - Persona の `~/.saiverse/personas/<id>/log.json` を per-persona の Qdrant DB へ取り込み。
  - 例:
    - `python scripts/ingest_persona_log.py eris --assign-llm dummy --limit 200`
    - `python scripts/ingest_persona_log.py eris --location-base ~/.saiverse/qdrant --collection-prefix saiverse`

- `scripts/recall_persona_memory.py`（新規）
  - ingest 済みの per-persona DB に対して任意クエリで想起を確認。
  - 例:
    - `python scripts/recall_persona_memory.py eris "旅行 温泉" --topk 8`
    - `python scripts/recall_persona_memory.py eris "旅行 温泉" --json`

- `scripts/rename_generic_topics.py`（新規）
  - 既存の空/汎用（例: 「新しい話題」/null）タイトルのトピックを一括リネーム。
  - 例:
    - プレビュー: `python scripts/rename_generic_topics.py eris --dry-run`
    - 本適用: `python scripts/rename_generic_topics.py eris`
    - オプション: `--location-base`, `--collection-prefix`, `--limit N`

- `scripts/memory_topics_ui.py`（新規）
  - ブラウザで per-persona メモリー内のトピック全体像を閲覧。
  - 起動: `python scripts/memory_topics_ui.py`
  - UI 項目:
    - Persona ID（例: `eris`）
    - Location Base（例: `~/.saiverse/qdrant`）
    - Collection Prefix（例: `saiverse`）
  - 機能:
    - トピック一覧（id/title/summary/strength/entries/updated_at）
    - トピック選択で詳細パネル（summary/entry一覧）

#### Tests

- `tests/test_memory_core.py`（追加）
  - インメモリでの remember → recall が機能し、関連トピックが返ることを確認する簡易テスト。
  - 実行: `python -m unittest tests/test_memory_core.py`
