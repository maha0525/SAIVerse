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
| `openai_codex` | OpenAI Codex (ChatGPT OAuth) | `openai_codex` | — | — |
| `plamo` | PLaMo (Preferred Networks) | `openai_compat` | `https://api.platform.preferredai.jp/v1` | `PLAMO_API_KEY` |
| `sakana` | Sakana AI | `openai_compat` | `https://api.sakana.ai/v1` | `SAKANA_API_KEY` |
| `ollama` | Ollama (local) | `ollama_compat` | `http://127.0.0.1:11434` | — |
| `llama_cpp_server` | llama.cpp Server (local) | `openai_compat` | `http://127.0.0.1:8080/v1` | — |

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
