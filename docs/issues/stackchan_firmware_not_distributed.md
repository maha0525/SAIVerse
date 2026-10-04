# Issue: Stack-chan Vessel の firmware が一般ユーザーに届かない

**ステータス**: 🟣 検証待ち (= アドオン v0.5.0 は本家 gateway 0.18.0 / firmware-v1.17.0 に切り替えて作業用ブランチで完成。push の承認と、まはーの機体での確認待ち)
**優先度**: high (= 実ユーザーが導入時点で詰まっている)
**作成日**: 2026-09-07
**報告**: 外部ユーザー (Stack-chan Vessel v0.4.0、「ファームウェア書き込み」が利用不可)
**関連**: `docs/intent/stackchan_vessel.md` §不変条件 8 / §I-1、`docs/intent/addon_catalog_management.md` §firmware 配布の方針、`docs/issues/stackchan_mcp_upstream_pr_strategy.md`

## 現在の方針 (2026-10-03)

**fork からファームウェアを配るのをやめて、本家 (= 下の節でいう upstream、`kisaragi-mochi/stackchan-mcp`) が配っているファームウェアをそのまま使う。** まはーの方針。これは `docs/intent/stackchan_vessel.md` の不変条件 8 (ユーザーが本家の配布ページから直接ダウンロードする) に書いてあった元の形で、下の「対応方法」の A (fork に配布ページを作る) と「裁定が必要な点」の 2 件 (まはーが GPL-3.0 の配布者になるか / どのブランチを配布版にするか) は不要になる。

この方針が成り立つのは、fork にしか無いファームウェアの修正を本家の最新と中身で突き合わせ直したところ、SAIVerse が実際に必要としているものが 2 つまで減っていたため。2026-10-03 に `upstream/main` (6a28ca4、2026-09-20) と比べた結果は次のとおり。

| fork にしか無かった修正 | 本家の状態 | SAIVerse に要るか |
|---|---|---|
| 無音で切れた接続から復帰する (WS keepalive) | 2026-09-20 に本家へ入った (本家 PR #240)。ただし本家が配っているファームウェアの最新は `firmware-v1.16.0` (2026-07-12) で、それより後の変更なので配布物には入っていない | 要る |
| センサー読み取り (IMU / 環境光と近接 / NFC) | 本家 PR #339 が下書きのまま未マージで、本家の最新と衝突している。本家側に別の人の IMU の PR (#355) も未マージで並んでいる | アドオンのスペル 4 つ (`read_imu` / `read_imu_context` / `read_environment` / `scan_nfc`) が頼っている。**本家に入るまでふさぐ** (= スペルをペルソナから見えなくする。まはーの方針) |
| ゲートウェイの接続先 URL が上書きされる問題 | 本家が別の方法 (本家 PR #119) で直し済み。fork の PR #216 は 2026-05-23 に取り下げた | 不要 |
| 機体の再起動を検知する ID (`boot_session_id`) | 本家に無い。PR も出していない | 不要。アドオンは 2026-08-25 から、ゲートウェイが接続ごとに振る `session_id` を見ている (`avatar_loader.py`) |
| クラッシュの記録を機体に残す設定 (coredump) | 本家に無い。PR も出していない | 開発者の調査用で、ユーザーの機体には不要 |

下の「実態」(1) の表にある「upstream/main より 10 commit 先行」は、起票時にコミットの数で比べた数字である。そのため、本家がすでに取り込んでいた修正 (排他ロックをポートごとに分ける修正、画像アップロードの修正) まで fork 固有として数えてしまっていた。中身で比べた上の表が正しい。

残っている仕事は 3 つ。

1. **本家が keepalive 入りのファームウェアを配る** — まはーが如月もちさんに依頼する (2026-10-03)。断られた場合や時間がかかる場合は、下の A (fork から配る) に戻る。
2. **アドオンが本家のファームウェアを自動で取ってくる** — 下の B の `download_file` の `url` を、fork ではなく本家の配布物に向ける。本家の配布物は `firmware-` で始まるタグにしか付いていないので、タグ名まで書いた URL と SHA256 を書く。
3. **センサー読み取りのスペルをふさぐ** — 本家のファームウェアにはセンサー読み取りが無いので、スペル 4 つをペルソナから見えなくする。あわせて、ゲートウェイの取得元 (`mcp_servers.json` が fork の `feature/stackchan-imu-readings` を指している) を本家に戻すかを決める。今のアドオンを本家のファームウェアで動かしたときにスペルがどう失敗するかは未確認。

`docs/intent/addon_catalog_management.md` §firmware 配布の方針には、「案 A = fork の配布ページに置く」を将来採用する案として書いてある。1 の返事で方針が固まったら、そちらも書き換える。

下の C (紛らわしい旧ファームウェアの片付けと案内文) と D (開発者の環境でだけ成功する経路) は、この方針でもそのまま要る。

### 同日の訂正 — 上の表と「残っている仕事」は、開発者の手元のアドオンを読んで書いたもの

上の表の「SAIVerse に要るか」の列と、残っている仕事の 3 は、`expansion_data/saiverse-stackchan-addon/` の作業ツリーを読んで書いた。この作業ツリーは、ユーザーに配られているアドオンとは別物だった。2026-10-03 に確かめた事実は次のとおり。

| | 中身 | 日付 |
|---|---|---|
| **ユーザーに配られているもの** (公開カタログが指す v0.4.0 = コミット `2d7ef6d`) | ゲートウェイは fork の `dev/integration` ブランチから取る。スペルの元になるツールは `see.py` と `units/env3.py` だけ。機体の再起動の検知は、fork のファームウェアが返す `boot_session_id` に頼っている | 2026-05-23 |
| アドオンの GitHub にあるが、カタログが指していないもの (`v0.4.0..origin/main`) | 15 コミット (Avatar 制作の画面の改善、PaHUB・超音波・8 サーボのユニット、ゲートウェイの取得元の切り替え) | 2026-05-23 〜 06-29 |
| 手元にコミット済みで、GitHub に無いもの (`origin/main..main`) | 6 コミット (複数機体の同時稼働、機体管理の画面、IMU のスペル、再起動の検知をゲートウェイの `session_id` に変える修正) | 2026-07-01 〜 08-26 |
| 手元で未コミットのもの | 変更 15 ファイル (+1576 / −456 行) と、未追跡 14 ファイル (センサー読み取りのスペル 4 つのうち 3 つ、`device_controls.py`、ToF ユニット、テストを含む) | 主に 2026-07-03 〜 07-11。`addon.json` は 09-15、`vessel_manager.py` は 09-23 |

ここから言えること。

- センサー読み取りのスペルは、ユーザーには一度も配られていない。「ふさぐ」対象は開発者の手元にしか無い。
- 上の表で `boot_session_id` を「不要」と書いたのは、手元のアドオンについてだけ正しい。配られている v0.4.0 は `boot_session_id` に頼っているので、v0.4.0 のまま本家のファームウェアを使うと、機体の再起動のあとに表情の絵が送り直されない。
- したがって、ユーザーに本家のファームウェアを届けるには、アドオンの新しいバージョンをカタログに載せることが必ず要る。**どの状態のアドオンを次のバージョンにするか** (手元の状態を丸ごと出すか、v0.4.0 に最小の修正を足すか) が未決。
- 本家が配っているゲートウェイの最新は v0.17.0 (2026-07-12) で、`session_id` を返す変更 (本家 PR #357、2026-07-21) より前である。ゲートウェイを本家の配布物に切り替えるなら、本家のゲートウェイの新しいリリースも要る。まはーが如月もちさんに、ファームウェアとゲートウェイの両方のリリースを依頼済み (2026-10-03)。

### 次のバージョン (v0.5.0) の進め方 (2026-10-03、まはー GO)

**開発者の手元の状態を丸ごと次のバージョンにする。** まはーが毎日動かしてきた状態で、機体の再起動の検知もゲートウェイの `session_id` を使う形にすでに直してあるため。v0.4.0 に最小の修正だけ足す道は、誰も動かしたことのない組み合わせをユーザーに渡すことになるので採らない。

- 手元で未コミットだった変更は、内容を変えずにアドオンのリポジトリの `main` へ 3 コミットで記録した (`fc8ddb6` / `5fe4e6a` / `f1981c7`、GitHub へは未 push)。
- リリースの準備は、本番で動いているフォルダ (`expansion_data/saiverse-stackchan-addon/`) を触らずに、別の作業用フォルダ (`temp/stackchan-addon-release`、ブランチ `release/v0.5.0`) で進める。本番のフォルダを切り替えるのは、まはーの機体に本家のファームウェアを書き込むときと同時にする。
- 本家のリリースを待たずにできる部分 (センサー読み取りの撤去、旧ファームウェアの `archive/` への移動、README、ファームウェアが見つからないときの案内、開発者の PC にだけある探索経路の削除、古くなっていたテストの修正) を先に実装する。
- **開発者の PC にだけある探索経路は、表示で区別するのではなく削除する** (下の D の具体形)。開発者が手元のビルドを使いたいときは、アドオンの設定の `firmware_path` で明示する。残しておくと、まはーの PC では fork のビルドが自動ダウンロードされたファームウェアより先に選ばれ、開発者とユーザーが別のファームウェアを使う状態が続く。

**先に実装した部分は、作業用フォルダのブランチ `release/v0.5.0` に 4 コミットで入っている** (2026-10-03、GitHub へは未 push)。

- `8e60348` 古くなっていたテストの修正 (118 件すべてが setUp で落ちていた。保存先の関数の名前が v0.4.0 で変わったのに、テストが古い名前を差し替えていた)。
- `51711d6` センサー読み取りの撤去。本家にセンサーが入って配られたら、このコミットを revert して戻す。
- `e5178c3` ファームウェアの探し方と、見つからないときの案内。開発者の PC にだけある探索経路を削除し、画面の警告と 404 のメッセージに入手先と置き場所を書いた。
- `6ba8c5b` 旧ファームウェアの `archive/` への移動と README の書き直し。

確かめたこと: テスト 118 件が通る / ruff が通る / 開発者の PC に fork のビルドが実在しても拾われない (隔離した一時フォルダで実行) / 画面のファイルに構文の誤りが無い / README に書いた画面の文言とスペルの名前がコードと一致する。

まだ確かめていないこと: 画面の型検査と実際の見た目 (作業用フォルダは SAIVerse のフロントエンドから見えない場所にあるので、本番のフォルダを切り替えるときに確かめる) / 実機 / ローカルレビュー (本家のリリース後の変更と合わせて、v0.5.0 の差分全体に一度かける)。

**このブランチを、`download_file` の設定が入る前に公開カタログへ載せないこと。** 画面・404・README が「アドオンの導入時に自動でダウンロードされます」と案内しており、その設定が入るまでは事実ではない。

**本家のリリースへの切り替えも、作業用ブランチに入った** (2026-10-04)。本家は 2026-10-03 に gateway 0.18.0 (PyPI) と firmware-v1.17.0 を出し、どちらにも keepalive (本家 PR #240) と session_id (本家 PR #357) が入っていることを git の履歴で確かめた。

- `a87265f` ゲートウェイを `stackchan-mcp[tts]==0.18.0` に固定し、`download_file` で firmware-v1.17.0 の `merged-binary.bin` (9,981,049 バイト、SHA256 `af7ca3a3…` は GitHub の配布ページの値) を取得する。version 0.5.0 / setup_version 2。NOTICE と、fork を指していた記述を直した。
- `daf884b` `mcp_servers.json` のコメントからプレースホルダの構文記号を外した (ローカルレビューの指摘)。
- 確かめたこと: uvx で取得した 0.18.0 でゲートウェイの版の表示と Opus の Encoder の生成が通る / `addon.json` が SAIVerse のマニフェストの検証を通る / ダウンロード先の URL が応答する (中身は取得していない) / テスト 118 件と ruff。
- ローカルレビュー (NOVA の qwen3.8-flash-next、v0.5.0 の差分 30KB を一撃で): 指摘 12 件のうち本物は 1 件 (上の `daf884b`)。残りは実物で裏を取って解消済み (Opus の読み込みは実行で確認済み、消したはずのエントリは消えている、関数とスタイルは定義がある、設定値の比較は解決側と同じ作り方、ほか)。機体ごとのトークンが全機体で共通という指摘は、今回の変更より前からの設計で、今回は触っていない。

**残りの手順** (まはーの承認・操作が要るもの):

1. ~~アドオンを GitHub へ push し、タグ `v0.5.0` と GitHub Release を作る~~ — 2026-10-04 に済み (まはー承認)。`main` = `daf884b`、Release は https://github.com/maha0525/saiverse-stackchan-addon/releases/tag/v0.5.0 (本文は公開前に言葉づかいの検査を通した)。
2. まはーの SAIVerse で、公開カタログの代わりに手元のカタログのファイル (`temp/addon-registry-local.json`、v0.5.0 を載せたもの。SAIVerse と同じ読み方で読めることを確認済み) を `.env` の `SAIVERSE_ADDON_REGISTRY_URL` で読ませて、カタログの「更新」を押す。これがユーザーの更新と同じ道 (`update_addon` → setup の再実行 → ファームウェアのダウンロード) を通る。その後、パネルから firmware-v1.17.0 を機体に書き込み、Wi-Fi と接続先を設定し直し、ペルソナを降ろして声・首・カメラ・表情を確かめる。
3. 問題がなければ、公開カタログ (`saiverse-addon-registry`) の `registry.payload.json` に 0.5.0 を足し、まはーの秘密鍵で `sign_registry.py` を実行して push する。秘密鍵はまはーの手元にしか無い。

**まはーの機体での確認で見つかったこと (2026-10-04)**

- カタログからの更新は通った (ダウンロードと SHA256 の照合まで成功。置かれたファイルの大きさと指紋を確認)。ゲートウェイ 0.18.0 が機体 2 台ぶん起動したこともログで確認した。
- 2号機 (`f0d40b0b`、ポート 8767) に firmware-v1.17.0 を書き込んだあと、18:02:22 に **1号機用のゲートウェイ (ポート 18765) へ接続が来て、Token の不一致で 401 で弾かれた** (2号機からの接続である可能性が高いが、ログに接続元の IP は出ていない)。
- 本家のファームウェアは、接続先の URL が空のときだけ、LAN の中のゲートウェイを mDNS で自動で探す (`websocket_protocol.cc`)。firmware-v1.17.0 からこの自動探索が配布物に入った (本家 PR #381。それまでの配布物では、ビルドの手順の都合で無効になっていた)。ゲートウェイは機体ごとに起動し、どれも同じ名前で自分を知らせるので、URL が空の機体は別の機体のゲートウェイにつながりうる。全機体が同じ `master_token` を使っているため、Token だけ正しく入っていれば、別の機体のゲートウェイに**黙って**つながってしまう。
- 機体のセットアップ画面は「Wi-Fi」と「Advanced」の二つのタブに分かれていて、接続先の URL と Token は「Advanced」タブにあり、保存のボタンも別にある (前のファームウェアでも同じ画面)。README の手順 4 は「次の 4 つを入力して保存」としか書いておらず、タブが分かれていることを書いていない。直す。
- 1号機と 2号機のどちらでも、放っておくと 60 秒で暗くなり 300 秒で電源を切る仕組み (`PowerSaveTimer(-1, 60, 300)`) は、前のファームウェアにも同じ形であった。今回の電源断がこれによるものかは未確認。

**2号機での確認の結果 (2026-10-04 夜)**

- 2号機に firmware-v1.17.0 を書き込み、セットアップ画面の「Advanced」タブで接続先と Token を保存し直したあと、18:17:58 に 2号機用のゲートウェイへ接続できた (機体の機能 44 個、前のファームウェアは 38 個)。最初の保存が残らなかった理由は分かっていない。2回目の保存は、機体の記録 (`Saved settings: … websocket_url=ws://192.168.0.128:8767/ … websocket_token=(set)`) で書き込みを確認した。
- 音声の会話が返らなかった原因は v0.5.0 とは別で、機体の声 (audio-in) が「画面から別の Building へ発言するときは先にユーザーを移す」手順 (/chat/utter) を通っていなかったこと。画面で別の Building を開いている間は、本体の現在地の照合 (2026-07-21、W7 柱5) で断られ、断られたことはどこにも記録されていなかった。アドオンの `dea705e` で、声を届ける前にユーザーを Vessel Building へ移すようにし、断られたときは警告をログに残すようにした。
- 再起動後の確認 (まはー): 2号機に話しかけると、ユーザーがエリスの部屋から 2号機の部屋へ移り、ペルソナが返事をして、カメラで見て、表情を変えた。ログでも、ユーザーの移動 (20:59:55)、カメラの撮影 (21:00:07)、表情のスペルの実行 (21:00:37) を確認した。
- ほかに直したもの: ペアリングのログが Token を使い回しただけでも「更新した」と書いていた件 (アドオン `a43f863`)、本体のカタログの導入済み一覧が言語ごとの表示名で 500 になる件 (本体 `1e6ead2a`)。

**残っていること**

1. 1号機に firmware-v1.17.0 を書き込んで確かめる (まはー)。
2. アドオンの `a43f863` と `dea705e` を GitHub へ上げる。v0.5.0 のタグはその前のコミット `daf884b` にあるので、カタログに載せる版をどうするか決める。
3. 本体の `1e6ead2a` (カタログの導入済み一覧の修正) を含む SAIVerse を先に出す。出す前に Stack-chan v0.5.x を公開カタログに載せると、更新した人のカタログ画面が 500 で開けなくなる。
4. 公開カタログに載せ、まはーの秘密鍵で署名する。まはーの `.env` の `SAIVERSE_ADDON_REGISTRY_URL` の行を消す。

**更新の操作の副作用 (2026-10-04 に写しで確認)**: カタログの「更新」は `git fetch --depth 1` を使うので、完全な履歴を持つリポジトリ (開発者の手元のアドオンのフォルダ) が浅い履歴の状態になる。写しで試すと、更新の前は 75 コミット見えていた履歴が、更新の後は 1 コミットしか見えなくなった。ユーザーの導入物では実害は無い。開発者の手元では `git fetch --unshallow origin` で戻せる。本体の `saiverse/addon_installer.py` の `update_addon` の挙動で、今回は直していない。

**本家のリリースが出てから書くこと** (2026-10-03 に本家の最新 `upstream/main` 6a28ca4 で確かめた事実つき。すべて上の 2 コミットで済んだ)。

- ゲートウェイの取得元を、fork のブランチから本家の配布物 (PyPI の `stackchan-mcp`) のバージョン指定に変える。アドオンの `spell_tools` に載っているツールと、アドオンのコードに名前が直接書かれているツールを本家のゲートウェイのツール一覧と突き合わせると、本家に無いのはセンサーの 3 個 (`read_imu` / `read_environment` / `scan_nfc`) だけで、アドオンが渡す環境変数 6 個はすべて本家にある。Opus の符号化に要る `opuslib` は本家では追加機能 `tts` に入っているので、`stackchan-mcp[tts]` の形で指定すれば、いまの `--with opuslib` は不要になる見込み (実際に起動して確かめる)。
- 本家のゲートウェイには、アドオンの `spell_tools` に載っていないツールが 16 個ある (`beat_*`、`port_b_ws2812_*`、`port_c_ws2812_*`、`stackchan_follow_led_stream`)。SAIVerse の MCP クライアントは載っていないツールをペルソナから届かないものとして扱うので、追記しなくても漏れない。
- `addon.json` に `download_file` step を足す (下の B)。`url` は本家のファームウェアのリリース、`sha256` はその `merged-binary.bin` のもの。`version` を 0.5.0 に、`setup_version` を 2 にする。
- 公開カタログ (`maha0525/saiverse-addon-registry`) に 0.5.0 を載せる。
- `docs/intent/stackchan_vessel.md` と `docs/intent/addon_catalog_management.md` §firmware 配布の方針を、実際の形に合わせる。`stackchan_vessel.md` は、ファームウェアの探し方 (I-1 と Phase 2' の「3 段階」) と、センサーの記述 (不変条件 15・16 と C-4) が古くなる。
- アドオンの中で、ゲートウェイの取得元を変えるときに一緒に直す古い記述: `mcp_servers.json` の `_comment_command` と `_comment_scope`、`addon.json` の `data_subdirs.firmware` の説明、`NOTICE` (stackchan-mcp の記載が無い)、`tools/units/README.md` と `tools/see.py` の fork への言及。

## 症状

Stack-chan Vessel を有効化すると AddonManager の UI に警告が出て、「ファームウェア書き込み」が使えない。

> ⚠ merged-binary.bin が見つかりません。「ファームウェア書き込み」は利用不可。
> 配置 path 候補: (1) AddonConfig.firmware_path で絶対 path 指定 (2) `<SAIVerse repo>/temp/stackchan-mcp/firmware/build/` (3) `~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/`

報告者はリポジトリ内を検索して `expansion_data/saiverse-stackchan-addon/firmware/dist/` に `bootloader.bin` / `partitions.bin` / `firmware.bin` の3つを見つけ、「`merged-binary.bin` の生成または同梱処理が抜けているのではないか」と報告した。

## 実態 — バイナリが抜け落ちたバグではない

`merged-binary.bin` をアドオンに同梱していないのは意図的な設計。device に焼く firmware は `SCServo_lib` 由来で GPL-3.0 であり、SAIVerse 側が再配布すると GPL-3.0 §6 のソース提供義務を SAIVerse が負う。これを避けるため「ユーザーが upstream から直接ダウンロードする」運用にする、と intent の不変条件 8 に定めてある。

**問題は、その「ユーザーが直接ダウンロードする」経路が製品のどこにも存在しないこと。** 内訳は4つある。

### (1) 配布物そのものが存在しない

SAIVerse の Stack-chan Vessel が必要とするのは **fork (`maha0525/stackchan-mcp`) の firmware** であって、upstream (`kisaragi-mochi/stackchan-mcp`) の firmware ではない。upstream にまだ入っていない fork 固有の修正が残っているため。

2026-09-07 時点の実測 (ローカル `temp/stackchan-mcp` を `upstream/main` = 558cf40 / 2026-08-23 と比較):

| ブランチ | upstream/main より先行 | 含まれる firmware 側の修正 (抜粋) |
|---|---|---|
| `integrate/all-fixes-2026-06-24` | 10 commit | WS keepalive (無音切断からの復帰)、ownership lock の per-port 化 |
| `feature/stackchan-imu-readings` | 9 commit | CoreS3 BMI270 の電源と初回サンプル、NFC UID scan、touch 失敗ログの抑制 |

アドオン側には既に IMU の Spell が入っている (アドオン repo の commit `4d42207 Add StackChan IMU spell`)。**upstream の firmware を焼いても、この Spell は動かない。**

そして配布物の置き場所の現状は次のとおり。

| 置き場所 | `merged-binary.bin` | 備考 |
|---|---|---|
| `maha0525/stackchan-mcp` の Releases | **0 件** (リリース自体が存在しない) | fork には CI build がある |
| `kisaragi-mochi/stackchan-mcp` の Releases | あり (`firmware-v1.16.0` 等) | fork 固有の修正は入っていない |
| アドオン repo | 同梱しない方針 | 不変条件 8 |

`kisaragi-mochi` 側は罠も抱えている。`merged-binary.bin` が付いているのは `firmware-*` で始まるタグのリリースだけで、GitHub が「最新リリース」として表示する `v0.17.0` (= gateway 側のリリース) は**アセットが空**。「Releases から落として」と案内された人がページを開くと、一番上のリリースには bin が無い状態になる。

### (2) 入手方法が製品のどこにも書かれていない

- UI の警告 ([`ui/Panel.tsx:968-980`](../../expansion_data/saiverse-stackchan-addon/ui/Panel.tsx)) は**置き場所の候補**しか出さず、どこから入手するかを言わない
- backend の 404 メッセージ ([`api_routes.py:2022-2031`](../../expansion_data/saiverse-stackchan-addon/api_routes.py)) は「GitHub Releases から DL」とだけ書いてあり、**どのリポジトリなのかが無い**
- アドオンの `README.md` には `merged-binary` も `stackchan-mcp` も一度も出てこない

唯一書いてあるのはアドオンカタログ (`registry.json`) の説明文の末尾 (「ESP32-S3 firmware (merged-binary.bin) は GPL-3.0 のため別途手動配置が必要」) だが、これはカタログ画面の説明文であって、警告が出た瞬間に読める場所ではない。

### (3) 隣に紛らわしい旧 firmware が置きっぱなし

報告者が見つけた `firmware/dist/` の3つの bin は、stackchan-mcp を採用する前の**自前 firmware** (2026-05-12 ビルド、`firmware/README.md` に Apache-2.0 と明記、Web Serial で3アドレスに分けて書き込む旧世代の配布形式)。

`docs/intent/stackchan_vessel.md` には `firmware/` を `archive/firmware/` へ移動する計画が書かれているが、**実施されていない**。加えて `README.md` も「Phase 1 + 2 完了 (2026-05-13 時点)」「Web Serial でファームウェアを書き込む」という旧世代の記述のまま。報告者が「これが該当ファイルのはずで、merge 処理が抜けている」と読んだのは、材料がそう読めるように置いてあるため。

### (4) 開発者の環境でだけ常に成功する

`_firmware_resolve_path()` の解決順序の (2) は `<SAIVerse repo>/temp/stackchan-mcp/firmware/build/merged-binary.bin` で、まはーのローカルにはこれが存在する (2026-05-21 build、9.98 MB)。**まはーの環境では警告が一度も出ない。** この経路があるために、配布物が無いことが開発中に露出しない構造になっていた。

## 対応方法

「普通に導入して普通に動く」に到達するには A + B が必須。C は誤誘導の除去、D は再発防止。

### A. fork に firmware のリリースを作る (配布物を用意する)

`maha0525/stackchan-mcp` に firmware タグを切り、`merged-binary.bin` を Releases に publish する。

- 対象ブランチ: 実機で使っている統合ブランチ (現状 `integrate/all-fixes-2026-06-24` + `feature/stackchan-imu-readings` 相当)。**どのブランチを配布版とするかの確定が要る**
- fork には CI build (`workflow_dispatch` 対応) があるので、タグ push → build → Releases 添付の自動化が可能
- upstream 側と紛れないよう、タグ名は SAIVerse 向けと分かる形にする (例: `saiverse-firmware-vX.Y.Z`)

**GPL-3.0 の扱い — ここはまはーの裁定が要る**: バイナリを Releases に置くと、GPL-3.0 §6 のソース提供義務が `maha0525/stackchan-mcp` の配布者に発生する。fork のソースは同じ GitHub 上で公開されているので、リリース説明に「対応するソースはこのタグ」と明記すれば実務上は満たせる形になる (これは私の理解であって法的な確認ではない)。不変条件 8 が禁じているのは「SAIVerse-stackchan-addon が firmware を再配布すること」なので、stackchan-mcp fork から配ること自体は不変条件に抵触しない。ただし**まはーが配布者の立場に立つ**ことを意味するので、そこを承知のうえでの判断になる。

### B. addon.json に `download_file` step を足す (届ける)

**必要な機構は既に実装済みで、addon.json に数行足すだけで済む。**

- `saiverse/addon_installer.py:359` `_exec_download_file()` — URL から DL → SHA256 検証 → atomic rename で配置
- `saiverse/addon_manifest.py:167` `DownloadFileStep` — `url` (HTTPS 必須) / `sha256` (64 hex 必須) / `dest_data` (永続データディレクトリ内の相対 path)
- アドオンは既に `manifest_version: 2` / `setup_version: 1` で、`setup` セクションが空なだけ

書く内容:

```json
"setup": {
    "steps": [
        {
            "type": "download_file",
            "name": "Stack-chan firmware",
            "url": "https://github.com/maha0525/stackchan-mcp/releases/download/<tag>/merged-binary.bin",
            "sha256": "<64 hex>",
            "dest_data": "firmware/merged-binary.bin"
        }
    ]
}
```

`dest_data` の配置先は `~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/merged-binary.bin` になり、`_firmware_resolve_path()` の解決順序 (3) と一致する。**アドオン側のコードも SAIVerse 本体も変更不要。**

firmware を更新するときは `addon.json` の `url` + `sha256` を差し替えて `setup_version` をインクリメントする。既存ユーザーがアップデートすると installer が step を再実行する (`addon_installer.py:622`)。SHA256 が必須なので、firmware のバージョンは addon.json で pin される形になる = 検証済みの組み合わせだけが配られる。

### C. 誤誘導を消す

- `expansion_data/saiverse-stackchan-addon/firmware/` (自前 firmware の src / dist / platformio.ini) を `archive/firmware/` へ移動する — intent に書かれたまま未実施
- アドオンの `README.md` を v0.4.0 の実態に合わせる (Web Serial 手順は旧世代、現行は esptool + `merged-binary.bin`)
- UI の警告と backend の 404 メッセージに、A+B が失敗したときの入手先 URL を書く (DL 失敗・オフライン環境の受け皿)

### D. 開発者の盲点を塞ぐ (再発防止)

`_firmware_resolve_path()` の (2) `<repo>/temp/stackchan-mcp/firmware/build/` は開発者だけがヒットする経路。`GET /flash/firmware-info` は `source` を返しているので、UI 側で「これは開発者ローカルのビルドです」と明示するか、この経路を環境変数でのみ有効にすると、配布物の欠落が開発中に見えるようになる。

## 裁定が必要な点

1. **fork の Releases に firmware バイナリを置くか** (= まはーが GPL-3.0 の配布者になることを受け入れるか)。置かない場合、「普通に導入して普通に動く」は達成できず、C の案内改善までが上限になる
2. **どのブランチを配布版とするか** — 現状 `integrate/all-fixes-2026-06-24` と `feature/stackchan-imu-readings` が分かれている。配布用の統合ブランチを一本作る必要がある
