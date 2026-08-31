# Issue: usage 収集漏れの棚卸し(画像生成コスト + 全 LLM コール点検)

**ステータス**: 🔲 未着手
**優先度**: mid
**作成日**: 2026-07-08
**関連**: `saiverse/usage_tracker.py`, `builtin_data/tools/image_generator.py`, `saiverse/model_configs.py`(pricing), `frontend/src/app/usage/page.tsx`(表示)

## 背景

画像生成でかかった金額は本来コストとして確認できるはずなのに、現在の usage 集計に**まったく計上されていない**。まはーの気づきから起票。ついでに「他にも usage 収集できていない LLM コールが無いか」を一度総点検する。

## 実体(調査で判明・2026-07-08)

- usage の記録本体は `UsageTracker.record_usage(model_id, input_tokens, output_tokens, ...)`(`saiverse/usage_tracker.py`)。**トークン数を前提**にした設計で、`calculate_cost(model_id, input_tokens, output_tokens, ...)` でコストを出す。
- 画像生成 `builtin_data/tools/image_generator.py` は `record_usage` を**一切呼んでいない**(grep で usage / cost 系の記録コード無し)。かつ画像は「入力/出力トークン」ではなく**枚数・解像度ベースの課金**なので、現行の record_usage にそのまま乗らない。
- → 画像生成コストは DB(`LLMUsageLog`)に一切残らず、`usage/page.tsx` にも出ない。

## 確認事項

1. 画像生成 API(Gemini 2.5 Flash Image)の**課金単位**(1枚あたり固定? 解像度依存? トークン換算あり?)を公式で確認する。
2. `LLMUsageLog` / `record_usage` に「トークンを伴わない、コスト直接指定」の記録経路を足すのが良いか、`record_usage` を汎用化するか(cost を直接渡せる口)。
3. 記録時の `category`(例 `image_generation`)と `model_id` の扱い。
4. **棚卸し**: `record_usage` / `record_cache_storage` の呼び出し箇所を全部洗い出し、LLM を叩いているのに usage 記録が抜けている経路が他に無いか点検する(grep `record_usage` の呼び出し元 ↔ 実際に LLM を呼ぶ経路の差分)。

## 解決案候補

- `UsageTracker` に画像/固定コスト用の記録メソッド(例 `record_flat_cost(model_id, cost, *, category, persona_id, ...)`)を追加、または `record_usage` に `flat_cost` 引数を足す。
- `image_generator.py` の生成成功後にコストを記録する(枚数 × 単価、単価は model JSON の `pricing` に image 用フィールドを追加)。
- 棚卸しで見つかった他の漏れも同じ仕組みで塞ぐ。

## 関連リソース

- `saiverse/usage_tracker.py`(`record_usage` / `record_cache_storage` / `_flush_to_db`)
- `builtin_data/tools/image_generator.py`
- `saiverse/model_configs.py`(`calculate_cost` / `get_model_pricing` / pricing 定義)
- `frontend/src/app/usage/page.tsx`(表示側。通貨混在の別件は #7 で対応中)
- アイディア帳: `docs/overview/ideas.md`「基盤/開発体験」

## ログ

- 2026-07-08: 起票。ideas.md から昇格。画像生成が record_usage を呼んでいない点まで調査済み。
