# Chronicle 生成パイプラインの二重実装

## 問題

Chronicle 生成処理が2系統存在し、フィルタ条件やロジックの同期が取れていない。

## 発見経緯

2026-05-28: Memopedia 改善作業中に、Chronicle のソースメッセージ表示にシステム通知（`event_message` タグ付き）が混入していることを発見。調査の結果、`build_arasuji_core.py` 側のメッセージ取得クエリに `event_message` 除外フィルタが抜けていたことが判明。

さらに、メッセージ取得の先にある Chronicle 生成パイプライン自体が2系統に分かれていることが判明した。

## 2系統の所在

### 系統 A: runtime.py（Metabolism 経由）

```
sea/runtime.py: _generate_chronicle()
  → get_messages_for_chronicle()  ← 共通化済み
  → ArasujiGenerator.generate_unprocessed()
    - 既処理メッセージの判定（source_ids_json で processed_ids 算出）
    - contiguous run 分割（処理済みメッセージで分断されたグループ判定）
    - qualifying run フィルタ（batch_size 未満の孤立 run をスキップ）
    - gap-fill 判定（既存 Lv-2 のカバー範囲内の Lv-1 を統合）
    - incomplete entry 管理（Track Chronicle 用）
    - batch_callback（Memopedia entity 抽出）
```

### 系統 B: build_arasuji_core.py（CLI / UI 経由）

```
scripts/arasuji/build_arasuji_core.py: run_cli()
  → fetch_messages() → get_messages_for_chronicle()  ← 共通化済み
  → split_message_batches()  ← 独自実装、単純な等分割
  → generate_level1_batches()  ← 独自実装、ArasujiGenerator の generate_from_messages 直呼び
  → consolidate_levels()  ← 独自実装
  → memopedia_batch_callback（旧 extract_knowledge 経由、entity_extractor ではない）
```

## 具体的な差異

| 機能 | 系統 A (runtime) | 系統 B (build_arasuji_core) |
|------|------|------|
| メッセージ取得 | `get_messages_for_chronicle()` | `get_messages_for_chronicle()` |
| 既処理判定 | `source_ids_json` で自動判定 | なし（offset/limit で手動制御） |
| contiguous run 分割 | あり | なし（全メッセージを等分割） |
| gap-fill | あり | なし |
| incomplete entry | あり（Track Chronicle） | なし |
| Memopedia 連携 | `entity_extractor.make_batch_callback` | 旧 `extract_knowledge`（別実装） |
| バッチサイズ | env `MEMORY_WEAVE_BATCH_SIZE` | CLI 引数 `--batch-size` |

## 今回の応急処置

- メッセージ取得を `sai_memory.memory.storage.get_messages_for_chronicle()` に一元化
- `CHRONICLE_EXCLUDED_TAGS` 定数でフィルタ条件を一箇所管理
- 両系統から共通関数を呼ぶように修正済み

## 必要な対応

系統 B を系統 A の `generate_unprocessed()` に統合する。CLI / UI 経由でも同じパイプラインを通るようにする。

### 注意点

- 系統 B は `--offset` / `--limit` で範囲指定する機能がある。`generate_unprocessed()` にこのインターフェースがない
- 系統 B の `--with-memopedia` は旧 `extract_knowledge` を使っており、現行の `entity_extractor` と別実装。これも統合対象
- 系統 B の `--maintain-interval` による定期メンテナンス（merge_similar, fix_markdown）は系統 A にない
- 系統 B はクエリの `LIMIT ? OFFSET ?` で範囲を絞るが、`get_messages_for_chronicle()` の共通化時にこの機能は維持済み

## 関連ファイル

- `sai_memory/memory/storage.py` — `get_messages_for_chronicle()`, `CHRONICLE_EXCLUDED_TAGS`
- `sea/runtime.py` — 系統 A の呼び出し元（`_generate_chronicle`）
- `scripts/arasuji/build_arasuji_core.py` — 系統 B の全体
- `sai_memory/arasuji/generator.py` — `ArasujiGenerator`, `generate_unprocessed()`, `generate_from_messages()`
