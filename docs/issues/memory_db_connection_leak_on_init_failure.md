# 記憶データベースの用意に失敗すると接続が閉じられず、後続がロック待ちになる

**状態**: 実装済み・検証待ち (2026-09-03。接続リークは v0.3.3 で修正済み。引っ越しが毎回失敗する根本原因を 2026-09-03 に特定して v0.3.5 で修正 — 報告者の環境での確認待ち)
**起票**: 2026-09-02 (v0.3.1 利用者からの「ひとりだけ Chronicle が存在しないと出る / Chronicle の後に Memopedia の読み込みが終わらない」報告の調査)
**関連**: `api/routes/people/arasuji.py` `_get_arasuji_db` / `sai_memory/memory/storage.py` `init_db` / `tools/utilities/memory_settings_ui.py` `_get_arasuji_connection`

## 報告された症状

> 2人いるペルソナのうちひとりのみ chronicle が存在しないと表示されます。
> memopedia は表示されますが chronicle を表示した後に memopedia を表示しようと
> すると読み込みが終わらなくなります。.saiverse に memory.db はあります。

## 分かったこと

### Chronicle の画面は、開くたびに memory.db へ書き込む

Chronicle の API はリクエストのたびに `_get_arasuji_db` を通り、その中で
`init_arasuji_tables` が走る。中身は読み取りではなく**書き込み**:

1. テーブルが無ければ作る
2. 互換 VIEW を作り直す
3. **旧 `arasuji_entries` テーブルが残っていれば、全行をページへ写して DROP する**

3 は一回きりの引っ越しだが、**旧テーブルを消すのは全行を写し終えた後**。途中で
倒れると旧テーブルが残るので、次に画面を開いたときも同じ場所で倒れる。「ひとり
だけ・毎回」という症状の形と一致する。

### 用意に失敗すると、開いた接続を誰も閉じられない (修正済み)

```python
conn = sqlite3.connect(...)
init_arasuji_tables(conn)   # ← ここで倒れると
return conn                 # ← ここに来ない
```

各エンドポイントは `finally: conn.close()` を持っているが、**`conn` に接続が
代入される前に例外が飛ぶ**ので到達しない。用意は書き込みなので、閉じられない
接続が SQLite の書き込みロックを握ったまま残る。

そのあと Memopedia を開くと、そちらも `init_memopedia_tables` (CREATE TABLE) で
書き込みに行くため、前の鍵が残っていれば待たされる。**「Chronicle の後に
Memopedia が終わらない」という順序依存はこれで説明がつく。**

同じ形が 3 箇所あった (族として一括で修正):

| 場所 | 内容 |
|---|---|
| `api/routes/people/arasuji.py` `_get_arasuji_db` | connect → `init_arasuji_tables` → return |
| `tools/utilities/memory_settings_ui.py` `_get_arasuji_connection` | 同じコード |
| `sai_memory/memory/storage.py` `init_db` | connect → 200 行超の CREATE / ALTER → return |

いずれも「開く」と「用意する (書き込み)」を 1 つの関数でやり、後者の失敗で前者が
漏れる形。`init_db` は本体を `_init_schema` へ切り出し、3 箇所とも
`try/except: conn.close(); raise` で塞いだ。

テスト: `tests/test_memory_db_connection_leak.py`。`sqlite3.connect` を包んで
作られた接続を控え、用意を失敗させたあと**その接続が実際に閉じているか**
(閉じた接続への `execute` が `ProgrammingError` になるか) を確認する。

### 調べきれていないこと

**「読み込みが終わらない」の体感を説明しきれていない。** SQLite の鍵待ちは既定
5 秒で諦めるので、本来はエラーになるはず。可能性は ①画面側が繰り返しの失敗を
表示せずローディングのまま回っている ②引っ越しそのものが重くて実際に何十秒も
かかっている、のどちらかだが**未確認**。

当初「Python 側のロックを握ったまま離していないのでは」と疑ったが、これは**外れ**。
`sai_memory/db_locks.py` の錠前はすべて `with` で取られており、例外が出ても必ず
離す。手動 `acquire()` は 1 箇所も無い。

**報告者の環境で引っ越しが何で倒れるかも未確定。** まはーの環境では未引っ越しの
ペルソナが 6 人いるが (エイド 0 件 / ルシア 31 件 / MIA 4 件 / 凪 16 件 ほか)、
複製の上で引っ越しを試したところ**全員最後まで通った** — つまり、まはーの環境は
単に画面をまだ開いていないだけで、データは健全。報告者の環境固有の何かが要る。

## 根本原因 (2026-09-03 確定)

報告者からの返答と診断ファイルで確定した。

- 返答: ①再起動して Chronicle に触らなければ Memopedia は開ける (壊れた状態はプロセスの中にある = 接続リーク説の裏付け) ②5 分待ってもエラー表示は出ず読み込み中のまま ③Chronicle タブは「まだ生成されていません」、手動整理のダイアログは 55,567 字で畳むものなし。
- 診断ファイル: 該当ペルソナの memory.db は 560 MB、**Chronicle は旧形式のまま 1443 件**、起動ログに `sqlite3.IntegrityError: UNIQUE constraint failed: memopedia_pages.id` が 10 回。

`memopedia_pages.id` の UNIQUE 違反を起こせる書き手は、`create_page` に明示の id を渡す呼び出しだけで、その中で旧行の id をそのまま使うのは `_migrate_legacy_arasuji_table` (旧 `arasuji_entries` → ページ) だけ。旧テーブルの id は PRIMARY KEY なので表の中に重複は無い。つまり **旧行の id を持つページが、移行の前から既に存在していた** — 前回の移行が途中で止まった状態。v0.3.4 までの移行は `create_page` を行ごとに commit していたので、途中の行で倒れれば (busy、タイムアウト、プロセス停止のどれでも) 一部だけがページになって残る。全行を commit した後の `DROP TABLE arasuji_entries` で倒れる形もありうる。どちらが起きたかはログからは特定できないが、どちらでも次の症状は同じ。移行は「旧テーブルが在るか」だけで再開の可否を判断し、行ごとの既存確認をしていなかったので、次からは最初の 1 行で UNIQUE 違反 → 例外 → (v0.3.2 までは) 接続が閉じられず書き込みロックが残る → Memopedia の初期化が後ろで待つ、が毎回繰り返された。Chronicle が「まだ生成されていません」に見えるのは、一覧の API が初期化の時点で倒れて中身を返せないため。

修正 (v0.3.5、`sai_memory/arasuji/storage.py`): 移行は既にページになっている id (trunk を除く chronicle ページ) を飛ばし、残りだけ写して DROP まで進む。再開したことを WARNING で残す。あわせて、全行を一つのトランザクションで写して最後に一度だけ commit する (途中状態の種類を増やさない)、再開時の short_id は既存ページ側の最大値も見て採番する (前回採番した番号を重ねない)、二接続が同時に移行へ入って負けた側が踏む IntegrityError は rollback して一度だけやり直す、例外時は rollback してから投げる。レビュー (ローカル LLM + Codex) の指摘を反映。`tests/test_memory_atlas.py::ChronicleLegacyMigrationTest::test_migration_resumes_after_a_run_that_committed_pages_but_not_the_drop` で固定。

報告者の環境では、v0.3.5 に上げて Chronicle を一度開けば残りの引っ越しが走って旧テーブルが消え、以後は Chronicle も Memopedia も普通に開けるはず。**確認してもらうこと**: Chronicle タブに 1443 件前後が並ぶこと、その後 Memopedia が開くこと、起動ログに `legacy migration resumed` の警告が一度だけ出ること。

### レビューの裁定 (2026-09-03)

ローカル LLM 1 巡 + Codex 1 巡。**採用**: short_id の再利用 (両方が指摘) / 行ごとの commit と rollback の欠如 / 二接続の競合 (rollback + 一度の再試行で対応、DB 単位のロックは足さない — 再試行で収束する) / 既存ページの判定に `is_trunk = 0` を足す (Codex medium)。**採用せず**: 「DROP TABLE が busy のときの明示的な再試行と、未完了を初期化失敗として扱う設計」(Codex medium) — memory.db は WAL なので読み手は DROP を止めず、busy は他の書き手のトランザクション中だけの一過性 (busy_timeout 5 秒)。倒れれば例外で 500 になり、次のリクエストが既存ページを飛ばして DROP をやり直すので収束する。「既存ページの内容・親・metadata を旧行と照合してから飛ばす」(Codex) — id は旧テーブルの PRIMARY KEY で、同じ id のページを作れるのはこの移行だけなので、id の一致で「移行済み」と言える。

## 確定させる手段

診断ツール (v1.2) に【5】を追加した。ペルソナごとに:

- `memory.db` の有無とサイズ
- Chronicle が旧形式のままか (`arasuji_entries` が table か view か) と件数
- 引っ越しを止める行の数え上げ (`level` が数値でない行など)
- **`memory.db` の複製を作り、本体と同じ引っ越しを実際に走らせて、倒れた場所を
  traceback ごと出す** (本物は読むだけ)
- 起動ログから Chronicle / Memopedia のエラーを traceback ごと抽出

検証: `level` が NULL の行を混ぜた環境で、数え上げ (「上から 2 件目」) と試運転
(`TypeError: int() argument ... not 'NoneType'`、`storage.py:210`) の両方が原因を
名指しすることを確認済み。

## 直したあとも残る設計上の疑問

**一覧を見るだけの画面が、なぜ毎回スキーマの用意と引っ越しを走らせるのか。**
引っ越しは一回きりの処理なので、読み取りの入口に毎回ぶら下げる必要はない。
起動時に一度だけ済ませる形にすれば、「画面を開くたびに同じ失敗を繰り返す」構造
自体が無くなる。今回は報告された欠陥の修理までにとどめ、この構造の見直しは
別件とする。
