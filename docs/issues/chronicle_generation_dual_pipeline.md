# Chronicle 生成パイプラインの二重実装

## 状態: 解決済み (2026-05-28)

## 問題

Chronicle 生成処理が2系統存在し、フィルタ条件やロジックの同期が取れていない。

## 発見経緯

2026-05-28: Memopedia 改善作業中に、Chronicle のソースメッセージ表示にシステム通知（`event_message` タグ付き）が混入していることを発見。調査の結果、`build_arasuji_core.py` 側のメッセージ取得クエリに `event_message` 除外フィルタが抜けていたことが判明。

さらに、メッセージ取得の先にある Chronicle 生成パイプライン自体が2系統に分かれていることが判明した。

## 解決内容

系統 B (CLI) の独自パイプラインを廃止し、系統 A と同じ `ArasujiGenerator.generate_unprocessed()` に統合した。

### 削除した独自実装

- `split_message_batches()` — 単純な等分割（`generate_unprocessed()` の contiguous run 分割に統合）
- `generate_level1_batches()` — 独自 Lv1 生成ループ（`generate_from_messages()` に統合）
- `consolidate_levels()` — 独自統合ループ（`generate_from_messages()` 内の `maybe_consolidate()` に統合）
- `log_processing_summary()` — 独自統計出力（簡潔なログに置換）
- `--maintain-interval` — レガシー機能として削除

### 統合で得られた機能

CLI / UI 経由の Chronicle 生成が runtime (Metabolism) と同じパイプラインを通るようになった:

- **既処理判定**: `source_ids_json` による正確なメッセージ単位の重複排除
- **contiguous run 分割**: 処理済みメッセージで分断された「島」の個別処理
- **gap-fill / dismantle**: 既存 Lv2 階層との整合性維持
- **Memopedia 連携**: `entity_extractor.make_batch_callback` (Fragment ベース) に統一
  - 旧 `extract_knowledge` (Page ベース) の呼び出しを廃止

### 維持した CLI 固有機能

- `--offset` / `--limit`: メッセージ取得段階で範囲を絞り、その範囲を `generate_unprocessed()` に渡す
- `--no-timestamp`: `ArasujiGenerator(include_timestamp=False)` で渡す
- `--debug-log`: `generator.debug_log_path` に設定

## 関連ファイル

- `sai_memory/memory/storage.py` — `get_messages_for_chronicle()`, `CHRONICLE_EXCLUDED_TAGS`
- `sea/runtime.py` — runtime 側の呼び出し元（`_generate_chronicle`）
- `scripts/arasuji/build_arasuji_core.py` — CLI 側（統合後）
- `sai_memory/arasuji/generator.py` — `ArasujiGenerator`, `generate_unprocessed()`, `generate_from_messages()`
- `sai_memory/memory/entity_extractor.py` — `make_batch_callback()` (Memopedia 連携の統一実装)
