# Cache Lifecycle Control — 次セッション引き継ぎ (2026-05-25)

設計: `docs/intent/cache_lifecycle_control.md` (v0.12)。本ファイルは進捗スナップショットと再開手順。

## 完了 (実装 + 実機検証済み)

- **Phase 1**: read-only キャッシュタイマー UI (Anthropic explicit cache)。`GET /api/people/{id}/cache-status`、ChatOptions に「キャッシュ（このペルソナ）」セクション + persona switcher + 2s polling。
- **Phase 2**: per-persona cache 設定 (オフ/5分/1時間)。`manager._persona_cache_overrides` + `manager.resolve_persona_cache`。`_get_cache_kwargs(persona_id)` / `_get_anchor_validity_seconds(model, persona_id)` / Phase 4-e を per-persona 化。UI 統合 (1 箇所)。
  - **TTL モデル (重要)**: 「生きてるキャッシュは短い TTL で短縮されない」(Anthropic 実測)。`_update_anchor_for_model` は生存中なら `max(既存, 新)` 維持・短い書き込みでは window をスライドさせない (モデルB / under-promise)。書き込み時 TTL を `METABOLISM_ANCHORS[model].ttl_seconds` に記録。
- **Phase 3 M1**: Gemini explicit cache の配線 (B 戦略 = system_instruction + contents[:-1] をキャッシュ、tail だけ送信)。`llm_clients/gemini_cache.py` (`GeminiCacheController`)、`gemini.py:_resolve_explicit_cache`。`gemini-2.5-flash` / `gemini-3.5-flash-paid` に cache 設定。**env `SAIVERSE_GEMINI_EXPLICIT_CACHE=1` で opt-in** (cleanup 未実装のため)。実機で text+media が正しくキャッシュされることを確認済み。

## 重要な確定事実 (調べ直し不要)

- **Gemini explicit cache は text + media をキャッシュする** (実測。画像も cache_tokens_details に乗る)。
- **Gemini は thought_signature (思考署名) をキャッシュしない**。3.5 Flash は thought preservation デフォルト ON で過去ターンの thinking を毎回再送 → 実測 ~10,579 tok/ターンが**キャッシュ不可で毎回課金**。B のバグではなく Gemini 仕様。
- B 機構の正常性・media キャッシュ・スケール (84k) は `temp/verify_gemini_cache_*.py` で実測済み。
- 実機ログのストリーミング usage は複数コール交錯でペアリング不能 → forensic 解析は避け、`temp/` の制御スクリプト or `cache split` ログ (backend.log) を使う。

## 残タスク (Phase 3 続き)

- **M2**: 起動時 orphan cleanup (`caches.list` → `displayName` prefix `saiverse:` で delete) + head 変更 (metabolism) での明示 invalidate + 即時 supersede delete (毎ターン create の古い cache 掃除)。
- **M3**: タイマー UI を Gemini 対応 (cache resource の `expire_time` で残り表示、persona/line 紐付け、Gemini の細かい TTL 選択肢 5m/15m/30m/1h を UI に)。
- **M4**: 標準/連続モード UI + `CacheLifecycleState` + `ICacheController` 形式化 + 標準モードの pulse 終了 delete (Phase 4 hook)。
- env ゲート `SAIVERSE_GEMINI_EXPLICIT_CACHE` は M2-M4 完了後に撤去。

## 別 issue (Phase 3 とは独立)

- `docs/issues/gemini_multiturn_reasoning_thought_signature_uncached.md` — マルチターン推論 (thought_signature) が cache 不可で毎ターン ~10k 課金。過去ターンの thought_signature を送信前に剥がす ON/OFF トグルを提案。`gemini.py` の thought_signature 再送経路に入れる。**制約**: 現ターンの function-call 署名は剥がすと 400。過去ターンのみ。

## 検証スクリプト (temp/、gitignore)

- `verify_gemini_cache_media.py` — 画像がキャッシュされるか (される)
- `verify_gemini_cache_b.py` / `_stream.py` — B 機構 (tail だけ送れば non_cached=tail)
- `verify_gemini_cache_scale.py` — 84k スケールでも全キャッシュ
- `verify_real_split.py` — 実 production messages を `_convert_messages` に通して split 確認 (thought_signature 発見の経緯)

## このセッションのコミット (feature/memory-notes-and-organize)

Phase 1-2 (`b8f6251`, `ea750bd`) → TTL 修正群 (`a33ef25`, `4faa3e3`, `484bc90`) → Phase 3 M1/B (`1f4948e`, `d7d32da`, `20494ad`) → 計測ログ + issue + 後始末。stackchan 等の無関係ファイルは未コミットのまま残置 (本作業外)。
