# runtime_marker の os.kill(pid, 0) は Windows で相手を終了させうる

**状態**: 修正 PR 待ち (2026-10-28 起票 / 2026-11-14 修正実装)

## 事実

`saiverse/runtime_marker.py` の `_marker_state` は、runtime マーカーの pid が生きているかを確かめるとき、psutil で `Process(pid).create_time()` が取れない場合 (例: AccessDenied) の代替として `os.kill(pid, 0)` を使っている。POSIX では signal 0 は「送らずに存在確認」だが、**Windows の `os.kill` は CTRL_C_EVENT / CTRL_BREAK_EVENT 以外の値を終了コードとして `TerminateProcess` を呼ぶ**ため、生存確認のつもりでそのプロセスを終了させる可能性がある (Python 公式ドキュメントの os.kill の Windows の項)。

発見の経緯: 2026-10-28、システム通知一本化 (docs/intent/system_notice_delivery.md) のレビュー消し込み中に、移行の同時起動の検査を書いたサブエージェントが隣で見つけた。この経路に入るのは psutil が失敗したときだけで、通常は踏まない。

## 影響

別プロセスが同じ DB を持っているかの判定 (CITY_SLUG の自動リネームの拒否など) の途中で、判定対象の SAIVerse プロセス (または pid を使い回した無関係のプロセス) を殺しうる。

## 対応 (2026-11-14)

- `saiverse/runtime_marker.py` に `probe_process_alive(pid)` を新設し、Windows (`sys.platform == "win32"`) では `os.kill(pid, 0)` を一切呼ばず、`ctypes` 経由で `kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` と `GetExitCodeProcess` を使って生存確認を行うようにした。`ERROR_INVALID_PARAMETER` または `exit_code != STILL_ACTIVE` のみ `"dead"`、それ以外のアクセス拒否や判定不能なエラーは安全側 (`"unknown"`) に倒す。`pid <= 0` は `"dead"` とする。
- 同族の `sai_memory/backup.py` の `_is_process_alive(pid)` も `os.kill(pid, 0)` を直接呼んでいたため、`probe_process_alive(pid) != "dead"` を使う形に統一した。
- `tests/test_runtime_marker_failclosed.py` で、Windows 上で `os.kill` を呼ばずに生存中の子プロセスを殺さないこと、および `psutil` 不在時・`backup._is_process_alive` の回帰テストを追加・更新した。
