# 一部の Memopedia 書き込みがアダプタのロックを取らず、追記を落としうる

**状態**: 実装済み・実機検証待ち（2026-08-05 起票 / 2026-08-06 実装）
**方向**: 漏れている生成箇所へ `db_lock=adapter._db_lock` を渡す（まはー裁定 2026-08-05）
**誰待ち**: まはー（実機で Metabolism の抽出が通ることの確認）

## 何が起きるか

`Memopedia` はコンストラクタで `db_lock` を受け取れる。**渡さないと自分で新しい `RLock` を作る**（`sai_memory/memopedia/core.py:69`）。同じ DB 接続（`adapter.conn`）を使っていても、生成箇所ごとに別のロックで守られることになり、**排他が成立しない**。

同じ接続へ他所から `commit()` が入ると、開いているトランザクションが途中で確定する。書き込みが競合すれば `SQLITE_BUSY` になる。

**とくに `entity_extractor` が危ない。** 追記が失敗しても、バッチ callback は例外を警告ログにして戻るだけで、再試行も永続キュー化もしない。**ペルソナの記憶追記が黙って落ちる。**

## 現況

生成箇所の大半は既に渡している。**規約は存在していて、数箇所が漏れている**。

| 渡している | 渡していない |
|---|---|
| `saiverse/memory_atlas.py`（8 箇所）<br>`saiverse/uri_resolver.py:968`<br>`sai_memory/curation_ops.py:1126`<br>`builtin_data/tools/memopedia_note.py:55` | **`sai_memory/memory/entity_extractor.py:422`**<br>**`api/routes/people/memopedia.py:36, 796`**<br>`sai_memory/memopedia/generator.py:397`<br>`sai_memory/arasuji/estimate.py:130` |

`scripts/` 配下の単体スクリプトは他の書き手と同居しないので対象外。

## なぜ今すぐ直さないか

`entity_extractor` は素の `conn` だけを受け取る（`apply_extraction_to_memopedia` などの公開関数のシグネチャにロックが無い）。渡すには呼び出し経路を通す必要があり、共有コードへ波及する。**本文 → Fragment 変換の案件（[intent](../intent/memopedia_body_to_fragment.md)）とは別の工事**として切り出す。

## どう直したか（2026-08-06 実装）

1. **`entity_extractor`**: `extract_and_reflect` / `make_batch_callback` に `db_lock` を通し、`Memopedia(conn, db_lock=...)` に。呼び出し元の Metabolism（`sea/session_lifecycle.py`）が `adapter._db_lock` を渡す
2. **`api/routes/people/memopedia.py`**: `_get_memopedia` は `adapter._db_lock` を直接渡す。二つのバックグラウンドワーカー（ページ生成 / ログからの一括構築）は**専用接続**を開くため、起動ルートで `_adapter_db_lock(manager, persona_id)` を解決して引数で渡す —— 接続が別でも、ロックを尊重する書き手同士は直列化される（debug.py の `_memopedia_session` と同じ流儀）
3. **`generator.py` / `estimate.py`**: `db_lock` パラメータを追加し、届く呼び出し元（API ルート）から渡した。`scripts/` 配下は単独 DB なので渡さない（従来どおり）
4. **失敗を失敗として扱う**（本体）: 握り潰しは三層あった —— callback 自身の `warning`、`executor.execute_plan` の `exception`＋continue、`bands._fire_identity_fragment_callbacks` の `exception`＋continue。callback の握り潰しを撤去し、executor は `ExecutionResult.extraction_failures` に entry id を記録、bands は out-param（`extraction_failures` リスト）で同じ器に積む。Metabolism は完了時に **ERROR ログ + 実行台帳の result + 画面通知**で表へ出す

**失敗を「Chronicle の failed」に畳み込まなかった理由**: チャンクは確定済みで、failed 再実行しても `source_ids` の冪等スキップにより抽出は再発火しない。丸ごと failed にすると「Chronicle は成功したのに失敗と記録され、再実行しても抽出は戻らない」という嘘の状態になる。だから成否は分離し、失敗の事実だけを消えない形で残す

5. **拾い直し**（まはー裁定 2026-08-06: 「失敗した部分は次回実行で再処理される」が期待される姿）: 失敗した entry id を付箋テーブル `entity_extraction_backlog`（persona memory.db）に貼り、**次の Metabolism の頭**で entry の `source_ids` からメッセージを読み直して同じ抽出をやり直す。成功したら付箋を剥がす。やり直しは 3 回まで（壊れたデータで毎晩 LLM 課金し続けない天井）—— 上限後も付箋は剥がさず残し、WARN で見え続ける（黙って諦めない）。entry や元メッセージが消えていて拾いようのない付箋だけは剥がして進む

## 経緯

- **2026-08-05**: 本文 → Fragment 変換の Codex レビュー（四〜七巡目）で繰り返し指摘された。変換は専用接続で `BEGIN IMMEDIATE` を張るため、その間に抽出器の書き込みが `SQLITE_BUSY` になりうる
- 変換側で回避する案（自律稼働が止まっているときだけ変換を許す）も検討したが、**まはー裁定で「抽出器側の問題」として切り分け**。呼び出し側で避けるのではなく、欠陥のある側を直す
- **2026-08-06**: 上記 1〜4 を実装。契約テスト（callback は握り潰さない / db_lock が Memopedia まで届く / executor が entry id を記録する）を追加
