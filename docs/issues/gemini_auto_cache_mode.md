# Gemini 自動キャッシュモード

## 状態: 実装済み (2026-05-30) — 環境変数 `SAIVERSE_GEMINI_AUTO_CACHE=1` で有効化

## 背景

Gemini explicit cache は create が無料で、cached hit は通常 input の 1/10 価格。実測により「単発の呼び出しであっても create → generate → delete すると generate 時の入力が cached price (1/10) になる」ことが判明した (2026-05-29)。

つまり、Gemini のすべての LLM 呼び出しで自動的に B 戦略 (create → use → delete) を適用すれば、prefix の共通性に関係なく常に 1/10 価格で利用できる。

## 提案

環境変数 (例: `SAIVERSE_GEMINI_AUTO_CACHE=1`) で Gemini 自動キャッシュモードを有効化できるようにする。

### ON のとき

- 全ての Gemini `generate` / `generate_stream` 呼び出しで:
  1. 入力全体 (system + contents) を explicit cache に create
  2. `cached_content` を指定して generate (入力が 1/10 価格)
  3. 完了後に cache を delete (storage 課金を最小化)
- 既存のキャッシュ設定 (`enable_cache` / `cache_ttl` / TTL モード / タイマー UI) は全て無効化される (毎回 create → 即 delete なので意味がない)
- 1024 トークン未満の短いプロンプトでは create が失敗するため、silent fallback (キャッシュなしで通常コール)

### 影響範囲

- Chronicle / Memopedia 生成 (現在キャッシュ未使用、最も効果が大きい)
- 通常の会話 (既に B 戦略が部分的に実装済み、`_resolve_explicit_cache`)
- 自律行動 pulse

### cache_lifecycle_control.md との関係

`docs/intent/cache_lifecycle_control.md` の §8.2 で provider 統一抽象の見直しが検討事項に挙がっている。自動キャッシュモードは Gemini 固有の最適化であり、Anthropic の延命戦略とは共存する形になる。モード ON 時は Gemini 側の TTL / モード設定を完全にバイパスするため、既存の 3 モード抽象 (標準/連続/マニュアル) は Anthropic 専用になる。

## 関連ファイル

- `llm_clients/gemini.py` — `_resolve_explicit_cache`, `generate`, `generate_stream`
- `llm_clients/gemini_cache.py` — `GeminiCacheController`
- `docs/intent/cache_lifecycle_control.md` — キャッシュライフサイクル制御の設計
- `sai_memory/arasuji/generator.py` — Chronicle 生成 (LLM 呼び出し)
- `sai_memory/memory/entity_extractor.py` — Memopedia 抽出 (LLM 呼び出し)
