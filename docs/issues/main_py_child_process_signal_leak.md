# main.py が起動する子プロセスが、割り込み信号を親へ漏らしうる

**状態**: 未着手 (2026-09-16 記録)。実害は未観測。直すには停止の仕組みの変更が要るため、単独では着手していない。

## 何が起きうるか

`main.py` がバックエンドと画面を起動している 2 箇所 (`cleanup_and_start_server` / `cleanup_and_start_server_with_args`) の `subprocess.Popen` は、標準入出力もプロセスグループも指定していない。Windows では、この形で起動した子プロセスは親のコンソールとプロセスグループをそのまま共有する。

この状態で子プロセス側に割り込み信号が発生すると、信号が**プロセスグループを遡って親まで届く**。SAIVerse を IDE (Antigravity / Claude Code) のターミナルから起動している場合、その親には IDE 自身のプロセスが含まれる。

## 根拠 (実証済み)

同じ欠陥が `tests/test_runtime_marker_failclosed.py` にあり、2026-09-15 に実害として現れた。Antigravity からフルテストを回すと、起動 40〜60 秒後に Antigravity の `language_server.exe` が死に、テストのプロセス一族が同一ミリ秒で全滅していた。OS にはメモリ不足もクラッシュも記録が残らない。

修正として `stdin/stdout/stderr=DEVNULL` と `creationflags=CREATE_NEW_PROCESS_GROUP` を同時に入れたところ、フルスイート 6,303 件が完走した。さらに `creationflags` の 1 行だけを外して回し直したところ**再び落ちた** (2026-09-16 実測、`language_server.exe` が 00:46:07.920 に同一ミリ秒で全滅)。つまり効いていたのは**プロセスグループの分離**であって、入出力の切り離しではない。

## なぜ今すぐ直していないか

**引き金の条件が違う。** テストの子プロセスは、親 (pytest) が生きている間に次々と終了するため、信号の発生機会が繰り返し訪れる。一方 `main.py` が起動するバックエンドと画面は、SAIVerse が動いている間ずっと生き続け、死ぬのは SAIVerse 自身が終わるときだけ。そのため今日まで実害は観測されていない。

**直すと停止の仕組みが変わる。** `CREATE_NEW_PROCESS_GROUP` を付けると Ctrl+C がバックエンドに届かなくなり、`shutdown_subprocess` の `terminate()` による強制終了だけが残る。SAIMemory (SQLite) への書き込み中に強制終了されると、データが壊れうる。安全に直すには、起動時のグループ分離と合わせて、**終了時に `CTRL_BREAK_EVENT` を送って穏やかに止める経路**を用意する必要がある。

## 直すときにやること

1. 2 箇所の `Popen` に `stdin=subprocess.DEVNULL` と、Windows なら `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` を付ける。
2. `shutdown_subprocess` に、Windows で `os.kill(pid, signal.CTRL_BREAK_EVENT)` を先に試し、待ってから既存の `terminate()` に落ちる経路を足す。
3. **実機検証が必須**: Ctrl+C で SAIVerse を止めて、バックエンドが穏やかに終了すること (ログの `[shutdown]` 行が出ること) と、SAIMemory が壊れないことを確認する。
4. 同じ検査を `saiverse/addon_installer.py:127` にも当てる (ここは `stdout`/`stderr` は指定済みだが `stdin` とグループが未指定)。

他の `Popen` 箇所 (`api/routes/system.py` の 3 箇所、`llm_clients/llama_server.py:293`、`scripts/update_engine.py:1011`) は既にグループ分離と入出力の指定が入っており、対処済み。
