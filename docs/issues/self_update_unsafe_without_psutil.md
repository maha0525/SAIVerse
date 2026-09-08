# Windows で psutil の無い環境では、UI からのセルフアップデートが必ず失敗しバックエンドが落ちたまま戻らない

**状態**: 検証待ち (2026-09-07 起票、v0.3.11 主案件。実装・レビュー収束済み、残 = まはーの PR 確認と発行)
**起票**: 2026-09-07 (v0.3.9 → v0.3.10 へ UI から更新しようとした利用者 (稟乃さん) の「アップデートが終わらず、バックエンドが二度と立ち上がらない」報告の調査。self_update.log で確定)
**関連**: `scripts/update_engine.py` `wait_for_owned_process_exit` / `_process_alive` / `_identity_matches`、`requirements.txt` `requirements.lock`、`saiverse/runtime_marker.py` `_marker_state` / `another_running_process_owns_db`、`sai_memory/backup.py` `_is_process_alive`

## 症状

UI の「アップデート」ボタンを押すと、バックエンドは設計どおり 3 秒後に終了するが、
別プロセスのアップデータが更新を中止し、誰も再起動しないまま終わる。
利用者から見ると「アップデートしたら SAIVerse が落ちて二度と起動しない」。
update.bat による手動実行は同じ環境で正常に完了する (報告者の self_update.log
22:41:39 に「Update applied. Start SAIVerse normally」の完了行があり、その後
v0.3.10 の動作を実機で確認済み)。

self_update.log の決定的な行 (2 回の試行とも同一):

```
Phase: create and validate pre-update world snapshot
ERROR: SAIVerse appears to be running (port 8001 is open (SAIVerse backend listening)).
Update aborted: create and validate pre-update world snapshot failed with exit 1
```

時刻がそのまま証拠になる: バックエンド本体の終了は 22:23:15 なのに、
スナップショット側の稼働検査は 22:23:11.7 に走っている。
**アップデータが本体の終了を待たずに先へ進んだ。**

この欠陥は Windows 固有。macOS / Linux では psutil の無い場合の代替
(`os.kill(pid, 0)`) が生存確認として働き、終了待ちは機能する。

## 原因

1. アップデータは「本体プロセスの終了を最大 30 秒待ってから」更新前スナップショットを
   撮る設計 (`wait_for_owned_process_exit`)。プロセスの生存確認 `_process_alive` は
   psutil で行い、**psutil が import できない場合、win32 の代替経路は「もう終了している」
   を返す** (代替 `_process_create_time` も psutil 依存で必ず None → False)。
2. **psutil は requirements.txt にも requirements.lock にも入っていない。** 本体だけを
   セットアップした利用者の venv には存在しない。
3. その結果、psutil の無い Windows では終了待ちが判定できないまま即座に通過し、まだ
   生きている本体の隣でスナップショットに進む。スナップショット側の稼働検査で
   「動作中」と検出されて更新全体が中止される。中止した時点で本体は終了予約済みなので、
   再起動する者がいない。
4. update.bat による手動実行が完了するのは、config 無しの経路が終了待ちの工程
   そのものを持たない (利用者が先に全ウィンドウを閉じている前提) ため。

なお報告者のログの稼働検査の文言 (port 由来) は、その瞬間の runtime marker 照合
(`marker_status`) の結果が「停止中」だったことも意味する (稼働検査はマーカーを先に
見て、"running" / "unknown" ならその理由を出すため)。本体は生きていたので、
マーカー照合の psutil 無し代替 (`os.kill`) も誤った判定を返したということ。どの経路で
誤ったかは報告者の環境でしか確定できないが (候補は権限エラーを「死んでいる」に潰す
下記の枝)、psutil を必須化すればマーカー照合自体が正確になり、どの枝でも塞がる。

三つの原因の整理 (CLAUDE.md「After a failure we caused」):

- **直接の原因**: 生存確認の「判定できない」場合が「終了済み」と同じ返り値に
  潰れており、待つべき場面で待たずに通過する。
- **判断の失敗**: update_engine を書いたとき、開発環境の venv に psutil が居た
  (voice-tts アドオンの間接依存 accelerate/peft 経由) ため「psutil はある」前提を
  検証しなかった。開発環境では構造的に再現しない。
- **通した条件**: main.py は psutil を「無ければ netstat で代替して続行する任意の部品」
  として扱っており、後発のコードがその前例に倣った。任意でよいのは*性能や利便性*が
  落ちるだけの箇所であって、*安全のための判定*が psutil に乗ることとの区別が無く、
  本体コードが import する外部パッケージと requirements の突き合わせ検査も無い。

## 同じ型が他に残っていないかの全数調査 (転移テスト、2026-09-07 実施)

数え上げの基準は「psutil を import しているか」ではなく、直接の原因の型そのもの —
**「プロセスの生死が判定できない場合を、エラーを出さずに『死んでいる』へ潰していないか」**。
`import psutil` の 4 ファイルに加え、`os.kill(pid, 0)` を生存確認に使う箇所も調べた:

- `saiverse/runtime_marker.py` `another_running_process_owns_db` —
  CITY_SLUG 自動修復が稼働中プロセスの City を乗っ取らないための、二重起動を防ぐ検査。
  psutil が無いとプロセス照合が "unknown" になり、**"running" ではないので検査を通過
  してしまう** (稼働中プロセスの City を改名しうる)。同ファイルの起動時検査
  `acquire_runtime_marker` は "unknown" なら起動を拒否する安全側の作りで、
  同じファイル内で異常時の扱いが逆向き。→ 修正 4
- `saiverse/runtime_marker.py` `_marker_state` — psutil 無しの代替 `os.kill(pid, 0)` が
  **権限エラー (PermissionError = プロセスは存在するがアクセスできない) も「死んでいる」
  に潰す**。"stopped" 判定のマーカーは `acquire_runtime_marker` が削除するため、
  生きているプロセスのマーカーが消え、二重起動を防ぐ検査そのものが無効になりうる。→ 修正 4
- `sai_memory/backup.py` `_is_process_alive` — バックアップの多重実行を防ぐロックの
  「持ち主が死んだので消してよい」判定。同じく権限エラーを「死んでいる」に潰す
  (実害は fcntl のある Unix 側のみ — Windows はロック機構ごとスキップされる)。→ 修正 5
- `api/routes/system.py` — 更新 config に書く `main_process_created_at` が None になる。
  これを読む側 (`_identity_matches`) は None を「照合不能 = 触らない」と判定するため
  安全側。対処不要。
- `main.py` `find_pid_for_port` — netstat での代替あり。利便性が落ちるだけで安全のための
  判定ではない。対処不要。

調査の副産物二つ: (a) Windows の `os.kill(pid, 0)` は「プロセスを殺す」と誤解され
がちだが、実験 (Python 3.13.2) で「生存していれば何もせず戻り、存在しなければ
OSError」の生存確認として正しく働くことを確認した。危険なのは殺すことではなく、
権限エラーの潰し方。(b) 更新 config の `child_processes[].process_created_at` は
書く側 (api/routes/system.py) だけがあり、update_engine のどこからも読まれていない
未使用の項目 (小さな負債として記録のみ)。

## 修正方針

1. **psutil を requirements.txt に追加し、requirements.lock を再生成する**
   (`uv pip compile requirements.txt --universal --python-version 3.11 -o requirements.lock`、
   その後 `python scripts/check_lock_platforms.py` で 4 プラットフォーム × Python 3.11〜3.13 の
   wheel 存在を検査)。
   **配布の道筋に注意**: UI からの更新で走るアップデータは git 更新より前の (= 修正前の)
   チェックアウトのものなので、psutil を持たない Windows 利用者に v0.3.11 は UI 更新
   では届かない — **update.bat を一度実行すれば入り、以後は UI 更新も直る**。
   リリースノートにこの一手を明記する。報告者は 22:41 に update.bat を通しているので、
   次の update.bat (または起動時の仕上げ) で自然に入る。
   さらに (Codex 一巡目 high の採用)、**UI 更新の入口 (api/routes/system.py) に psutil の
   事前検査を置く**: 無ければアップデータを起動もシャットダウン予約もせず、本体が
   生きているうちに「update.bat を一度実行してください」と画面に返す。案内をリリース
   ノートだけに置くと、読まない利用者で同じ事故が再発するため。
2. **update_engine の生存確認で、「判定できない」を「終了済み」と区別する**: psutil が
   import できないときは「終了している」と誤った判定を返さず、UpdateError を発生させて
   明示的に中止する (「本体の終了を確認できないため更新を中止した」とログに残る)。結果が
   「進まない」なのは同じでも、誤った判定のまま先の検査任せにする構造を消す。
   self-contained 方針 (他モジュールを import しない) は維持する。
   import 後の呼び出し (`psutil.pid_exists`) の実行時エラーも同じ UpdateError へ変換する —
   素通しすると呼び出し元は UpdateError しか受け止めないため、未処理のまま落ちて
   ログに中止の記録すら残らない (Codex 一巡目 medium の採用)。
3. **終了待ちの上限を 30 秒から 120 秒へ延ばす**: 大きな世界のシャットダウンは 30 秒に
   収まらないことがあり、期限超過時の挙動が「本体を強制終了する」なので、上限が短いと
   シャットダウン途中 (発言の確定・記録の書き込み中) の本体が強制終了されうる。待ちを
   延ばすコストは更新開始が遅れるだけで、失うものがない。
4. **runtime_marker の判定を安全側に揃える**: (a) `another_running_process_owns_db` で
   照合が "unknown" のマーカーは「稼働中かもしれない」として修復拒否側に数える
   (起動時検査と同じ向き)。(b) `_marker_state` の psutil 無し代替で、権限エラーは
   「死んでいる」ではなく "unknown" (存在するが照合できない) に落とす。
   (c) psutil のある通常経路でも、マーカー側の照合値 (`process_created_at`) が欠落・
   型不正なら "stopped" (= pid 再利用の確定) に潰さず "unknown" にする — None の記録は
   psutil の無い環境で書かれたマーカーの正規の姿で、生きている本物かもしれないため
   (Codex 一巡目 high の採用)。"stopped" は両方の値が取れて数値が本当に不一致の場合だけ。
5. **backup.py の生存判定も同じ向きに直す**: `_is_process_alive` で権限エラーは
   「生きている」扱いにする (ロックの誤削除を防ぐ)。
6. 回帰テスト: psutil の import を封じた状態で (a) `wait_for_owned_process_exit` が
   UpdateError を発生させること、(b) `another_running_process_owns_db` が unknown
   マーカーで拒否すること、(c) 死んだ pid のマーカーは psutil 無しでも修復を止めない
   こと (安全側に倒しすぎの回帰防止)、(d) 権限エラーが "unknown" / 「生きている」に
   落ちること。requirements.lock に psutil の行があることの検査も 1 本置く。

「判定できないなら止まる」に直すのは、**更新の入口と、破壊的な後始末の判定
(マーカー削除・ロック削除・自動修復) に限る**。起動可否の判定 (`--check-complete`)
が「判定できないときは起動させる」側に倒れているのは、dependency_management.md に
記録された別の決定 (2026-09-02、fail-open 維持) であり、本件では触らない。

**範囲外の明記**: 更新が (正当な理由で) 中止されたときにバックエンドを立て直す仕組みは
本 issue の範囲外。本件の修正後も、fail-closed の中止 (樹の変更検出・スナップショット
拒否など) では「落ちたまま + self_update.log に理由」の挙動が残る。中止時の再起動や
UI への結果通知は別の設計課題。

「本体コードが import する外部パッケージと requirements を機械的に突き合わせる検査」も
本件より広い構造課題として記録に留める。

## 検証

- 回帰テスト (修正方針 6) と既存の update_engine 安全テストが全件合格であること。
- requirements.lock は uv での再生成結果と一致すること (手編集でないことの確認) と、
  `scripts/check_lock_platforms.py` 合格 (2026-09-07 実施: バージョン固定した 132 件の
  パッケージ全てが全プラットフォームで取得可能)。
- 素の venv に requirements.lock だけで入れて全テストスイートが合格すること
  (dependency_management.md §5-2 の「ユーザーの手元の代理」。2026-09-07 実施:
  Python 3.13 の新規 venv + lock のみで 5754 件合格・7 件スキップ)。

## 経緯

- 2026-09-07: 稟乃さんの報告 (v0.3.10 配布時) を調査し、self_update.log の提供を受けて
  確定。同日、まはーが「了解！その対応を0.3.11としよう。issue起案から実装よろしく！」と
  決定し、v0.3.11 の主案件として着手。
- 2026-09-07 (レビュー): ローカル LLM (qwopus-27b) は誤指摘ゼロ・新規指摘なし (「未確認」
  5 点は全て発注側の呼び出し元検算で確認済みの箇所と一致)。Codex 敵対レビュー一巡目は
  3 件 (high 2 + medium 1) で**全件採用** — ①psutil 無し利用者は UI 更新で修正版を導入
  できない (→ UI 入口の事前検査、修正 1 に追記)、②マーカーの照合値が欠落・型不正だと
  psutil があっても "stopped" に逆戻り (→ 修正 4c)、③`psutil.pid_exists` の実行時エラーが
  未処理で漏れる (→ 修正 2 に追記)。
- 2026-09-08 (Codex 三巡目): medium 1 件のみ — `import psutil` の失敗を ImportError に
  限定していた変換漏れ (壊れた拡張は OSError でも失敗しうる) を except Exception へ拡張して
  採用。指摘の軌跡は本物 3 → 外周 3 → 外周 1 で収束と見立て、四巡目は投げずに仕上げた。
  仕上げのフルスイートは 5764 件合格。
- 2026-09-08 (Codex 二巡目): 3 件全て採用 — ①UI の事前検査を「API 自身の import」から
  「実際にアップデータを動かす venv の python で probe (import + create_time 呼び出し) が
  動くか」の subprocess 検証へ置き換え、②db_path の無いマーカーは同じ DB か判定できない
  ので修復拒否側へ、③照合値の型を厳密化 (数値文字列・bool・無限大は "stopped" ではなく
  "unknown")。二巡目で的は実利用者の型から破損マーカーの理論値へ移っており、修正が
  極小なので採ったが収束は近い。指摘中「NaN は stopped に落ちる」は事実誤認 (NaN の
  比較は常に偽で running = 拒否側に落ちる) — 型検査には含めて統一した。
- 2026-09-07 (整合性レビュー): Opus サブエージェントの検査で 4 件を取り込んだ —
  ①欠陥の Windows 限定 (表題・症状に反映)、②稼働検査の port 文言がマーカー照合の
  誤判定を含意すること (原因に反映)、③転移テストの数え上げ基準を「import psutil」から
  欠陥の型そのものへ広げ、`_marker_state` の権限エラーの枝と backup.py を追加
  (修正 4b・5)、④「UI 更新では修正が届かない」配布の道筋 (修正 1)。
  「Windows の os.kill(pid, 0) は生存確認にならない」という指摘は実験で棄却
  (副産物 (a))。
