# Issue: 一部 LLM プロバイダの anyOf 構造化出力対応が不十分

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-10
**関連**: [meta_judgment_structured.md](../intent/persona_cognition/meta_judgment_structured.md) (メタ判断 v2 が `anyOf` field-level discriminator パターンに依存)

## 背景

メタ判断 v2 では `response_schema` に `anyOf` を含む discriminator パターン (例: `decision: anyOf[{type:"activate",...}, {type:"create",...}]`) を使う方針。SAIVerse の既存実装を確認したところ、プロバイダごとに対応状況に差がある。

| プロバイダ | コード | anyOf 対応 |
|---|---|---|
| OpenAI strict / NVIDIA NIM | `llm_clients/schema_utils.py:_process` | ◯ 再帰処理 (anyOf 内 object に `additionalProperties: false` + required 全列挙を自動補完) |
| Anthropic | `llm_clients/anthropic_request_builder.py:_fix_object` (L36-39) | ◯ `anyOf` / `allOf` を再帰処理 (※ `oneOf` は処理されないので避ける) |
| Gemini | `llm_clients/gemini.py:_to_schema` (L453-457) | ◯ ネイティブ `any_of` に変換 |
| **xAI** | `llm_clients/xai_schema_utils.py:_json_type_to_annotation` | **✗ Pydantic 変換時に `anyOf` を見ていない** |
| **Ollama** | `llm_clients/ollama.py` | **? 未確認** |
| **llama_cpp** | `llm_clients/llama_cpp.py` | **? 未確認** |

メタ判断 v2 の Playbook が xAI を重量級モデルに使う場合、`anyOf` を含むスキーマで実行時エラーになる可能性が高い。Ollama / llama_cpp も同様に未検証。

## 解決案候補

### 1. 各プロバイダの対応状況を実機検証

- 簡単な `anyOf` discriminator スキーマを使った動作確認スクリプトを書く
- 構造化出力 API を持つプロバイダ (Ollama JSON モード、llama_cpp grammar) で実際に通るか確認
- 失敗パターン (エラー / 構文無視 / 一部フィールドのみ充足等) を記録

### 2. 対応コードの追加

- **xAI** (`xai_schema_utils.py`):
  - `_json_type_to_annotation` で `anyOf` を Pydantic の `Union[...]` に変換するロジックを追加
  - `Literal[const_value]` で discriminator フィールドを表現
- **Ollama** / **llama_cpp**:
  - 構造化出力モード (json_schema 系) で `anyOf` がそのまま渡って動くか確認
  - ダメなら手前で平坦化するフォールバックを入れるか、ドキュメントで「これらのプロバイダはメタ判断 v2 の重量級モデルに使えない」と明記

### 3. メタ判断 v2 側のフォールバック

- スキーマ構築時にプロバイダ種別を見て、anyOf 非対応プロバイダなら平坦 discriminator (全フィールド required + decision_type で分岐) に切り替える
- 実装は MetaLayer 側で `_build_response_schema(provider_type, situation)` のような分岐
- ただし複雑になるので、まずは 1 + 2 で対応する方針

## 関連リソース

- メタ判断 v2 Intent: `docs/intent/persona_cognition/meta_judgment_structured.md`
- OpenAI 構造化出力 strict mode 仕様: <https://platform.openai.com/docs/guides/structured-outputs>
- 各クライアント実装:
  - `llm_clients/schema_utils.py`
  - `llm_clients/anthropic_request_builder.py`
  - `llm_clients/gemini.py`
  - `llm_clients/xai_schema_utils.py`
  - `llm_clients/ollama.py`
  - `llm_clients/llama_cpp.py`

## ログ

- 2026-05-10: 起票。メタ判断 v2 設計議論の中で発覚。OpenAI / Anthropic / Gemini は実装済みだが xAI 未対応、Ollama / llama_cpp 未確認。
