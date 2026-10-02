# 最新ではない ZIP から導入すると、中身は古い版のまま、更新もできなくなる

**状態**: 未着手 (2026-10-02 発見。v0.3.19 の発行当日、クリーンな Windows での ZIP インストールのテストを準備しているときに、まはーの「最新版をダウンロードしたのでは、アップデート先が無い」という指摘から見つかった)
**深刻度**: P2 — 新しい版が出たあとで、手元に残っていた古い ZIP から setup.bat を実行した利用者は、古い版のまま動き、update.bat も画面の「Update」ボタンも「手元に変更がある」と断って更新できない。最新の ZIP をダウンロードしてすぐ導入する利用者には起きない

## 何が起きるか (時間順)

1. 利用者が、ある版 (例: v0.3.18) の ZIP をダウンロードして解凍する。
2. そのあとで新しい版 (例: v0.3.19) が発行される。
3. 利用者が setup.bat を実行する。setup.bat は、ZIP を解凍したフォルダを Git の管理下に置くために、`git init` → `git fetch origin` → `git reset origin/main` を実行する (`setup.bat` の「9b. Initialize git repository」)。
4. `git reset origin/main` は、Git の記録上の現在地を main の最新 (v0.3.19) に合わせるが、フォルダの中のファイルは書き換えない。その結果、Git は「このフォルダは v0.3.19 だ」と記録しているのに、ファイルの中身は v0.3.18 のままになる。
5. Git から見ると、v0.3.18 と v0.3.19 のあいだで変わったファイルが全部「利用者が手元で書き換えたファイル」に見える。
6. update.bat や画面の「Update」ボタンは、更新の前に「手元に変更があるか」を調べ (`scripts/update_engine.py` の `assert_git_update_ready`)、変更があると更新を始めずに断る。Git の記録の上ではもう最新なので、更新する先も無い。

## 確かめたこと (2026-10-02、作業用フォルダでの再現)

- v0.3.18 のタグから ZIP と同じ中身を作り (`git archive`、リリースの自動処理と同じコマンド)、setup.bat と同じ順で `git init` → `git fetch` → `git branch -M main` → `git reset origin/main` を実行した。そのあと `VERSION` は 0.3.18 のまま、Git の現在地は v0.3.19 のマージコミットになり、`git status --porcelain --untracked-files=no` は 87 件の変更を返した。
- 同じ手順を v0.3.19 のタグから作った中身で実行すると、変更は 0 件だった。最新の ZIP から導入する道は、この問題を踏まない。

実際の ZIP と実際の setup.bat では、まだ確かめていない (再現は同じ Git のコマンドを手で実行したもの)。

## 誰が踏むか

- ZIP をダウンロードしてから setup.bat を実行するまでのあいだに、新しい版が発行された利用者。
- GitHub の画面でブランチを develop などに切り替えてから「Code → Download ZIP」で落とした利用者。このときは、ZIP の中身が main のどの版とも一致しない。ブランチを切り替えずに落とした ZIP は、既定のブランチである main の中身になるので、最新の ZIP と同じくこの問題を踏まない (README は「Code」ボタンからの落とし方も案内している)。

## 決めること

- setup.bat (と setup.sh) が、Git の記録上の現在地を「main の最新」ではなく「ZIP の中身と同じ版」に合わせるようにするか。ZIP の中身の版は `VERSION` ファイルで分かり、発行済みの版にはタグ (`v0.3.18` など) がある。合わせる先のタグが無い中身 (develop の ZIP など) のときにどうするかも決める。
- すでにこの状態になっている利用者を、どう救うか。

## 関連

- [git_required_for_zip_install.md](git_required_for_zip_install.md) — ZIP から導入した利用者に Git を自動で入れる仕組み。この issue は、その仕組みが Git を初期化するときの一行から来ている
- [update_refuses_on_tracked_local_changes_without_exit.md](archive/update_refuses_on_tracked_local_changes_without_exit.md) — 手元の変更で更新が断られたときの出口の件 (完了済み)
