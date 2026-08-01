# LlamaCachedClient が state を委譲せず、tool call と reasoning が消える

**状態**: 未解決 (2026-08-01 起票、Codex レビュー二巡目の指摘1)。既存欠陥。使用量帰属の修正 (`742ba25`) で `config_key` の委譲だけを追加したため、**残りの委譲漏れが相対的に目立つ状態**になっている。

## 現象

`llm_clients/llama_cache.py` の `LlamaCachedClient` は任意の `LLMClient` を包む wrapper だが、inner へ委譲しているのは以下だけ:

- `configure_parameters`
- `consume_usage`
- `config_key` (`742ba25` で追加)
- `generate` / `generate_stream`

一方で `LLMClient` は他にも呼び出し側が消費する state を持つ。これらは委譲されていないため、**wrapper 自身の空の state** が返る:

- `consume_tool_detection`
- `consume_reasoning` / `consume_reasoning_details`
- `consume_thought_signature`
- `consume_attachments`
- `model` (wrapper 側は空文字のまま)
- `supports_audio` / `supports_video` (`supports_images` だけは `__init__` で引き継いでいる)

`sea/runtime_llm.py` はこれらを factory が返したオブジェクト、つまり wrapper に対して呼ぶ。

## 影響

`llama_slot_save_path` を設定したモデルを使うペルソナで、

- **tool call が実行されない**: `consume_tool_detection()` が `None` を返すため、inner が検出した tool call が通常のテキストとして扱われる
- **reasoning が消える**: `consume_reasoning()` が空を返す
- **thought signature が繋がらない**: Gemini 系を包んだ場合の思考連続性が切れる

現状 `llama_slot_save_path` を持つのはユーザー環境のローカルモデル設定 2 件。**実機で tool call を試した記録はまだ無い**ため、上記は機構から読んだ帰結であって観測ではない。

## 修正の方向

wrapper を「一部だけ委譲」から**完全な facade** へ変える。`consume_*` 全種と `model` / `supports_*` を inner へ委譲し、fake inner を使った回帰テスト (streaming tool 検出・reasoning・thought signature) を付ける。

`config_key` だけを property 委譲した現状は、`742ba25` で使用量帰属を塞ぐために必要だった最小限で、設計としては中途半端なまま止まっている。
