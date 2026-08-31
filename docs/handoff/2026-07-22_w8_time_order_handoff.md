# W8 (柱6 — 時刻) 走行メモ — S7 正典順序キー化 + 時刻系 finding 棚卸し

> セッション固有の走行メモ (2026-07-22)。工程の真実は [audit_remediation_plan.md](../overview/audit_remediation_plan.md)、finding の消し込み状態は [code_review_ledger.md](../overview/code_review_ledger.md) が持つ。

## セッションスコープ (まはー指示)

- **S7** (SEA 監査 P2): 秒精度 timestamp による anchor 境界・履歴順の破れ → thread 内単調 sequence を正典順序キーに
- **監査横断の時刻系 finding の棚卸し**
- 完了条件: 同一秒衝突の回帰固定 (anchor 境界 / pagination)

## 棚卸し結果 (監査横断の時刻系 finding)

| finding | 出所 | 状態 (2026-07-22 裏取り) |
|---|---|---|
| S7 秒精度 timestamp の anchor 境界・履歴順 | SEA 監査 P2 | **本セッションで消し込み** (下記) |
| A3 host/City 時差 (発火後に host 時計へ戻る) | 自律行動監査 P1 | **未修正・残存**。`saiverse/autonomy_wiring.py` に timezone 処理なしを確認。host=City 同一 TZ の現行構成では潜伏。柱6 スコープの残り — 共通 clock helper + persona-local 比較 + EventScheduler 直前変換の設計から必要 (監査の修正方針参照)。**配置 (W8 継続 or 分離) はまはー裁定待ち** |
| occupancy event_key の同一秒衝突 | 分離監査 P2-1 | 消し込み済み (W7: 台帳 execution_id 採番) |
| main DB backup 名のミリ秒衝突 | migration 監査 P2 | 消し込み済み (第二陣: `database/backup.py:69-77` uuid4 suffix + collision_index を裏取り) |
| 深夜帯コマの sanitize sort/丸め変形 | 自律行動監査 | 消し込み済み (回帰 3 件を HEAD で再実行済みの記載を確認) |
| day_close 終端の半開区間で life 記帳漏れ | 自律行動監査 | 消し込み済み (台帳の自律行動行の残は A3/A10 のみ) |

## S7 の設計判断

- **正典順序キー = `(created_at, rowid)` 昇順** (thread 内 total order)。
  - `created_at` = 意味時刻 (秒精度)。インポート・移植で過去時刻の行が後から入っても歴史位置に並ぶ (rowid 単独を正典にしない理由)。
  - `rowid` = 同一秒内の挿入順 tie-breaker。クエリ時にその場で解決し**永続化しない** — VACUUM の再採番 (順序保存) にも、volatile 削除後の rowid 再利用 (残存行より必ず大きい側) にも安全。
  - 先行事例: W1 の episode 読み口 (`adapter.get_messages_by_origin_episode`) が既に同形。
- **anchor 境界 = anchor 行の `(created_at, rowid)` 以上のキーセット** (`created_at > ts OR (created_at = ts AND rowid >= rid)`)。anchor 自身を一度だけ含む。anchor 行不在 / created_at NULL は空 (旧実装のサブクエリ NULL と同挙動)。別 thread の anchor 行も旧実装同様に境界として許容 (thread 切替の耐性維持)。
- **W4 evict 境界 (`created_at < boundary_epoch`) は据え置き** — 同秒・anchor より古い行は「窓から退役するが今回は編纂されない」繰延で、次回 Metabolism で境界が進めば編纂される (消失ではない。W4 実装時に保守側と明記済みのコメントあり)。
- **rowid 単独だった会話窓・範囲クリップも正典キーセットへ統一** (`get_messages_around` / `get_conversation_window_around` / `get_conversation_messages_between`) — 過去時刻の後挿入行が「後」側に化ける食い違いの解消。
- **native export の書き出し順を正典化** — 移植先は export 順に INSERT するため、ここが同一秒内で不定だと移植先の rowid tie-breaker が元 DB の履歴順と食い違う。

## 変更ファイル

- `sai_memory/memory/storage.py` — 本丸。正典順序キーのモジュールコメント + `get_messages_from_id` キーセット化 + 全 messages/pulse_logs クエリの tie-breaker (`get_messages_last` / `get_messages_paginated` / `get_messages_by_resource` / `get_all_messages_for_search` / `get_embeddings_for_scope` / `get_messages_around` / `get_messages_around_timestamp` / `get_messages_with_persona_in_audience` / `sample_messages` / Stelis 冒頭3件 / `get_messages_for_chronicle` / `get_conversation_messages_between` / `get_conversation_window_around` / keyword 検索 / `get_pulse_logs_by_pulse` / `list_pulse_ids` / `get_messages_without_embeddings`)
- `saiverse_memory/native_export.py` — export 順の正典化 (移植の順序決定性)
- `sai_memory/memopedia/generator.py` / `sai_memory/arasuji/storage.py` — 窓・一覧の tie-breaker
- `saiverse/user_conversation_preserver.py` / `saiverse/uri_resolver.py` / `builtin_data/tools/get_memory_weave_context.py` — 履歴読み出しの tie-breaker
- `api/routes/people/{activity,arasuji,storage_layers,pulse_timeline,memopedia}.py` — 表示系の tie-breaker
- `tests/test_time_order_canonical_w8.py` — 新設回帰 14 件

## 回帰 (tests/test_time_order_canonical_w8.py + test_native_import_separation.py 追加分)

- anchor 境界: 同一秒群中央の anchor で evicted prefix が再混入しない / 群の先頭・末尾 anchor / 秒跨ぎ / anchor 不在で空 / 同秒順序 = 挿入順
- pagination: ページ境界を同一秒群の中央に置いても重複・欠落なし / `get_messages_last` の同秒 tail
- backdated 挿入が created_at の歴史位置に並ぶ (pagination / anchor の両方)
- Chronicle の全順序が pagination と一致
- 窓・範囲: `get_messages_around` 同秒 / backdated 隣人が「前」側 / 範囲クリップの同秒端点 (逆順渡し含む) / 会話窓の同秒
- NULL created_at: NULL anchor の周辺・anchor 起点・範囲端点 / NULL 行の先頭側整列と materialize (created_at=0)
- export/import 往復: スレッド横断の同一秒交互順序の保存 / seq なし旧 archive のフォールバック

## Codex レビュー

- **一巡目: 1 件 (P2)・受諾** — native export/import の往復でスレッド横断の同秒順序が保存されない (export/import が thread 単位のため、同一秒に `A1, B1, A2` と交互記録された履歴が復元後 `A1, A2, B1` になり、`get_messages_for_chronicle` 等のグローバルクエリの正典順が復元前後で不一致)。
  - 修正: export の各 message に元 rowid を `seq` として持たせ (additive・旧 importer は無視)、import を「第一段 = thread 器のスキャフォールド全件 → 第二段 = 全 thread 横断の (created_at, seq) 正典順で message INSERT」の二段に再構成。単一トランザクション契約は不変。`seq` なし旧 archive は archive 出現順 (= 旧挙動) に自然フォールバック。
  - 回帰: `test_roundtrip_preserves_cross_thread_same_second_order` / `test_import_without_seq_falls_back_to_archive_order` (test_native_import_separation.py)。ロールバック回帰は新契約 (`_insert_message_in_txn` の失敗) に書換。
- **二巡目: 2 件 (P2×2)・受諾 1 / スコープ外 1**
  - 受諾: **NULL created_at 行の周辺検索退行** — native import は created_at 欠落行を明示的に受け入れるのに、一巡目実装で入れた `is None → 空/None` ガードが、そういう行を anchor にした `get_messages_around` / 範囲クリップ / 会話窓を常に空にする (旧 rowid 検索では取得できていた)。修正: NULL の正典順位置を「全ての実時刻より前 (NULL 群は rowid 順)」= SQLite の ORDER BY ASC の NULL 並びと同一に定義し、境界句ヘルパー `_canonical_after_clause` / `_canonical_before_clause` (NULL anchor 対応) に 5 関数を載せ替え。`_row_to_message` の `int(None)` クラッシュ経路も NULL→0 写像で修正。回帰 4 件追加 (NullCreatedAtTest)。
  - スコープ外: gemini.py:2152 の InvalidRequestError が `generate_stream` の広域 except で汎用 LLMError に再包装される件 — **W8 の差分ではなく、working tree に同居していた前セッションの未コミット Gemini 作業への指摘**。W8 コミットには含めず、まはーへ申し送り (該当作業のオーナーセッションで対応)。
- **三巡目: 2 件 (P2×2)・受諾 1 / スコープ外 (再掲) 1**
  - 受諾: **部分 native import で保持スレッドとの同秒相対順が壊れる** — `thread_suffixes` / `export_thread_by_id` の部分 export を同一 DB へ replace 再 import すると、再挿入行の rowid が保持スレッドの既存行より必ず大きくなり、同一秒の交互順序 (`A1, B1, A2` → `B1, A1, A2`) が変わる。修正: archive の `seq` (元 rowid) が移植先で全て空いているとき (全 thread 復元・部分往復・空 DB への移植 — replace 削除後の同一 tx 内で検査) は**明示 rowid で挿入**して完全復元、一つでも欠け・重複・衝突があれば追記順に**全件**フォールバック (部分的な明示 rowid は中途半端な並びを作るのでやらない)。回帰 2 件追加 (部分往復の順序保存 / 衝突フォールバックが既存行を潰さない)。
  - スコープ外 (二巡目と同一): gemini.py:2152 — 前セッションの未コミット Gemini 作業への指摘 (再掲)。
- **四巡目: W8 差分への指摘ゼロで収束** — 残った 1 件は gemini.py (スコープ外・再掲) のみ。「その他の確認した変更と対象テストには、明確な破壊的問題は見つかりませんでした」。
- 合計: **受諾 3 / スコープ外 (前セッション Gemini 作業) 1**。対象は 1→2→2→1(スコープ外のみ) と単調収束。

## まはーへの申し送り (W8 スコープ外)

- working tree に前セッションの未コミット Gemini 作業 (`llm_clients/gemini.py` / `sea/runtime_llm.py` / `builtin_data/models/gemini-3.*.json` / `tests/test_gemini_latest_contract.py`) が同居している。W8 コミットには含めていない。
- Codex がその作業に **P2 を 1 件** 出している: 最新契約モデルで「非空の model turn 終端」を検出して投げる `InvalidRequestError` ([gemini.py:2152](../../llm_clients/gemini.py) 付近) が、**streaming 経路では** `generate_stream()` の包括 `except Exception` → `_convert_to_llm_error()` で汎用 `LLMError` に再包装され、`invalid_request` コードとユーザー向け説明が失われる。既存 `LLMError` はそのまま再送出する必要がある — 当該作業のセッションで対応を。

## まはー実機検証の観点

- 通常運転の無変化 (履歴表示・会話継続・Metabolism)
- 高速 tool round / spell loop 直後の Metabolism で、直前に評価済みの同秒メッセージが窓へ再出現しないこと
- 履歴 UI のページ送りで同秒群の重複・欠落がないこと
