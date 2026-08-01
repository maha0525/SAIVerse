# 構造化出力の経路で使用量が記録されない (OpenAI 互換 / NVIDIA NIM)

**状態**: 未解決 (2026-08-01 起票、Codex レビュー一巡目の指摘3・二巡目の指摘4)。使用量帰属の修正 (`92ead95` / `742ba25`) とは独立した既存欠陥。

`_store_usage` が呼ばれないタイプの欠落なので、設定キーへの帰属を直した `92ead95` では直らない。

## 経路1: OpenAI 互換クライアントの `generate_stream` + `response_schema`

`llm_clients/openai.py` の `generate_stream` は `stream=not bool(response_schema)` でリクエストを組む。構造化出力を求めると**非ストリームのリクエスト**になり、`_stream_text_mode` の非ストリーム分岐へ入る。

この分岐は `_store_reasoning` / `_store_reasoning_details` は呼ぶが、**`_store_usage` を呼ばない**。ストリーム分岐の側は `store_usage_from_last_chunk` で記録しているので、同じメソッドの中で片方だけ落ちている。

`NvidiaNIMClient` はこの実装を継承するため、NIM でも同じ欠落が起きる。

## 経路2: NVIDIA NIM の forced function calling 経路

`llm_clients/nvidia_nim.py` の `generate` は、**tools 無し + `response_schema` あり**のときだけ別経路に入る。Mistral 系向けの回避策として `_create_nim_structured_output_via_tool` で生 HTTP を叩き、その結果を `return text_body` でそのまま返す。

親の `OpenAIClient.generate` へ進まないため `_store_usage` が呼ばれず、`consume_usage()` は `None` を返す。

## 影響

構造化出力を使うノード (SEA の judgment 系や router 系で `response_schema` を持つもの) の使用量が、使用画面と予算計算の双方から欠落する。費用が過小に見えるだけでなく、作業セッションの予算消費も実際より少なく見積もられる。

## 修正の方向

1. 経路1: 非ストリーム分岐でもレスポンスの `usage` を読み、`_store_usage` を一度だけ呼ぶ (ストリーム分岐と二重計上しないこと)。
2. 経路2: `_create_nim_structured_output_via_tool` のレスポンスから `usage` を読み、同じく一度だけ記録する。
3. 検証は factory 経由で client を作り、`response_schema` 付きの `generate_stream` と NIM の forced function calling 双方で `UsageInfo` が生成され、その `model` が設定キーであることを見る。

## 訂正の記録

本 issue は当初「usage を記録しない client は NIM のこの分岐のみ」と書いていたが、**それは誤りだった** (2026-08-01、Codex 二巡目の指摘で判明)。`llm_clients/` を `_store_usage` の有無で見て「他の client は各応答経路で呼んでいる」と断定したが、実際には `OpenAIClient.generate_stream` の中に呼ばない分岐があり、client 単位で見たことで**メソッド内の分岐**を見落としていた。範囲を経路単位へ改めた。
