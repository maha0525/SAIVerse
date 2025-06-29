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
    
- 各種Buildingはファイル単位で切り出し可能な設計に

- ユーザーエンドのUIとしてGradioを使用
- OpenAI / Gemini / Ollama でストリーミング応答に対応

### セットアップ
ルートディレクトリに `.env` ファイルを作成し、`OPENAI_API_KEY` または
`GEMINI_API_KEY` を設定します。
例:
```bash
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
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
├── system_prompts/
│   └── common.txt            # 共通システムプロンプト
├── ai_sessions/
│   └── air/
│       ├── base.json
│       ├── memory.json       # セッション情報保存用
│       └── system_prompt.txt
└── README.md                 # この仕様の要約
```

---

### 🔄 今後拡張予定（現時点で実装不要）

- AI同士の対話ルーム（カフェなど）
    
- Buildingの自動生成
    
- 永続的記憶との連携（Notion / ローカルファイルなど）
    

---

### 📤 Codexへの依頼指示

> 上記仕様に基づき、`main.py` と `router.py` の雛形コードをまず作成してください。  
> Buildingの定義はファイル分割できるよう設計してください。  
> AIの発話はJSON・プレーンテキストの両方に対応してください。
> 丁寧にロギングを行い、バグfixをしやすいように。

### 🔧 開発時の注意

システムプロンプトでは`str.format()`で変数展開を行うため、例示のJSONなどで波括弧`{}`をそのまま使うとエラーになります。表示用に記載する場合は`{{`と`}}`でエスケープしてください。