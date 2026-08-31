# Changelog

SAIVerse の主要な変更点を記録する。日付は `YYYY-MM-DD` 形式。

## [Unreleased] / v0.3.0 (development)

### Changed

- **Memopedia の編集履歴 (rollback) の保存形式を変更**: 編集履歴に「編集前の title / summary / content の完全スナップショット」(`before_title`, `before_summary`, `before_content` カラム) を保存するようにした。従来は unified diff から編集前の状態を再構築していたが、長いページの局所編集に対して rollback すると、ハンク外の本文が欠落してページが破損する問題があった。スナップショット保存に切り替えたことで、どのような編集パターンでも確実に rollback できるようになった。
  - **互換性についての注意**: v0.3.0 より前に記録された編集履歴 (`before_*` カラムが NULL のエントリ) は rollback できなくなる。該当する履歴に対して rollback を試みた場合、エラーログを残して何もせず終了する。履歴の参照 (diff の表示) は引き続き可能。
  - 関連ファイル: `sai_memory/memopedia/storage.py`, `sai_memory/memopedia/core.py`
