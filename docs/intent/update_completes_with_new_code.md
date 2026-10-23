# Intent Document: アップデートの作業を、新しいコードで最後まで行う

**ステータス: 検証待ち** (2026-10-23 まはー GO「それはやるべきだな。その設計でいい！」。実装と自動テストは済み、§6 の 5・6 の通し確認が残る)

## 芯 (専門用語なしの数行)

アップデートで入れ替えが必要な物は、すべて「本体と画面を止めた状態で、取ってきたばかりの新しいコードを使って入れ替える」。入れ替えの対象から漏れていた SearXNG も対象に含める。過去の古い更新処理で入れ替えが済んでいない環境では、次に start.bat で起動する前に一度だけ、今のフォルダの中身のまま入れ替えをやり直す (SAIVerse のバージョンは進めない)。うまくいかなかったら画面で知らせる。

## 1. なぜ必要か

v0.3.25 の発行直後、[PR #415 の Codex レビュー](https://github.com/maha0525/SAIVerse/pull/415)で、v0.3.24 以前からの更新では v0.3.25 で足した「pip 自身の更新」と「fastembed の削除」がその回に走らないことが判明した。更新を実行しているのが、新しいファイルを取ってくる前に読み込まれた古い `update_engine.py` だったため。

対処を議論する中で (2026-10-23)、一度は「再起動の後にしかできない作業」を番号つきの一覧で持つ新しい仕組み (アップデート再起動後処理) を設計しかけたが、コードを辿り直して二つの前提が誤っていたと分かり、取り下げた:

- **「新しいコードは再起動後にしか動かない」は #414 で既に解消されていた。** v0.3.25 以降の更新では、パッケージの入れ替え (`update_dependencies`) は、ディスクに取ってきた新しい `update_engine.py` の子プロセス (`--install-dependencies`) によって、本体と画面を止めた状態で行われる。今後の更新で足す作業は、その回の更新の中で実行される。実行されないのは v0.3.24 以前の古い `update_engine.py` で更新した回だけで、これは今後増えない範囲に限られる。
- **SearXNG は更新の対象に入っていなかった。** `update_engine.py` では SearXNG の停止も再起動も行っていなかった。SearXNG の入れ直しは、次に start.bat / start.sh から `run_searxng_server.ps1` / `.sh` で SearXNG を起動したときにだけ行われる。画面の「Update」ボタンで更新してウィンドウを開いたまま使う環境では、新しい SearXNG が反映されない。

リポジトリの設計方針 (救済の機構を足す前に、原因の穴を塞いで機構ごと不要にできないか先に問う) に従い、新しい概念は作らず、二つの穴を塞ぐ。

## 2. 利用者に約束すること (2026-10-23 まはーとの合意)

1. **アップデートの続きとしてしか、入れ替えは走らない。** 漏れた入れ替えのやり直しは「古い更新処理で更新された」と分かる環境でだけ行う。
2. **正常に一度済めば、二度と走らない。** 失敗した回は完了扱いにせず、次の機会に再挑戦する。
3. **新しく導入した環境では一度も走らない。**
4. **SAIVerse のバージョンを進めない。** 入れ替えのやり直しでは、今のフォルダの中身のまま `update_dependencies` を実行するだけで、新しいリリースを取りに行かない (2026-10-23、既存の「未完了の更新を `--manual` で最初からやり直す処理」の流用はこの理由でまはーが却下 — 起動しただけでバージョンが進むのは、やろうとした動作と違うことをやらかす)。
5. **うまくいかなかったら画面で知らせる。** ログに残すだけにしない (2026-10-23 まはー「イレギュラーなこと起きてんだったらユーザーに分かるように！鉄則！」)。起動は止めない。

## 3. 仕組み (出てくる名前と流れ)

- **`update_dependencies`** (`scripts/update_engine.py`) — 更新のたびに、pip 自身の更新 → `requirements.lock` の導入 → 使われなくなったパッケージ (今は fastembed) の削除 → `pip check` → `npm ci` を順に行う関数。
- **子プロセス (`--install-dependencies`)** — 更新の途中で、ディスク上の新しい `update_engine.py` を別プロセスとして起動し、`update_dependencies` を実行させる仕組み (#414)。
- **`--check-complete`** — start.bat / start.sh で本体を起動する前に行う検査。ディスク上の (= 新しい) `update_engine.py` で実行される。
- **`.update_complete`** — 更新が最後まで済んだときに SAIVerse のフォルダへ書かれる完了記録ファイル。

### 穴 1: SearXNG を更新の流れに入れる

更新の最初に、画面 (フロントエンド) と同じく SearXNG のサーバーも止める。入れ直しが必要かどうかの判定と入れ直し作業 (`searxng_install.py` の `ready` / `setup`) は、`update_dependencies` の一部として子プロセスの中で新しいコードの手順で行う。更新の最後に、止めた SearXNG を起動し直す。SearXNG を導入していない環境では何もしない。

### 穴 2: 古い更新処理で漏れた入れ替えを、start.bat の起動前に一度だけやり直す

- 子プロセスで `update_dependencies` を済ませたら、「どのバージョンのコードで、どのパッケージ一覧 (`requirements.lock` など) を入れ終えたか」を記録する。この記録は `.update_complete` とは**別のファイル**に書く — `.update_complete` は更新の最後に親プロセス (古い `update_engine.py` の場合がある) によって上書きされるため、新しいコードで足した項目は消されてしまうから。
- `--check-complete` の実行時、`.update_complete` が今のコードと一致しているのにこの記録が無い (または一致しない) 場合は「古い更新処理で更新された環境」と判定し、`--install-dependencies` を一度だけ実行して記録を書く。新しいリリースはダウンロードしない。
- `.update_complete` 自体が無い場合 (新しく導入した直後の環境) は、既存の「入っているパッケージを直接確かめる検査」を行い、問題なければ `update_dependencies` を実行せずにこの記録だけを書く (約束 3)。`.update_complete` が古い形式や壊れた中身で読めない場合も、既存の扱い (無い場合と同じに判定する) に揃えて、記録だけを書く。
- 同梱の Node.js の入れ替え (`refresh_portable_node`) も `update_dependencies` の中に移し、新しいコードの手順で行う。start.bat にあった「毎回の起動時に Node.js のバージョンを見比べる検査」は、このやり直し処理に置き換えて取り除く (まはー「全部まとめるべき。特別扱いすることで今後の更新でいつか影響が出る可能性を恐れてる」)。

### 失敗したときの画面表示

start.bat での入れ替えのやり直しは本体の起動より先に行われるため、失敗した場合はその内容を SAIVerse フォルダ内のファイルに書き残す。本体は起動時にそのファイルを読み取って `manager.startup_alerts` に追加し、画面上部のバナー (`GET /api/system/alerts`) で表示する (旧ログの取り込み失敗などで既に使っている仕組み)。次の機会に入れ替えが成功したら、そのファイルを消す。更新の中で同梱の Node.js の入れ替え (`refresh_portable_node`) か SearXNG の入れ直し (`refresh_searxng`) が失敗したときも、更新は止めずに、入れ替えを済ませた記録を書かないまま同じファイルに失敗を書くので、画面のバナーに出て、次の start.bat / start.sh で入れ替えがやり直される (成功するまで起動のたびに再挑戦する)。

### 実装の具体 (2026-10-23)

- **記録のファイル**: `update_dependencies` を済ませた記録は `.update_dependencies_complete` (中身は `.update_complete` と同じ `completion_fingerprint`)。書くのは子プロセス (`install_dependencies_main`) だけで、成功したときに限る。コードが動いた時点 (`_apply_code_and_dependencies`) で消してから、子が書き直す。失敗の記録は `.update_dependencies_failed.json` (バージョン・エラーの末尾・時刻、Node.js / SearXNG の失敗のときは失敗した作業の名前 `steps`)。どちらも `.gitignore` の `.update_dependencies_*` で除外する。子は成功すると、先に失敗の記録を消し、そのあと完了の記録を書く (逆の順だと、間で止まったときに消えないバナーが残る)。
- **`--check-complete` の終了コード**: やり直しが失敗したときは新しい終了コード 12 (`CHECK_DEPENDENCIES_FAILED`) を返す。start.bat / start.sh は 12 のとき警告を出して起動を続ける。警告には「画面が開かない場合も、インターネットを確かめて SAIVerse の窓をすべて閉じ、start.bat (start.sh) をもう一度」を含める — やり直しの `npm ci` が失敗すると画面のパッケージも欠けていて、バナーそのものが出せないため。やり直しは、このフォルダの画面のサーバーが動いていれば `npm ci` を走らせずに失敗として扱う (`--manual` と同じ理由: 動いている画面のサーバーの横で `npm ci` すると `node_modules` が空になる)。
- **画面のパッケージが欠けていても、やり直しは全体の更新に化けない (2026-10-23、PR #417 の Codex 四巡目)**: `.update_complete` が今のコードと一致していて、依存の記録が無い・一致しない、または失敗の記録があるときは、画面のパッケージが欠けていても終了コード 10 (= `--manual` の全体の更新。新しいリリースを取りうる) を返さず、`reinstall_dependencies_once` に回す。やり直しの `npm ci` は `node_modules` を空にしてから失敗するので、以前は「やり直しが一度失敗した次の起動」が判定の順番で全体の更新に切り替わり、約束 4 を破っていた。10 を返すのは、印と記録が一致して失敗の記録も無いのに部品が欠けているときだけ (コードの側の入れ替えは済んでいて、あとから部品が消えた場合)。
- **新しく導入した環境の記録の順番**: `.update_complete` が無い分岐では、依存の記録を先に書き、書けたときだけ `.update_complete` を書く。逆の順で間に止まると、次の起動が「古い更新処理で更新された環境」と同じ形を見て、新しい導入でパッケージを全部入れ直してしまう (約束 3)。依存の記録が書けなかったときは `.update_complete` も書かず、次の起動でもう一度直接確かめる (`write_dependencies_record` が書けたかどうかを返す)。
- **SAIVerse が動いているかの確かめと `.env`**: やり直しの前の確かめ (`_saiverse_may_be_running`) は、本体と同じ場所の runtime marker を見るために `.env` の SAIVERSE_HOME / SAIVERSE_USER_DATA_DIR を取り込む。取り込みは main.py の `load_dotenv()` (override=False) と同じ規則で、`${NAME}` の展開では既にある環境変数が `.env` の値に勝つ (python-dotenv の `DotEnv(override=False)`、`PYTHON_DOTENV_DISABLED` も同じに扱う)。以前の `dotenv_values` は `.env` 側を勝たせて展開するので、本体と別のフォルダの runtime marker を見て、動いている本体を見落としえた。読む `.env` も main.py と同じもので、SAIVerse のフォルダから上のフォルダへ探して最初に見つかったもの (python-dotenv の `find_dotenv` の規則、`find_dotenv_path`。2026-10-23 の五巡目までは SAIVerse のフォルダの `.env` しか見ておらず、親のフォルダの `.env` に SAIVERSE_HOME を置いた環境では別のフォルダの runtime marker を見ていた)。`.env` があるのに読めない (python-dotenv が無い、文字コードが壊れている) ときは「確かめられない」として、やり直しを次の起動へ延ばす。
- **画面のサーバーと SearXNG を確かめられないとき (2026-10-23、五巡目)**: psutil が使えずにこのフォルダの画面のサーバーと SearXNG のサーバーを数えられないときも、やり直しを次の起動へ延ばす (失敗の記録とバナー、`refuse_while_frontend_runs(refuse_when_unchecked=True)`)。以前は警告して進んだので、動いている画面のサーバーの横で `npm ci` が走りえた。update.bat の経路は警告して進むまま (壊れた psutil を入れ直す経路なので)。
- **SearXNG のプロセスの見分け方**: python のプロセスで、コマンドラインに `-m searx.webapp` があり、動かしている python がこのフォルダの SearXNG の venv (`scripts/.searxng-venv`、`SEARXNG_VENV_DIR` で変えられる) のものであるもの (`cmdline[0]` か `exe()` で、パスの区切りの単位で比べる)。Windows の venv の `python.exe` は、同じコマンドラインで本体の python を子として起動するので、両方が当たる。その祖先のうち、start.bat の「SearXNG」の窓 (`cmd.exe` のコマンドラインに `title SearXNG`) と `run_searxng_server.ps1` を動かしている PowerShell だけを一緒に止める。別のフォルダの SearXNG や、利用者が自分で動かした python には触れない。止まらないプロセスが残ったら、画面のサーバーと同じく何も変えずに更新を断る。
- **SearXNG の起動し直し**: Windows は start.bat と同じ窓 (`cmd /k "title SearXNG && powershell -ExecutionPolicy Bypass -File scripts\run_searxng_server.ps1"`、作業フォルダは SAIVerse のフォルダ)、macOS / Linux は `run_searxng_server.sh` を新しいセッションで起動する。止めたときに動いていた場合だけ起動する。起動し直すのはバックエンドを起動する前 (巻き戻しの経路も同じ順番)。起動 (Popen) に失敗したら `.searxng_restart_failed.json` に書き、起動し直したバックエンドが起動時に `environment_alerts` で画面のバナーに出して、その記録を消す (その起動し直しで起きた出来事の知らせなので、一度出したら終わり。ログだけにしない — 約束 5)。バックエンドより前に起動するのはこのためで、バックエンドのあとだと、バナーを集め終えたあとに記録が書かれる。SearXNG を起動し直したあとでバックエンドが失敗して巻き戻すときは、二つ目の SearXNG を起動しない (巻き戻しの依存の入れ直しは、動いている SearXNG を入れ直さずに失敗として記録し、次の起動でやり直す)。起動直後に SearXNG が終わったかは見ない (SearXNG の窓にエラーが出る)。
- **SearXNG の入れ直しに失敗したとき**: Node.js と同じく更新は止めず、更新ログに警告を残して続ける。SearXNG は検索のための任意の部品で、GitHub に届かないだけで更新全体を止めると、コードの修正まで届かなくなるため。次に SearXNG を起動するとき `run_searxng_server.ps1` / `.sh` が同じ確認と入れ直しを行う。別の Python の系列で作られた SearXNG の venv を退避して作り直していたときは、`searxng_install.py` が作りかけを消して退避した venv を元に戻してから失敗を返す。退避の前に退避先を `.setup_in_progress/searxng-venv.json` に書いておくので、窓を閉じるなどで途中で止まっても、次の `setup` が入れ直しと退避先の戻しを引き継ぐ ([dependency_management.md](dependency_management.md) §2-7)。更新から呼ぶ `searxng_install.py setup` には `SAIVERSE_SEARXNG_SKIP_FAILURE_RECORD=1` を渡し、インストーラー自身の失敗の記録 (`.searxng_setup_failed.json`) は書かせない (同じ失敗は `.update_dependencies_failed.json` のバナーに出る)。
- **Node.js / SearXNG の失敗の知らせ**: `refresh_portable_node` / `refresh_searxng` は失敗を `update_dependencies` の戻り値として返し、子 (`install_dependencies_main`) は終了コード 0 のまま、`.update_dependencies_complete` を書かずに `.update_dependencies_failed.json` へ失敗を書く。そのため画面のバナーに出て、次の start.bat / start.sh の `--check-complete` で入れ替えがやり直され、やり直しがまた失敗したときは終了コード 12 を返す。`install_node_portable.ps1` / `searxng_install.py` がフォルダに無いときも画面に出す (約束 5) が、やり直しても直らないので `retry=False` の失敗として返し、`.update_dependencies_complete` は書く — 起動のたびにパッケージを全部入れ直し続けないため。バナーは update.bat / update.sh で SAIVerse のファイルを直すよう案内し、後の子の実行が失敗なしで終わるまで残る。
- **`.node.old` だけが残った状態の復旧**: start.bat / start-dev.bat が、`.node` が無く `.node.old\node.exe` があるときに `.node.old` の名前を `.node` へ戻す (バージョンの見比べはしない)。`install_node_portable.ps1` 自身も最初に同じことをするが、いまはこのスクリプトが更新の中でしか走らないので、起動の側にも残した。
- **巻き戻し**: 前のバージョンの子に `--keep-portable-node` を渡し、Node.js を戻さない ([dependency_management.md](dependency_management.md) §2-6 の方針)。この指定を知らないバージョンの子には渡さない。

## 4. 変更の置き場所 (なぜここか)

- 入れ替えの手順はすべて `update_dependencies` (`scripts/update_engine.py`) に集める — 子プロセスで新しいコードとして実行されるのはこの関数であり、ここにまとめれば「新しく足した作業がその回に走らない」という漏れは起きない。
- やり直しの要否判定は `--check-complete` に置く — start.bat / start.sh の中で、本体より先に新しいコードで実行される唯一の場所であるため。本体 (`main.py`) の稼働中にパッケージを入れ替えるのは危険 (Windows では使用中のファイルを差し替えられない) なので、`main.py` では入れ替えを実行しない。`main.py` に足すのは、失敗を記録したファイルを読み取って `startup_alerts` に積む呼び出しだけ (読み込み処理の本体は別のモジュールに置く)。
- `--check-complete` にある既存の「`--manual` で全体の更新をやり直す経路」には混ぜない。そちらを使うと新しいリリースまでダウンロードしてしまうため (約束 4)。

## 5. 塞げない範囲 (正直に)

v0.3.24 以前から画面の「Update」ボタンで更新し、そのあと start.bat / start.sh を一度も使わずに稼働させ続ける環境。古い `update_engine.py` で更新している最中は新しいコードを実行できず、start.bat を起動しない限り `--check-complete` も実行されない。そのため、漏れた入れ替えが行われるのは、次に start.bat を起動したときか、次のバージョンへ更新したときになる。この対象は v0.3.24 以前からの更新に限られており、今後増えることはない。

## 6. 検証 (本番と分けたコピー環境で、境界を跨ぐ順に)

1. v0.3.24 相当の環境 (パッケージ入れ替え完了の記録が無い `.update_complete`) で `--check-complete` を実行 → `update_dependencies` が一度だけ実行され、記録が書かれ、pip が更新されて fastembed が削除される。SAIVerse のバージョンは変わらない。
2. もう一度 `--check-complete` を実行 → 何も実行されない。
3. 新しく導入した直後の環境 (`.update_complete` 無し) で実行 → `update_dependencies` は実行されず、記録だけ書かれる。
4. 入れ替えのやり直しを故意に失敗させる → 本体の起動は続き、画面のバナーに失敗が表示される。次の成功時にバナーが消える。
5. 画面の「Update」ボタンでの更新を通しで実行 → SearXNG が停止・再導入・再起動される。Node.js の入れ替えも子プロセスで行われる。
6. 本物の v0.3.24 の ZIP から v0.3.26 へ更新する通しテスト (リリース前の実機検証で一度実施)。

## 7. 経緯

- 2026-10-23: [PR #415 の Codex レビュー](https://github.com/maha0525/SAIVerse/pull/415)への対応を議論。(a) 既存の「`--manual` で全体の更新をやり直す処理」の流用は「起動しただけでバージョンが進み得る」ためまはーが却下、(b) pip の更新と fastembed の削除だけを行う専用処理は「今回しか使わない機能をわざわざ作るのか」として却下。「再起動後に行うしかない更新処理」をまとめる新しい仕組み「アップデート再起動後処理」(工程番号つきの一覧。名前と「工程番号」はまはーの命名) を一度 intent に起草したが、私が前提を確かめずに書いていた — 「Update ボタンの再起動で SearXNG の入れ直しが走る」(実際は走らない) と「新しいコードは再起動後にしか動かない」(#414 で既に解消されていた) の二点。コードを辿り直して前提の誤りを報告し、原因の穴を塞ぐこの設計へ置き換えた (まはー GO)。同じ議論でまはーが出した三つの裁定 — 失敗はログではなく画面で知らせる / Node.js と SearXNG も特別扱いせずすべて同じ流れに入れる (プレリリース中の今は、目先の安定より今後の拡張でのトラブルの少なさを優先する) / `main.py` を膨らませない — はこの設計にそのまま引き継いだ。
- 2026-10-23: 実装した (具体は §3 の「実装の具体」)。§6 の 1〜4 は偽物の子プロセスで自動テストに固定した (`tests/test_update_dependencies_reinstall.py`)。SearXNG の止める・入れ直す・起動し直すと、Node.js の入れ替えが子プロセスの側へ移ったことも、既存のテストの形に倣って固定した (`tests/test_update_engine_searxng.py`、`tests/test_update_engine_frontend.py`、`tests/test_portable_node_refresh.py`)。start.bat の `.node.old` を戻す処理と終了コードの分岐は、cmd.exe で実際に動かして確かめた。動いている本物の SearXNG のプロセスでの見分け方の確認と、§6 の 5・6 の通し確認はまだ。
- 2026-10-23: PR #417 の Codex 敵対レビュー四巡目の指摘を直した (§3 の「実装の具体」に追記): やり直しの `npm ci` が失敗した次の起動が全体の更新 (`--manual`) へ切り替わる穴、`.env` の展開が本体と違う穴、新しい導入の記録の順番、SearXNG の起動し直しの失敗がログだけだった穴。同時起動で入れ直しが並走する件は、既存の issue ([update_entrances_lack_process_lock.md](../issues/update_entrances_lack_process_lock.md)) の範囲として直していない。
- 2026-10-23: PR #417 の Codex 敵対レビュー五巡目。指摘 6 件のうち 4 件 (setup の `finish` が消している途中の古い `.venv` を戻し先に選ぶ、アドオンの環境を消した直後に止まると次の回が見失う、create で始まった setup の中断を続けられない、SearXNG の退避先が次の回に引き継がれない) は、四巡目と同じく「途中で止まった瞬間」の別の瞬間だった。四巡目の手当ては、次の回が前の回のやりかけをフォルダの様子から推し量る作りで、推し量り方を細かくするほど推し量れない形が増えていた。そこで瞬間ごとに手当てを足すのをやめ、壊す操作の前に「やりかけている」ことを記録に書き、全部終わってから消し、記録が残っていれば書かれたものを全部やり直す、という一つの作りに置き換えた ([dependency_management.md](dependency_management.md) §2-7 の「やりかけの記録」と §7)。残りの 2 件は、psutil が使えないときに setup と起動前の入れ直しが警告だけで進んでいた件 (setup は利用者の確認を待ち、起動前の入れ直しは延ばす) と、`.env` の探し方が main.py と違った件 (上のフォルダへ探す形に揃えた、§3 の「SAIVerse が動いているかの確かめと `.env`」)。
