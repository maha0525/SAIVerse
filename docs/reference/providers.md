# プロバイダ一覧

`builtin_data/providers/` に同梱されるプロバイダ（LLM バックエンドへの接続先定義）の一覧。モデルは `provider_ref` でここを参照する。設計は [`docs/intent/model_provider_management.md`](../intent/model_provider_management.md)、エンドユーザー向け設定は [`docs/custom_providers.md`](../custom_providers.md) を参照。

## 組み込みプロバイダ

| id | 表示名 | protocol | base_url | api_key_env |
|---|---|---|---|---|
| `openai` | OpenAI | `openai_compat` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `anthropic` | Anthropic | `anthropic_native` | — | `CLAUDE_API_KEY` |
| `gemini` | Google Gemini | `gemini_native` | — | `GEMINI_API_KEY` |
| `xai` | xAI | `xai_native` | — | `XAI_API_KEY` |
| `nvidia_nim` | NVIDIA NIM | `nvidia_nim` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |
| `openrouter` | OpenRouter | `openai_compat` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `openai_codex` | OpenAI Codex (ChatGPT OAuth) | `openai_codex` | — | — |
| `plamo` | PLaMo (Preferred Networks) | `openai_compat` | `https://api.platform.preferredai.jp/v1` | `PLAMO_API_KEY` |
| `sakana` | Sakana AI | `openai_compat` | `https://api.sakana.ai/v1` | `SAKANA_API_KEY` |
| `lmstudio` | LM Studio (local) | `openai_compat` | `http://127.0.0.1:1234/v1` | —（不要） |
| `ollama` | Ollama (local) | `ollama_compat` | —（未設定＝自動探索） | — |
| `llama_cpp_server` | llama.cpp Server (local) | `openai_compat` | `http://127.0.0.1:8080/v1` | —（不要） |

`gemini` だけは `api_key_env_alternates: ["GEMINI_FREE_API_KEY"]` を併せ持つ。**`GEMINI_API_KEY` と `GEMINI_FREE_API_KEY` のどちらか一方が設定されていればモデル一覧に出る**（無料枠だけの利用を想定）。判定は `saiverse/model_configs.py` の `_get_required_env_vars()`。

### API キー不要のプロバイダ

`lmstudio` と `llama_cpp_server` は `api_key_required: false` を持つ。認証をしないローカルサーバー向けの宣言で、次の2つが変わる。

- **モデル一覧に出る条件** — キーが未設定でも利用可能として扱う（`_get_required_env_vars()` が空を返す）
- **クライアント生成** — OpenAI 互換クライアントは空のキーを受け付けないため、SAIVerse がプレースホルダを渡す。この宣言が無いと `OPENAI_API_KEY` を要求され、OpenAI のアカウントを持たない利用者はローカルモデルを起動できない

ローカルサーバー側に認証を掛けている場合は `api_key_env` を併記すれば、そちらが優先される（プレースホルダは環境変数が空のときだけ使われる）。この項目は UI（モデル管理 > プロバイダ > 「API キーなしで接続できるサーバー」）からも設定できる。

### Ollama の接続先だけ決め方が違う

他のプロバイダは `base_url` が固定だが、Ollama は待ち受けアドレスが環境によってばらつくため **未設定なら自動探索する**（`llm_clients/ollama.py` の `_probe_base`）。優先順位:

1. **設定されたアドレス** — `ollama` プロバイダの `base_url`（UI のプロバイダタブで編集 → `user_data` 上書き）、モデル JSON の `base_url`、環境変数 `OLLAMA_BASE_URL` / `OLLAMA_HOST` のいずれか。カンマ区切りで複数指定可
2. **自動探索** — 1が何も無いときだけ。`127.0.0.1:11434` → `localhost:11434` → `host.docker.internal:11434` → `172.17.0.1:11434` の順に実接続を試し、応答したものを採用（結果はプロセス内でキャッシュ）

**設定されたアドレスは探索対象を上書きせず、限定する。** 指定先が応答しなくても別のホストへ勝手に繋ぎ替えず、そのアドレスを保持して警告を出す（設定したはずの接続先と違う場所に繋がる事故を防ぐため）。

同梱の `ollama` プロバイダが `base_url` を持たないのはこのため。ここにアドレスを書くと全 Ollama モデルが「設定済み」になり、環境変数と自動探索の両方が無効になる。

## protocol の種類

| protocol | 説明 | UI から新規作成 |
|---|---|:--:|
| `openai_compat` | OpenAI 互換 API（LM Studio / llama.cpp server / Kimi 等） | ✓ |
| `ollama_compat` | Ollama 互換 API | ✓ |
| `anthropic_native` | Anthropic ネイティブ | ✗（builtin のみ） |
| `gemini_native` | Gemini ネイティブ | ✗（builtin のみ） |
| `xai_native` | xAI ネイティブ | ✗（builtin のみ） |
| `nvidia_nim` | NVIDIA NIM | ✗（builtin のみ） |
| `openai_codex` | OpenAI Codex（ChatGPT OAuth） | ✗（builtin のみ） |

`*_native` / `nvidia_nim` / `openai_codex` は `llm_clients/` にコード実装が必要なため builtin のみ。UI（モデル管理 > プロバイダ）から作れるのは `openai_compat` / `ollama_compat` の2種。

## 追加のしかた

- **UI**: グローバル設定 > モデル管理 > プロバイダタブ →「新規追加」→ protocol 選択 → base_url / api_key_env 入力 → 接続テスト
- **手動**: `~/.saiverse/user_data/providers/*.json` に配置（3層優先で builtin を上書き）
- 反映: `POST /api/providers/reload` または再起動

3層優先順位: `~/.saiverse/user_data/providers/` > `expansion_data/<addon>/providers/` > `builtin_data/providers/`
