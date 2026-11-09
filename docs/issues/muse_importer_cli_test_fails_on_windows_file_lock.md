# Muse 取り込みの CLI 試験が Windows で後片づけに失敗する

状態: 未着手 (2026-11-09 発見)

## 何が起きるか

`tests/test_muse_importer.py::MuseImporterEndToEndTest::test_cli_import_writes_fourteen_turns_plus_header` が、Windows のフルスイートで毎回落ちる。取り込み自体は成功していて (標準エラーに「15 件のメッセージを取り込みました」が出る)、落ちるのは試験の最後の後片づけ — 一時フォルダ (`tempfile.TemporaryDirectory`) を消すときに、`personas/muse-test/memory.db` が「別のプロセスが使用中」(`PermissionError` / WinError 32) で消せない。

## 分かっていること

- 2026-11-09、`fix/addon-restart-and-tool-stop` の作業中のフルスイートで見つけた。変更を入れていない develop 相当のフォルダ (`f0da661a`) でも、この試験だけを単独で流して同じく落ちることを確かめた。停止ボタンの変更とは関係ない。
- `memory.db` を開いた SQLite の接続 (SAIMemory のアダプタなど) が、試験の終わりまでに閉じられていない可能性が高い。どの接続が残っているかはまだ確かめていない。

## 次の一手

試験の中で取り込みが開いた `memory.db` の接続を探し、後片づけの前に閉じる (試験の側で閉じるのか、取り込みの CLI が閉じ忘れているのかを先に見分ける)。CLI が閉じ忘れているなら、実際の利用者の取り込みでもファイルが掴まれたままになっていないかを確かめる。
