# Memopedia 孤児 Fragment の再縁組み

**発見**: 2026-07-04（記憶アーキテクチャ v2 Phase 1 の実機検証中）
**状態**: air_city_a はバックアップの上で削除済み（同日、下記追記）。コード側の可視化改修（LEFT JOIN）は保険として維持。残課題はページ tombstone 化（農園設計時）

## 事象

air_city_a の実 memory.db で、`memopedia_fragments` 1638 件のうち **860 件（52%）の
`entity_id` が存在しないページを指していた**（親ページが物理削除済み）。
`is_deleted=1` のソフトデリートページに紐づく Fragment は 0 件——つまり親は
ソフトデリートではなく**行ごと消えている**。

孤児クラスタの例（entity_id 単位）:
- 163 件のクラスタ: まはーの誕生日（1月14日）等、人物系の記憶 → 旧「まはー」ページの残骸と推定
- 154 件のクラスタ: AI の主体的な記憶想起に関する考察系
- 65 件: ChatbotUI 構築期のデータベースエラー解析の記録

削除経路は未特定。現行の `delete_page`（`sai_memory/memopedia/storage.py:560-579`）は
Fragment もカスケード削除するため、これらの孤児は**それ以前の削除経路**
（旧整理スクリプト `build_memopedia_core.py` や直接 SQL 等）の産物と推定される（推測）。

## 実害（応急処置前）

`unified_recall` の Fragment 検索（埋め込み・キーワード両方）と埋め込み整備クエリが
`JOIN memopedia_pages` (INNER) だったため、孤児 Fragment は:
1. 埋め込み対象に列挙されない（永久に未埋め込み）
2. 埋め込まれていても検索結果に出ない

→ **記憶の半分が自動想起から構造的に不可視**だった。

## 応急処置（2026-07-04 実施）

- `sai_memory/unified_recall.py` の3クエリを `LEFT JOIN memopedia_pages` に変更
  （`get_fragment_embeddings` / `get_fragments_without_embeddings` / キーワードヒット付与）
- 埋め込みテキスト生成は entity_title 無しなら本文のみ
- `sea/auto_recall.py` の表示: 孤児は `[記憶の断片]` ラベル、深掘りハンドルは
  `memopedia_get_page`（ページが無いので不可）でなく `memory_recall_unified`

これで孤児 Fragment は本文単体で埋め込まれ、想起可能になる。

## 追記（2026-07-04 同日）: air_city_a の孤児は削除で決着

まはー判断: 孤児は旧版ページ由来で、現行ページの Fragment と内容が実質重複するため、
再縁組みすると**同じ記憶が二重に想起されるノイズ源**になる。よって再縁組みではなく
バックアップ→削除とした。

- バックアップ: `~/.saiverse/backups/memopedia_orphan_fragments_air_city_a_20260704_121019.json`
  （860件、全列、復元は JSON → INSERT で可能）
- 削除結果: fragments 1647 → 787、孤児 0、未埋め込み 0（生存 Fragment は全て埋め込み済み）

**他ユーザーの DB には孤児が残っている可能性がある**。LEFT JOIN 化により今後は
`[記憶の断片]` として想起に載る（沈黙はしない）。重複ノイズが問題になる場合の
点検・清掃手段（本ページのバックアップ→削除の汎用スクリプト化）は必要になったら。

## 併せて修正した近縁バグ（2026-07-04）

`unified_recall` のページ検索（埋め込み `get_memopedia_embeddings` / キーワード）が
`is_deleted` を見ておらず、**ソフトデリート済みページが想起に浮かび続ける**穴があった。
両経路に `is_deleted = 0 OR IS NULL` フィルタを追加済み。

## 残課題（農園構想側で対応）

1. **ページのソフトデリート徹底**: Fragment は墓標方式（intent doc §7.2 / §10-5）と
   決めたが、**ページ側も物理削除を廃止**しないと孤児は再生産される。農園スキーマ設計時に
   ページの tombstone 化を含めること
2. 削除経路の特定（どのコードが親を消したか）は、再発防止の観点で余裕があれば

## 関連

- `docs/intent/memory_architecture_v2.md` §7（Fragment 中心・墓標方式）、§9（農園構想）
