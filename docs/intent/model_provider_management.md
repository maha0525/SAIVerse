# Intent: モデル＆プロバイダ管理 UI

**ステータス**: 実装完了（Phase 1-4 完了、2026-05-11）。実機検証はまはー側で実施。

## これは何か

LLM プロバイダとモデル設定を、ユーザーが UI から完結的に追加・編集・複製・削除できるようにする。具体的には:

1. **カスタムプロバイダの UI 追加**: Kimi (Moonshot) / LM Studio / llama.cpp サーバーなど、OpenAI 互換または Ollama 互換のエンドポイントを `~/.saiverse/user_data/providers/<id>.json` として UI から作成
2. **モデルファイルの UI 編集**: builtin/expansion のモデルを UI から「編集」「複製」操作で `~/.saiverse/user_data/models/<id>.json` に派生させる
3. **チャット UI からの永続化**: `ChatOptions` で調整中の thinking_effort / cache TTL / parameters を「別名で保存」「上書き保存」でモデルファイル化
4. **接続テスト**: 新規プロバイダ作成時にエンドポイントへの疎通確認を 1 クリックで実行

## なぜ必要か

### 問題1: カスタムプロバイダ追加の敷居が高い

ユーザーから寄せられている「Kimi API と接続したい」「LM Studio と接続したい」「llama.cpp サーバーと接続したい」という要望に対し、現状の解決策は **モデル JSON を手動で書いてもらう** しかない。具体的な敷居:

- `~/.saiverse/user_data/models/` の存在を知る必要がある
- `provider`, `base_url`, `api_key_env`, `context_length`, `parameters` 等のスキーマを把握する必要がある
- API キーは別途 `.env` ファイルに環境変数として設定する必要がある
- 1つのプロバイダで複数モデルを使いたい場合、各モデル JSON に同じ `base_url` / `api_key_env` を重複記述する必要がある

OpenAI 互換 API を提供するサービス・ローカル LLM サーバーは今後も無限に増える（vLLM、SGLang、LiteLLM proxy、OpenRouter、Together AI、Fireworks 等）。その都度モデル JSON 手書きを案内するのは持続不能。

### 問題2: 既存モデル設定の編集が面倒

builtin モデル（例: `claude-opus-4-7.json`）の thinking_effort、cache TTL、temperature 等をユーザーが恒久的に変えたい場合、現状の手順は:

1. `builtin_data/models/claude-opus-4-7.json` を `~/.saiverse/user_data/models/` にコピー
2. テキストエディタで開いて該当フィールドを編集
3. `python scripts/...` で reload するか、サーバー再起動

これも UI で完結するべき作業。さらに、チャット UI で `ChatOptions` から調整した値（thinking_effort や cache 設定）は **その場限り** で、永続化したければ上記手順を別途踏む必要がある。試行錯誤と保存が分断されている。

### 問題3: プロバイダ情報がモデル JSON に分散

現状、`base_url` / `api_key_env` / `request_kwargs` / `convert_system_to_user` など **プロバイダ単位で共通の情報** を各モデル JSON に直書きしている (`saiverse/model_configs.py:80-200` 周辺の `factory.py` 分岐参照)。例えば LM Studio で 5 つのモデルを使うと、5 つの JSON に同じ `base_url: http://localhost:1234/v1` が重複する。LM Studio のポートを変更したくなったら 5 ファイル全部直す必要がある。

### 問題4: factory.py がプロバイダごとに if-elif で分岐

`llm_clients/factory.py:53-219` は `provider == "openai"`, `"anthropic"`, `"gemini"`, ... と分岐している。新しい「カスタム互換系」プロトコルを増やすには、Python コードに分岐を足す必要があり、UI 完結にならない。

## 守るべき不変条件

### 1. 3 層優先順位は維持する

`user_data/` > `expansion_data/` > `builtin_data/` の優先順位は、プロバイダ JSON ・モデル JSON ともに維持する。`saiverse/data_paths.py:iter_files()` が既にこの優先順位で検索しているため、プロバイダディレクトリも同じ仕組みに乗せる。

### 2. 既存モデル JSON は引き続き読める

すでに 100+ 個ある `builtin_data/models/*.json` には `provider` / `base_url` / `api_key_env` が直書きされている。新スキーマ `provider_ref` を追加するが、**直書きフィールドが指定されていればそれを優先**し、`provider_ref` のみが指定されている場合に参照先 JSON から継承する。後方互換は完全に維持。

### 3. API キー本体は環境変数のまま

UI 上で管理するのは「どの環境変数名（`api_key_env`）を使うか」だけで、キーの値そのものは `.env` ファイルまたは OS 環境変数で持つ。理由:

- キー値を JSON / DB に保存すると、バックアップ・git 管理・スナップショット時の漏洩リスクが上がる
- 現状の `GlobalSettingsModal` の環境変数タブが既に env 変数管理 UI を提供している（`api/routes/config.py` の `model_roles` 経由）。同じ仕組みに乗せれば追加のセキュリティ設計が不要
- env 変数方式なら CI/CD・Docker 環境でも標準的な扱いができる

### 4. builtin / expansion は不可変

builtin と expansion 配下の JSON はユーザー操作で書き換わってはいけない。「編集」アクションは常に **user_data へのコピーを作ってから編集** する。これにより:

- アップデート（`update.bat` の git pull）でユーザー編集が消えない
- expansion パッケージの更新が安全
- 「リセット」操作 = user_data の派生ファイルを削除するだけで済む

### 5. プロバイダ参照は 1 段階のみ

モデル JSON が参照するプロバイダは 1 段（`provider_ref: "lmstudio"`）のみ。プロバイダがさらに別プロバイダを参照する多段継承は許可しない。理由はループ検出やデバッグの複雑性に対するコスト効果が薄いため。

### 6. UI から作成可能なプロトコル範囲

Phase 1 では **OpenAI 互換** と **Ollama 互換** のみ。Anthropic 互換 / Gemini 互換 / ネイティブ llama.cpp / Codex OAuth 等は builtin プロバイダとして固定で同梱し、UI 作成対象外。Anthropic 互換 API（DeepSeek-Anthropic 互換等）は将来対応の余地として設計に残す。

### 7. プロバイダ削除時のモデル整合性

プロバイダを削除すると、それを参照するモデル JSON は壊れる。削除前に **参照しているモデル一覧を表示し、ユーザーに確認** を取る。強制削除した場合のモデル側の挙動は「ロード時に警告ログを出してスキップ」（既存の `model_configs.py:34` の missing field 扱いに準拠）。

### 8. チャット UI の試行錯誤体験を壊さない

`ChatOptions` の操作感は変えない。「別名で保存」「上書き保存」ボタンは追加するが、既存のスライダー・入力欄の挙動・即時反映は維持。詳細編集は別 UI に飛ばすことで、チャット UI 自体の情報密度を増やさない。

## 設計

### A. プロバイダ JSON スキーマ

新規ディレクトリ: `~/.saiverse/user_data/providers/` および `builtin_data/providers/`

スキーマ例（OpenAI 互換）:

```json
{
  "id": "lmstudio",
  "display_name": "LM Studio (local)",
  "protocol": "openai_compat",
  "base_url": "http://localhost:1234/v1",
  "api_key_env": "LMSTUDIO_API_KEY",
  "default_request_kwargs": {},
  "default_convert_system_to_user": false,
  "default_supports_images": false,
  "default_max_image_bytes": 5242880
}
```

スキーマ例（Ollama 互換）:

```json
{
  "id": "ollama-remote",
  "display_name": "Ollama (Remote)",
  "protocol": "ollama_compat",
  "base_url": "http://192.168.1.10:11434"
}
```

スキーマ例（builtin、Anthropic 純正）:

```json
{
  "id": "anthropic",
  "display_name": "Anthropic",
  "protocol": "anthropic_native",
  "api_key_env": "CLAUDE_API_KEY",
  "builtin": true
}
```

**フィールド説明**:

- **`id`**: プロバイダ一意識別子（ファイル名 stem と一致させる）
- **`protocol`**: `openai_compat` / `ollama_compat` / `anthropic_native` / `gemini_native` / `xai_native` / `openai_codex` / `nvidia_nim`。UI 作成可能なのは前2つのみ
- **`base_url` / `api_key_env`**: 該当プロトコルが必要とする接続情報
- **`default_*`**: モデル JSON 側で `request_kwargs` / `convert_system_to_user` 等が未指定の場合のフォールバック値
- **`builtin`**: true なら UI から編集・削除不可（builtin_data 配下のものは自動的に true 扱い）

### B. モデル JSON の `provider_ref` 対応

新フィールド `provider_ref` を追加。解決順序:

1. モデル JSON に `base_url` / `api_key_env` 等が直書きされていればそれを使う（既存挙動）
2. 直書きがなく `provider_ref` があれば、参照先プロバイダ JSON から継承
3. 両方ない場合は従来通り `provider` フィールドの builtin デフォルト

例:

```json
{
  "model": "qwen2.5-72b-instruct",
  "display_name": "Qwen 2.5 72B (LM Studio)",
  "provider_ref": "lmstudio",
  "context_length": 32768,
  "parameters": {
    "temperature": { "type": "slider", "min": 0, "max": 2, "default": 0.7 }
  }
}
```

`provider_ref` 解決後、内部的には旧 `provider` / `base_url` / `api_key_env` が埋まった状態と等価になる。`factory.py` から見れば既存の dict と同じ構造。

実装場所: `saiverse/model_configs.py` に `_resolve_provider_ref(config: dict) -> dict` を追加し、`load_configs()` 内で各モデル設定読み込み時に解決を実行する。

### C. プロバイダ設定モジュール

新規モジュール: `saiverse/provider_configs.py`

API:

```python
def load_configs() -> dict[str, dict]: ...
def reload_configs() -> dict[str, dict]: ...
def get_provider(provider_id: str) -> dict | None: ...
def save_provider(provider_id: str, config: dict) -> None: ...  # user_data に書く
def delete_provider(provider_id: str) -> None: ...  # user_data からのみ削除可
def list_models_using_provider(provider_id: str) -> list[str]: ...  # 削除前確認用
def is_builtin(provider_id: str) -> bool: ...
```

`load_configs()` は `iter_files(PROVIDERS_DIR, "*.json")` で 3 層優先順位読み込み。`builtin_data/providers/` 配下は自動で `builtin: true`。

### D. factory.py の更新

`llm_clients/factory.py` の分岐を以下のように変更:

```python
def get_llm_client(model: str, provider: str, context_length: int, config: dict | None = None) -> LLMClient:
    if config is None:
        config = get_model_config(model)

    # provider_ref 解決後の config が来る前提（model_configs.py 側で解決済み）
    protocol = config.get("protocol") or _legacy_provider_to_protocol(config.get("provider"))

    if protocol == "openai_compat":
        return _build_openai_client(model, config, context_length)
    elif protocol == "ollama_compat":
        return _build_ollama_client(model, config, context_length)
    elif protocol == "anthropic_native":
        return _build_anthropic_client(model, config, context_length)
    # ... 以下既存
```

`_legacy_provider_to_protocol()` は `provider == "openai"` → `"openai_compat"`、`"ollama"` → `"ollama_compat"`、その他はそのまま native 扱いにする後方互換マッピング。既存モデル JSON は `protocol` フィールドを持たないため、必ずこの経路を通る。

各 `_build_*_client()` 関数は現状の if-elif ブロックの中身を関数化したもの。ロジック変更はせず分割のみ。

### E. API エンドポイント

新規ルート: `api/routes/providers.py`

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/providers/` | プロバイダ一覧（builtin / user / expansion バッジ付き） |
| GET | `/api/providers/{id}` | プロバイダ詳細 |
| POST | `/api/providers/` | プロバイダ新規作成（user_data へ） |
| PUT | `/api/providers/{id}` | プロバイダ更新（builtin の場合は自動で user_data コピーに切り替えて編集） |
| DELETE | `/api/providers/{id}` | プロバイダ削除（user_data 配下のみ可） |
| POST | `/api/providers/{id}/test` | 接続テスト（後述） |
| GET | `/api/providers/{id}/models` | このプロバイダを参照するモデル一覧 |
| POST | `/api/providers/reload` | ディスクから再読み込み |

既存 `api/routes/config.py` のモデル管理エンドポイントを拡張:

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/config/models/` | モデル新規作成（user_data へ） |
| PUT | `/api/config/models/{key}` | モデル更新（builtin/expansion なら user_data コピー作成→編集） |
| DELETE | `/api/config/models/{key}` | モデル削除（user_data 配下のみ可） |
| POST | `/api/config/models/{key}/clone` | モデル複製（新しい key で user_data に保存） |
| POST | `/api/config/models/save-from-chat` | チャット UI の現在設定を新規モデル化（後述） |

### F. 接続テスト

`POST /api/providers/{id}/test` の実装:

- **OpenAI 互換**: `GET {base_url}/models` を Bearer token 付きで叩く。200 が返れば成功、レスポンスのモデル一覧を返す（フロントで「あなたのプロバイダは X 個のモデルを公開しています」と表示してモデル作成支援）
- **Ollama 互換**: `GET {base_url}/api/tags` を叩く。同様にモデル一覧を返す
- タイムアウト 5 秒、エラー時はステータスコード・エラーメッセージを構造化して返す

接続テストの結果はサーバー側で保存しない（その時点の疎通確認のみ）。

### G. フロントエンド UI

新規セクション「モデル管理」を `GlobalSettingsModal` の新タブとして追加（独立画面化はしない、既存パターン踏襲）。

タブ内構成:

```
┌─────────────────────────────────────────────┐
│ [プロバイダ] [モデル]                       │
├─────────────────────────────────────────────┤
│ プロバイダタブ:                              │
│  ┌────────────────────────────────────────┐ │
│  │ OpenAI         [builtin]  [接続テスト] │ │
│  │ Anthropic      [builtin]  [接続テスト] │ │
│  │ LM Studio (local) [user]  [編集][削除] │ │
│  │ + 新規プロバイダ作成                    │ │
│  └────────────────────────────────────────┘ │
│                                              │
│ モデルタブ:                                  │
│  ┌────────────────────────────────────────┐ │
│  │ Filter: [All] [Anthropic] [LM Studio]  │ │
│  │ ────────────────────────────────────── │ │
│  │ Claude Opus 4.7 [builtin]              │ │
│  │   ↳ [編集 → user_data コピー作成]      │ │
│  │   ↳ [複製]                             │ │
│  │ Qwen 2.5 72B (LM Studio) [user]        │ │
│  │   ↳ [編集] [削除]                      │ │
│  │ + 新規モデル作成                        │ │
│  └────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

新規プロバイダ作成モーダル:

- プロトコル選択（OpenAI 互換 / Ollama 互換）
- ID（slug 形式、ファイル名になる）
- 表示名
- base_url
- api_key_env（OpenAI 互換のみ、Ollama 互換は通常不要）
- 「接続テスト」ボタン → 成功時に「モデルを取り込む」ボタン表示

新規モデル作成モーダル:

- プロバイダ選択（プルダウン）
- モデル ID（API 呼び出し時の名称）
- 表示名
- context_length
- supports_images チェックボックス
- parameters（temperature 等のスライダー定義、Phase 1 では JSON エディタ。構造化フォームは将来検討）
- pricing（任意、入力支援あり）
- cache 設定（プロバイダが対応している場合のみ表示）

### H. チャット UI 連携

`ChatOptions.tsx` への追加:

- **「別名で保存」ボタン**: 現在表示中のモデル設定 + 編集中のパラメータを新規モデル JSON 化。モーダルで新しい `display_name` と key 名を入力させる
- **「このモデルに上書き保存」ボタン**:
  - user_data モデルの場合: 直接上書き
  - builtin/expansion モデルの場合: 「このモデルは builtin です。user_data にコピーを作成して編集します」と確認ダイアログ → user_data コピーを作って上書き
- **「詳細編集」リンク**: モデル管理 UI のモデル編集モードを **チャット UI の上にモーダル重ねで開く**（チャット UI は閉じない）。編集完了後にモデル管理モーダルを閉じれば元のチャット UI に戻る

「別名で保存」「上書き保存」のリクエストは `POST /api/config/models/save-from-chat` または既存の `PUT /api/config/models/{key}` に流す。チャット UI の状態（current_values）をそのままペイロードに含める。

### I. ディレクトリ構成

```
builtin_data/
├── providers/
│   ├── anthropic.json       ← protocol: anthropic_native, builtin: true
│   ├── gemini.json          ← protocol: gemini_native
│   ├── openai.json          ← protocol: openai_compat (公式 OpenAI)
│   ├── ollama.json          ← protocol: ollama_compat (localhost:11434)
│   ├── nvidia_nim.json      ← protocol: nvidia_nim
│   ├── xai.json             ← protocol: xai_native
│   └── openai_codex.json    ← protocol: openai_codex
└── models/                  ← 既存 100+ JSON、当面は触らない（互換性）

~/.saiverse/user_data/
├── providers/
│   ├── lmstudio.json        ← ユーザーが UI で作成
│   ├── kimi.json
│   └── llama-cpp-server.json
└── models/
    ├── qwen-via-lmstudio.json    ← provider_ref: "lmstudio"
    ├── kimi-k2.json              ← provider_ref: "kimi"
    └── claude-opus-4-7-tweaked.json  ← builtin からコピーして編集
```

### J. データベース変更なし

プロバイダ・モデル設定は JSON ファイルのまま。DB スキーマ変更不要。

## 設計判断の理由

### なぜ provider_ref を独立 JSON にするか（Q2 の答え）

選択肢として「プロバイダ概念は UI 入力支援だけにし、保存時は各モデル JSON に展開する」もあったが、独立 JSON 方式を採った理由:

- **1 プロバイダ変更 → 全モデル波及**: LM Studio のポートを変更したい時、プロバイダ JSON 1 つを直すだけで参照する全モデルに反映される
- **API キー env 変数名の集約**: プロバイダ単位で `api_key_env` を持てば、複数モデルを使うユーザーが env 変数名を覚え直さなくて済む
- **プロバイダ削除時の整合性チェックが可能**: 「このプロバイダを使っているモデル」を逆引きできる
- **既存仕組みとの互換**: モデル JSON 側の直書きフィールドを優先する仕様にすることで、100+ 個の既存 builtin モデルを書き換えずに済む

### なぜ API キーを環境変数のままにするか

UI で API キー値そのものを保存する選択肢もあったが、以下の理由で env 変数方式を維持:

- **既存仕組みの再利用**: `GlobalSettingsModal` の環境変数タブが既に存在する。プロバイダ作成時に「`LMSTUDIO_API_KEY` を設定してください」と促し、env タブへのリンクを置けば UX は完結
- **セキュリティ**: JSON / DB に平文保存するとバックアップ・スナップショット・git 経由の漏洩リスクが増える。暗号化保存するなら鍵管理の問題が発生
- **CI/CD・Docker 互換**: env 変数なら標準的な秘匿情報の扱い方に乗れる
- **ローカル LLM サーバーは API キー不要**: LM Studio や llama.cpp サーバーはデフォルトで認証なし。`api_key_env` を空にすれば dummy key (`"sk-no-key-required"` 等) を OpenAIClient 内部で使う既存挙動が活きる

### なぜ Anthropic 互換を Phase 1 に入れないか

DeepSeek が Anthropic 互換 API を出す等、需要は今後発生し得るが:

- 現時点でユーザーから明示的な要望は OpenAI 互換系（Kimi/LMStudio/llama.cpp）のみ
- Anthropic クライアントは thinking_effort / cache TTL 等の固有機能が多く、汎用「Anthropic 互換プロバイダ」として整理するには既存 `AnthropicClient` の設計レビューが必要
- 互換系 Anthropic API がどこまで純正 API の機能を再現するかは実装ごとにまちまちで、汎用化の難易度が高い

将来的に追加するときは `protocol: "anthropic_compat"` を増やすだけの設計余地は残してある。

### なぜ builtin の直接編集を許さないか

選択肢として「builtin JSON を直接書き換え可能」もあったが:

- `update.bat` の git pull で builtin が上書きされ、ユーザー編集が消える
- リポジトリ管理されているファイルが手元で書き換わると git 状態が汚れる
- 「builtin に戻したい」操作が「リポジトリから再取得」と等価になり、編集をリセットする標準的な手段がない

「編集 → 自動で user_data コピー作成」フローなら、リセット = user_data ファイル削除で完結する。

### なぜ接続テストを MVP に含めるか

新規プロバイダ作成時に「設定して保存 → 別画面でモデルを作って割り当て → チャットで初めて呼び出して失敗」という長いフィードバックループは UX として苦しい。プロバイダ作成画面で即座に疎通確認できれば:

- base_url のタイポ（末尾 `/v1` 忘れ等）が即座に判明
- API キー env 変数が未設定なことに気付ける
- レスポンスから取得したモデル一覧で、モデル作成画面のモデル ID プルダウンを補助できる

実装コストも低い（プロトコルごとに 1 エンドポイント叩くだけ）。

### なぜチャット UI に編集本体を置かず、モデル管理 UI に引き継ぎとするか（Q3 の答え）

`ChatOptions` に thinking_effort / cache / parameters の全フィールド編集を押し込むと:

- チャット UI が情報過多になり、本来の試行錯誤体験が損なわれる
- 同じ編集 UI が「チャット内」「モデル管理画面」の 2 箇所に存在することになり、保守コストが倍

チャット UI は「現在使っている値の試行錯誤＋その値を保存する 2 操作」に絞り、フル編集はモデル管理 UI に集約する。

### なぜプロトコルを `openai_compat` / `ollama_compat` の 2 種に分けるか

LM Studio は OpenAI 互換 API を提供するため `openai_compat` で扱える。Ollama 互換 API も提供しているが、SAIVerse からは `OpenAIClient` で十分動作する。それでも分けたのは:

- Ollama 互換サーバー（`OllamaClient`）は API キー不要・コンテキスト長指定方法が異なる等、`OpenAIClient` と挙動差がある
- Ollama 互換であることをユーザーが明示できると、UI で適切な接続テスト（`/api/tags` vs `/v1/models`）を実行できる
- 将来的に Ollama 固有機能（ローカルモデルのプル等）を UI から触る余地を残せる

## スコープ

### Phase 1 — バックエンド基盤

1. `~/.saiverse/user_data/providers/` および `builtin_data/providers/` ディレクトリ概念を追加（`saiverse/data_paths.py` に `PROVIDERS_DIR` 追加）
2. `saiverse/provider_configs.py` 新規実装（`load_configs` / `get_provider` / `save_provider` / `delete_provider` / `list_models_using_provider` / `is_builtin`）
3. builtin プロバイダ JSON を `builtin_data/providers/` 配下に作成（anthropic, gemini, openai, ollama, llama_cpp, nvidia_nim, xai, openai_codex の 8 個）
4. `saiverse/model_configs.py` の `load_configs()` で `_resolve_provider_ref()` を実行し、`provider_ref` 指定時にプロバイダ JSON からフィールドを継承
5. `llm_clients/factory.py` を `protocol` フィールドベースの分岐に変更（`_resolve_protocol()` + `_LEGACY_PROVIDER_TO_PROTOCOL` で後方互換）。各分岐の関数化は実施せず、既存ロジックに触らない方針で最小変更とした（関数分割は将来 Phase で検討）
6. テスト: 既存全モデル JSON が新ロード経路で同じ挙動になること、`provider_ref` 解決の優先順位、プロバイダ削除時の参照モデル列挙

### Phase 2 — API

7. `api/routes/providers.py` 新規実装（一覧・詳細・作成・更新・削除・接続テスト・参照モデル列挙・reload）
8. `api/routes/config.py` 拡張（モデル CRUD、複製、save-from-chat）
9. `_get_required_env_vars()` を `provider_ref` 経由でも env 変数を解決できるよう更新
10. テスト: API レベルの CRUD、builtin → user_data 自動コピー、接続テスト

### Phase 3 — フロントエンド

11. `GlobalSettingsModal` に「モデル管理」タブ追加
12. `ProviderManagementPanel.tsx` 新規（プロバイダ一覧・編集モーダル・接続テスト）
13. `ModelManagementPanel.tsx` 新規（モデル一覧・フィルタ・編集モーダル・複製・削除）
14. `ProviderEditor.tsx` / `ModelEditor.tsx` 新規（フォーム、parameters JSON エディタ）
15. `ChatOptions.tsx` に「別名で保存」「上書き保存」「詳細編集」ボタン追加
16. UX 検証: 新規プロバイダ作成 → 接続テスト → モデル作成 → チャットで使用、の一連の流れがスムーズか

### Phase 4 — ドキュメント整備

17. `CLAUDE.md` の「Model Configuration」セクション更新（provider_ref、UI 操作、protocol 一覧）
18. `docs/getting-started/` に「カスタムプロバイダ追加ガイド」を追記（Kimi・LM Studio・llama.cpp サーバー の具体例）
19. `README.md` の機能リストに記載

### 将来 Phase（範囲外、メモのみ）

- **モデル単位の任意fallback chain**（2026-07-16、まはー構想）:
  - 各model configから、障害時に切り替える別modelを順序付きで指定できるようにする。
  - fallback先は同一provider・paid modelに限定しない。free→paid、重量級→軽量、remote→local、別providerへの切替を同じ仕組みで表現する。
  - 現行routerのように「free失敗時は固定paid clientへ切替」「一度成功するとprocess全体をpaidへ固定」する暗黙policyは廃止し、requestごとにmodel configの明示chainを評価する。
  - failure分類は例外文字列substringではなくproviderの型付きstatus/retry metadataを使う。認証・入力不正・content policy等、fallbackしても解決しない失敗はchainを進めない。
  - streaming開始後や外部tool副作用後にはmodel fallbackでrequest全体を再実行しない。fallback可能なのはLLM出力・副作用がまだ確定していない境界だけとする。
  - chainの循環、存在しないmodel、利用不能credential、最大段数をload時に検証する。実行logには元model、選択先、分類済み理由を本文なしで残す。
  - paid modelを含むchainは明示opt-inとし、将来はbudget/cost ceilingもchain policyへ持たせる。
  - **今回（監査第二陣）は新規実装しない**。まず暗黙のprocess-global paid固定を停止し、明示設定なしではfallbackしない安全な暫定形へ直す。その後、本項を正典としてUI・schema・runtimeを設計する。
- `llm_clients/factory.py` の各 `if protocol == ...` 分岐を `_build_*_client()` 関数に分割するリファクタ（Phase 1 では分岐条件の変更のみで保留）
- Anthropic 互換プロトコル（DeepSeek-Anthropic 互換等）対応
- Gemini 互換プロトコル対応
- プロバイダごとの推奨パラメータプリセット（OpenRouter 用、vLLM 用 等）
- プロバイダの export / import（JSON ファイルダウンロード・アップロード、コミュニティ共有）
- 接続テスト時に取得したモデル一覧から「全モデルをまとめて作成」ボタン

## 検証観点

実機検証で必ず通すケース:

- **新規プロバイダ作成（OpenAI 互換）**: LM Studio をローカルで起動 → UI でプロバイダ作成 → 接続テスト成功 → モデル作成 → チャットで応答取得
- **新規プロバイダ作成（Ollama 互換）**: Ollama を別マシンで起動 → リモート IP でプロバイダ作成 → 接続テスト成功
- **接続失敗の挙動**: base_url タイポ・API キー env 未設定の場合、エラー内容が UI に表示される
- **builtin モデル編集**: Claude Opus 4.7 の thinking_effort を編集 → user_data にコピーが作成される → 編集前の builtin は変わらない
- **チャットからの保存**: ChatOptions で temperature を変更 → 「別名で保存」→ 新規モデルがリストに出現
- **チャットからの上書き（builtin）**: ChatOptions で値変更 → 「上書き保存」→ 確認ダイアログ → user_data コピー作成 → 上書き
- **プロバイダ削除時の参照確認**: 使用中のプロバイダを削除しようとすると、参照モデル一覧が表示されて警告
- **`provider_ref` 解決**: プロバイダの base_url を変更 → 参照する全モデルが新 base_url で動作
- **既存 builtin モデルの後方互換**: 100+ 個の既存モデルが従来通り読み込まれ、API 経由で利用可能
- **削除されたプロバイダを参照するモデル**: ロード時に警告ログ → モデル管理 UI で「壊れた参照」警告表示 → 修復 UI から別プロバイダに付け替え可能

## 補足: 設計上の前提

- **接続テストはサーバー側から実行**: フロントから直接 `localhost:1234` 等を叩くと、SAIVerse が別マシンで動いていてユーザー PC で LM Studio が動いている場合に接続できない。サーバー側 API として実装することで「SAIVerse が見える場所」での疎通確認になる
- **expansion_data のプロバイダも対象**: 将来的にアドオンが独自プロバイダを同梱する余地として、`expansion_data/<addon>/providers/` も `iter_files()` の対象に含める
- **既存の `is_model_available()` との関係**: 環境変数チェックは引き続き機能。`provider_ref` 経由で `api_key_env` を解決した後、既存ロジックがそのまま使える
- **`get_provider_for_model()` ヘルパー**: UI での「このモデルはどのプロバイダを使っているか」表示用に、新規ヘルパーを `model_configs.py` に追加（`provider_ref` 優先、なければ `provider` フィールドから推定）
