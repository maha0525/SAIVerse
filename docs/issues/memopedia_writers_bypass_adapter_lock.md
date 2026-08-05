# 一部の Memopedia 書き込みがアダプタのロックを取らず、追記を落としうる

**状態**: 未解決（2026-08-05 起票 / 方向は確定、着手待ち）
**方向**: 漏れている生成箇所へ `db_lock=adapter._db_lock` を渡す（まはー裁定 2026-08-05）
**誰待ち**: 私（実装）

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

## どう直すか

1. `entity_extractor` の公開関数へ `db_lock` を通し、`Memopedia(conn, db_lock=...)` にする
2. `api/routes/people/memopedia.py` の 2 箇所を `Memopedia(adapter.conn, db_lock=adapter._db_lock)` にする
3. `generator.py` / `estimate.py` の呼び出し元にアダプタのロックが届くかを確認し、届くなら渡す
4. 抽出バッチの書き込み失敗を、警告ログで握り潰さず**失敗として扱う**（再試行か、少なくとも呼び出し元へ伝える）

**4 が本体。** ロックを渡しても、将来の書き手が取り忘れれば同じ穴が開く。失敗を失敗として扱う経路が無いことが、記憶が黙って消える理由そのもの。

## 経緯

- **2026-08-05**: 本文 → Fragment 変換の Codex レビュー（四〜七巡目）で繰り返し指摘された。変換は専用接続で `BEGIN IMMEDIATE` を張るため、その間に抽出器の書き込みが `SQLITE_BUSY` になりうる
- 変換側で回避する案（自律稼働が止まっているときだけ変換を許す）も検討したが、**まはー裁定で「抽出器側の問題」として切り分け**。呼び出し側で避けるのではなく、欠陥のある側を直す
