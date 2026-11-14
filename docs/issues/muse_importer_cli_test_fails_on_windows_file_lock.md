# Muse 取り込みの CLI 試験が Windows で後片づけに失敗する

状態: 修正 PR 待ち (2026-11-09 発見 / 2026-11-14 修正実装)

## 何が起きるか

`tests/test_muse_importer.py::MuseImporterEndToEndTest::test_cli_import_writes_fourteen_turns_plus_header` が、Windows のフルスイートで毎回落ちる。取り込み自体は成功していて (標準エラーに「15 件のメッセージを取り込みました」が出る)、落ちるのは試験の最後の後片づけ — 一時フォルダ (`tempfile.TemporaryDirectory`) を消すときに、`personas/muse-test/memory.db` が「別のプロセスが使用中」(`PermissionError` / WinError 32) で消せない。

## 分かっていること

- 2026-11-09、`fix/addon-restart-and-tool-stop` の作業中のフルスイートで見つけた。変更を入れていない develop 相当のフォルダ (`f0da661a`) でも、この試験だけを単独で流して同じく落ちることを確かめた。停止ボタンの変更とは関係ない。
- `memory.db` を開いた SQLite の接続 (SAIMemory のアダプタなど) が、試験の終わりまでに閉じられていない可能性が高い。どの接続が残っているかはまだ確かめていない。

## 原因と対応 (2026-11-14)

- 取り込み CLI (`scripts/import_muse_conversations.py`) 本体は `finally:` で `adapter.close()` を呼んでおり、接続を閉じ忘れていなかった。
- 原因はテスト側 (`tests/test_muse_importer.py` の `test_cli_import_writes_fourteen_turns_plus_header`) で `with sqlite3.connect(db_path) as conn:` を使っていたこと。Python の `sqlite3.Connection` の `with` ブロックはトランザクションの commit/rollback を行うだけで `conn.close()` を呼ばないため、`TemporaryDirectory` の後片づけ時にテスト自身が開いた接続が残り、Windows で `PermissionError` (WinError 32) になっていた。
- `contextlib.closing(sqlite3.connect(db_path))` で確実に接続を閉じるように修正し、`tests/test_muse_importer.py` 全 12 件が Windows 上でパスすることを確認した。
