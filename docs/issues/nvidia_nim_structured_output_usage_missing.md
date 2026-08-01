# NVIDIA NIM の構造化出力経路が使用量を記録しない

**状態**: 未解決 (2026-08-01 起票、Codex レビューの指摘3)。使用量帰属の修正 (`92ead95`) とは独立した既存欠陥。

## 現象

`llm_clients/nvidia_nim.py` の `generate` は、**tools 無し + `response_schema` あり**のときだけ別経路に入る。Mistral 系モデル向けの回避策として `_create_nim_structured_output_via_tool` で forced function calling を生 HTTP で叩き、その結果を `return text_body` でそのまま返す。

この分岐は親の `OpenAIClient.generate` へ進まないため、`_store_usage` が一度も呼ばれない。結果としてこの経路では:

- `consume_usage()` が `None` を返す
- トークン数・費用・呼び出し回数のいずれも `LLMUsageLog` に記録されない

`_store_reasoning([])` は呼んでいるので、reasoning だけ初期化されて usage は素通りする形になっている。

## 影響

NIM の構造化出力を使うノード (SEA の judgment 系や router 系で `response_schema` を持つもの) の使用量が、使用画面と予算計算の両方から丸ごと欠落する。費用が過小に見えるだけでなく、作業セッションの予算消費も実際より少なく見積もられる。

`_store_usage` を呼ばない = `config_key` の問題ではないので、`92ead95` の修正では直らない。

## 修正の方向

1. `_create_nim_structured_output_via_tool` のレスポンスから `usage` を読み取り、`_store_usage` を一度だけ呼ぶ (二重計上しないこと)。
2. 検証は factory 経由で client を作り、構造化出力を模したレスポンスで `UsageInfo` が生成され、その `model` が設定キーであることを見る。同経路がスキーマ検証をどう扱うかも同時に確認する。

## 補足 — 確認済みの範囲

`llm_clients/` 全体で `_store_usage` の呼び出しを確認した限り、usage を記録していない client は NIM のこの分岐のみ。他の client (`openai` / `anthropic` / `xai` / `ollama` / `gemini` / `openai_codex`) は各応答経路で `_store_usage` を呼んでいる。NIM も **tools あり、または構造化出力なし**の通常経路では親の `OpenAIClient.generate` に入るので記録される。
