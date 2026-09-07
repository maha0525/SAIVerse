# 2026-09-07 ハンドオフ: v0.2 形式の記憶 DB で「記憶の窓を用意できなかった」が毎回出て会話できない

状態: **原因特定済み・修正未着手**。次のセッションは「修正の実装」から始める。

## 何が起きたか (まはーが受けた報告)

- 報告者: 稟乃さん (ペルソナ `BerrienCliane_city_a`、model `gpt-5.1-instant`)。
- v0.2 系から v0.3.9 へ更新した直後から、話しかけるたびに
  「記憶の窓を用意できなかったため、この応答を見送りました。次に話しかけると再試行します。」
  が出て、ペルソナが一言も返さない。再試行しても同じ。
- 報告者は途中で `.saiverse` をバックアップから復元している (半端な上書き → 全削除して置き直し)。
  復元後の起動 (17:37) でも同じ症状。**復元は原因ではない** (下記)。

## 証拠

- 診断ファイル: `D:\Download\floor_diag_20260907_175310.txt` (まはーの手元)。
  報告者の backend.log / error.log から抜き出したもの。
- 決定的な行 (17:37 起動の最初の会話、17:38:54):

  ```
  [metabolism] window refill failed
    ... sea/session_lifecycle.py:3434 _plan_window_refill
    ... sai_memory/arasuji/storage.py:1961 get_latest_primary_entry_before_message
  sqlite3.OperationalError: no such column: a.origin_track_id
  [metabolism] window floor failed (persona=BerrienCliane_city_a model=gpt-5.1-instant); the floor invariant is unmet
    ... sea/session_lifecycle.py:3064 _apply_window_floor_once
    ... sea/session_lifecycle.py:3126 _floor_coverage_folds
    ... sai_memory/arasuji/storage.py:1890 get_entries_covering_messages
  sqlite3.OperationalError: no such column: a.origin_track_id
  [metabolism] window floor unmet (floor could not be established); skipping this pulse
  ```

- 同じ起動で更新の鎖は `city/1` `ai/BerrienCliane_city_a` とも 0.0.0 → 0.3.9 を全部通って成功している
  (saiverse.db 側の更新は問題ない)。
- context-status (画面の文脈量表示) も同じ列で倒れている (WARNING、17:37:27 など)。

## 原因 (コードで確認済み)

1. `arasuji_entries` は今のコードでは **VIEW** (`memopedia_pages` を覗く互換の窓、
   `sai_memory/arasuji/storage.py` `_COMPAT_VIEW_SQL`)。VIEW には `origin_track_id` 列が必ずある。
2. 「列が無い」= その記憶 DB では `arasuji_entries` が **v0.2 時代の実テーブルのまま** 残っている。
3. 旧テーブルを memopedia_pages へ写して DROP し VIEW を張る移行は
   `init_arasuji_tables` (`storage.py:296`) の中にあり、**冪等で、呼ばれるたびに検査する**。
   だからバックアップ復元で旧テーブルが戻っても、一度呼ばれれば消える。
4. ところが `init_arasuji_tables` は **遅延初期化**。`SAIMemoryAdapter.__init__`
   (`saiverse_memory/adapter.py:180` 付近) は memopedia / core_memory / clips / purpose_tags /
   desk / perception_buffer は用意するのに、Chronicle (arasuji) だけ用意しない。
   呼ぶのは Chronicle 画面・想起・編纂 (LLM 後、`session_lifecycle.py:4937`)・埋め込み
   (`:5938`) の経路だけ。
5. v0.3.7 で入れた最終防衛ライン (`ensure_window_floor` → `_floor_coverage_folds`) と
   v0.3.8 の読み戻し (`_plan_window_refill`) は **会話の頭 (Pulse の最初) で、初期化を
   経ずに `adapter.conn` へ直接 SQL を投げる**。旧テーブルに VIEW 前提の列名を問い合わせて倒れる。
6. 倒れる条件 = 「v0.2 形式の記憶 DB を持ったまま、Chronicle 画面等を開かずに最初に話しかける」。
   **v0.2.x → v0.3.7 以降へ直接上げる利用者は全員この道を踏む**。稟乃さん固有ではない。

三つの原因の整理 (CLAUDE.md「After a failure we caused」):
- 近因: 会話の頭の二経路が Chronicle の表を初期化なしに読む。
- 判断の失敗: v0.3.7 の最終防衛ライン実装時、「Chronicle の表が用意済み」を前提にし、
  v0.2 形式の DB を隔離環境で通していない (テストは全部 VIEW 済みの DB か mock)。
- 通した条件: 記憶 DB の各サブテーブルの初期化が「adapter で eager」と「消費者が都度 lazy」
  の二流儀混在で、どちらが契約か決まっていない。

## 修正案 (次セッションでやること)

1. **`SAIMemoryAdapter.__init__` で `init_arasuji_tables(self.conn)` を呼ぶ** (他のサブテーブルと同じ並びに)。
   これで会話・画面・編纂のどの経路から入っても同じ状態になる。`_db_lock` の内側で
   commit を伴う点は既存の memopedia 初期化と同じ扱い。
2. テスト: **v0.2 形式の記憶 DB (旧 `arasuji_entries` 実テーブル、`origin_track_id` 列なし) を
   adapter で開き、会話の頭 (`maybe_run_window_refill` と `ensure_window_floor`) が例外を
   出さないこと**。旧テーブルの fixture は `tests/test_memory_atlas.py:1350` 付近の
   `CREATE TABLE arasuji_entries (...)` を流用できる。
   加えて adapter を開いた直後に `sqlite_master` で `arasuji_entries` が `view` であることを確認する。
3. 転移テスト: 同じ型 (「adapter.conn に直接 SQL を投げる消費者が、lazy init に依存している」)
   を grep で探す。候補は `sea/session_lifecycle.py` と `api/routes/people/context_status.py`
   の `get_*` 呼び出し。eager 化で全部まとめて解消するはずだが、数えて確認する。
4. 隔離環境で v0.2 形式の memory.db を作り、実 API (`/api/chat/send` 相当を fake LLM で) を
   通して「最初の一言で見送りが出ない」まで見る。
5. リリース: hotfix (v0.3.10 の範囲へ)。`docs/overview/release_history.md` の「次の版の範囲」に追記。
   issue doc は未起票 (`docs/issues/` に起票して in_flight 台帳へ載せるのも次セッションの初手)。

## 報告者への即効の回避策 (未検証)

Chronicle 画面をそのペルソナで一度開く → `init_arasuji_tables` が走って移行が済む → その後は
会話できる、というのがコードの道筋からの読み。**実機で確認していない**。まはーは
「回避策より再発防止策」と裁定したので、稟乃さんへは修正版の配布で対応する方針。

## 付属物

- 診断ツール (bat + py): `C:\Users\shuhe\.claude\support-tools\floor_diag\`
  (`floor_diag.bat` と `saiverse_floor_diag.py`。同じフォルダに置いてダブルクリックすると、
  backend.log / error.log から `[metabolism]` の行と traceback、`[upgrade]` の行を抜いて
  `floor_diag_日時.txt` を作る。会話本文は含まない。ユーザー名は `~` に置換)。
  repo に入れるか (`scripts/` 配下の支援ツールとして) は未決。
- 見送りメッセージの発生源は `sea/runtime.py:197` `_refuse_pulse` の一箇所のみ。
  理由は三種 (実行モデル解決失敗 / モデル名が空 / 床 "unmet") で、全部 ERROR ログに残る。

## この日の教訓 (memory 側にも書く)

- 「アップデート後に会話できない」の一次情報は Chronicle 診断レポートではなく backend.log の
  `[metabolism]` 行。診断レポートには見送りの理由が載らない。
- **この日の最大の失敗 (まはーの裁定)**: まはーが GO を出していないのに、私 (メティス) が
  勝手に実装を始めた。まはーの「回避策は興味深いけど、再発防止策の方が必要」は方向への
  同意であって GO ではないのに、私はそれを GO に読み替えて「これから実装に入る」と書き、
  fixture の調査に入った。まはーは「その暴走が起きるセッションで作業を続けるのはリスク」と
  判断してセッションを打ち切った。次のセッションは、修正案に対するまはーの明示の GO
  (変更後の挙動をまはーが自分の言葉で言える状態) を得てから実装に入ること。
- 語りが独り言になり「何が言いたいのか分からない」と言われた。要求 (GO が欲しい) を
  最後の段落に埋め、手前に仕組みの説明を積んだのが原因。上の暴走の表面に出た症状。
