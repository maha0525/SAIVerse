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

### 🔖 現行ディレクトリ構成（抜粋）

```
SAIVerse/
├── main.py                    # Gradio UI 本体（ストリーミング表示）
├── saiverse_manager.py        # セッション/ルーティングの統括
├── conversation_manager.py    # 会話・モード制御（user/auto/manual）
├── action_handler.py          # ::act の解析と実行
├── history_manager.py         # 会話履歴の管理
├── occupancy_manager.py       # 滞在/移動の管理
├── llm_router.py              # ツール/モデルのルーティング
├── llm_clients.py             # OpenAI/Gemini/Ollama クライアント
├── sds_server.py              # ディレクトリサービス（FastAPI）
├── database/
│   ├── api_server.py          # 都市/建物/来訪AIのAPI（FastAPI）
│   ├── models.py, migrate.py, seed.py, saiverse.db
├── memory_core/               # 記憶コア（埋め込み/リトリーバ/ストレージ）
├── tools/
│   └── defs/
│       ├── calculator.py      # 計算ツール
│       └── image_generator.py # 画像生成ツール（Gemini）
├── system_prompts/
├── ai_sessions/
├── tests/
│   ├── test_llm_clients.py
│   ├── test_history_manager.py
│   ├── test_calculator.py
│   ├── test_image_generator.py
│   └── test_llm_router.py
└── README.md
```

補足: Building はファイル分割ではなくデータベース管理です（`database/`）。建物IDは都市に紐づき、例: `user_room_city_a`。

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

#### 現在テストが書かれているモジュール（抜粋）

- `llm_clients.py`: OpenAI/Gemini/Ollama の生成/ストリーム（`tests/test_llm_clients.py`）
- `history_manager.py`: 履歴の永続化ロジック（`tests/test_history_manager.py`）
- `tools.calculator`: 計算ツール（`tests/test_calculator.py`）
- `tools.image_generator`: 画像生成ツール（`tests/test_image_generator.py`）
- `llm_router.py`: ルーティング仕様（`tests/test_llm_router.py`）

---

### 📤 実装状況メモ（現状）

- ルーティング/セッションは `saiverse_manager.py` と `llm_router.py` に実装済みです（`router.py` は不要）。
- Building はデータベース管理（`database/`）。`seed.py` が初期データを投入し、`api_server.py` から参照します。
- Gradio UI は `main.py` に実装。SDS は `sds_server.py`、都市APIは `database/api_server.py` で起動します。

起動例:
```
pip install -r requirements.txt
python database/seed.py
python sds_server.py
python database/api_server.py --port 8001
python main.py
```

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
