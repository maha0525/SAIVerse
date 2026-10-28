# runtime_marker の os.kill(pid, 0) は Windows で相手を終了させうる

**状態**: 未解決 (2026-10-28 起票)

## 事実

`saiverse/runtime_marker.py` の `_marker_state` は、runtime マーカーの pid が生きているかを確かめるとき、psutil で `Process(pid).create_time()` が取れない場合 (例: AccessDenied) の代替として `os.kill(pid, 0)` を使っている。POSIX では signal 0 は「送らずに存在確認」だが、**Windows の `os.kill` は CTRL_C_EVENT / CTRL_BREAK_EVENT 以外の値を終了コードとして `TerminateProcess` を呼ぶ**ため、生存確認のつもりでそのプロセスを終了させる可能性がある (Python 公式ドキュメントの os.kill の Windows の項)。

発見の経緯: 2026-10-28、システム通知一本化 (docs/intent/system_notice_delivery.md) のレビュー消し込み中に、移行の同時起動の検査を書いたサブエージェントが隣で見つけた。この経路に入るのは psutil が失敗したときだけで、通常は踏まない。

## 影響

別プロセスが同じ DB を持っているかの判定 (CITY_SLUG の自動リネームの拒否など) の途中で、判定対象の SAIVerse プロセス (または pid を使い回した無関係のプロセス) を殺しうる。

## 未裁定

直し方は決めていない。候補は「Windows では os.kill の代替を使わない (psutil が失敗したら不明として安全側に倒す)」など。
