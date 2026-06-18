# arasuji_entries: delete_incomplete_entries が Lv2 の source_ids を更新しない

## 現象

Chronicle 生成時に大量の WARNING が出る:

```
Source entry <uuid> not found for entry <short_id>
```

Lv2 エントリの `source_ids` が参照している Lv1 エントリが `arasuji_entries` テーブルに存在しない。

## 原因

`sai_memory/arasuji/storage.py` の `delete_incomplete_entries()` は、Track Chronicle の再生成前に `is_incomplete = 1` の Lv1 エントリを一括削除する。

```python
cur = conn.execute(
    "DELETE FROM arasuji_entries "
    "WHERE origin_track_id = ? AND level = 1 AND is_incomplete = 1",
    (origin_track_id,),
)
```

しかし、削除対象の Lv1 がすでに Lv2 に consolidated されていた場合、Lv2 側の `source_ids_json` から該当 ID を除去する処理がない。結果、Lv2 が孤立参照（dangling reference）を持つ。

呼び出し元: `generator.py` の `generate_unprocessed()` (line 1192-1194)。

## 影響

- `regenerate_consolidated_content()` 実行時に WARNING ログが大量発生
- Chronicle 生成が遅延し、ユーザーへの応答がブロックされる（同期実行のため）
- データ破損ではないが、不要な LLM 呼び出しコストが発生する可能性

## 修正方針

`delete_incomplete_entries()` で削除する前に、対象エントリの ID を収集し、Lv2 以上のエントリの `source_ids_json` から該当 ID を除去する。`delete_entry_and_update_parent()` が既に同様のパターンを実装しているので参考にできる。

## 発見日

2026-06-13
