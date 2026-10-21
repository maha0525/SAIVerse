# import_persona_pack をサーバーを通さず素の DB に直接使うと、標準データが黙ってスキップされる

- **状態**: 未解決 (2026-10-22 起票。PR #410 の Codex レビュー指摘 P2。実害なしと判定して次バージョン以降へ)
- **関連**: `saiverse_memory/persona_pack/importer.py` / `docs/intent/persona_pack.md`

## 指摘の中身 (Codex、PR #410)

取り込みの関数 `import_persona_pack(mode="new")` を、`init_db()` だけで作った素の memory.db
(SAIMemoryAdapter を一度も起動していない状態) に対して直接呼ぶと、`perception_batches` /
`perception_buffer` / `desk_items` などの標準テーブルが存在しないため、`stage_ext_memory()` が
標準のデータまで「取り込み先に表がない」としてスキップする。結果は成功扱いのまま
added=0 / skipped=N になる。

## 実害なしと判定した根拠

- この関数を呼ぶ口は製品の中に一つだけ (`api/routes/people/persona_pack.py` のサーバー route)。
  画面の「迎える」「合流」はすべてここを通る。
- サーバー経由ではテーブルが揃った状態で取り込みが走る。2026-10-21 のエリスのパック (1.47GB)
  の実測で、perception_batches 4 件・perception_buffer 3 件・desk_items 1 件が実際に追加された。
  テーブルが無い場合に出る「no table; N row(s) skipped」の警告も、これらの表については出ていない
  (出たのは `memopedia_body_conversion` だけ — そちらは [別 issue](persona_pack_drops_body_conversion_log.md))。
- つまり当たるのは、将来サーバーを通さずこの関数を直接使う新しいコード (スクリプト・他ツール) だけ。

## 直し方の候補

取り込みの初期化 (`importer.py` の約 1650 行、`_prepare_target_memory` 周辺) で、adapter の
起動に依存せず標準テーブルの作成を揃える。合わせて「標準のテーブルが無いのに成功扱い」を
警告からエラーに格上げするかも検討する。
