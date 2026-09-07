# v0.2 形式の記憶 DB のまま話しかけると「記憶の窓を用意できなかった」が毎回出て会話できない

**状態**: 検証待ち (2026-09-07 実装・隔離検証・レビュー済み。PR のまはー確認 → v0.3.10 で配布)
**起票**: 2026-09-07 (v0.2 系から v0.3.9 へ更新した利用者 (稟乃さん、ペルソナ `BerrienCliane_city_a`) からの「話しかけるたびに見送りメッセージが出て一言も返らない」報告の調査。経緯の全文は [handoff 2026-09-07](../handoff/2026-09-07_window_floor_unmet_on_v02_memory_db.md))
**関連**: `saiverse_memory/adapter.py` `SAIMemoryAdapter.__init__` / `sai_memory/arasuji/storage.py` `init_arasuji_tables` / `sea/session_lifecycle.py` `_plan_window_refill` `_floor_coverage_folds`。[memory_db_connection_leak_on_init_failure.md](memory_db_connection_leak_on_init_failure.md) 末尾の「直したあとも残る設計上の疑問」が予告していた構造の見直しを、この issue で実行する。

## 症状

v0.2 系から v0.3.7 以降へ直接更新すると、話しかけるたびに
「記憶の窓を用意できなかったため、この応答を見送りました。次に話しかけると再試行します。」
が出て、ペルソナが一言も返さない。再試行しても同じ。バックアップ復元も効かない。

backend.log の決定的な行:

```
[metabolism] window refill failed
  ... sea/session_lifecycle.py _plan_window_refill
sqlite3.OperationalError: no such column: a.origin_track_id
[metabolism] window floor failed (...); the floor invariant is unmet
[metabolism] window floor unmet (floor could not be established); skipping this pulse
```

## 原因

1. 現行コードの `arasuji_entries` は `memopedia_pages` を覗く互換 VIEW で、`origin_track_id` 列が必ずある。「列が無い」= その記憶 DB では v0.2 時代の**旧実テーブルがそのまま残っている**。
2. 旧テーブルを写して DROP し VIEW を張る移行は `init_arasuji_tables` (冪等) の中にあるが、この関数は**遅延初期化**で、Chronicle 画面・想起・編纂・埋め込みの経路からしか呼ばれない。`SAIMemoryAdapter.__init__` は memopedia / core_memory / clips / purpose_tags / desk / perception_buffer / curation は起動時に用意するのに、あらすじ (Chronicle) だけ用意しない。
3. v0.3.7 の最終防衛ライン (`ensure_window_floor` → `_floor_coverage_folds`) と v0.3.8 の読み戻し (`_plan_window_refill`) は、**会話の頭で、初期化を経ずに `adapter.conn` へ直接 SQL を投げる**。旧テーブルに VIEW 前提の列名を問い合わせて倒れ、床が確立できず毎回見送りになる。
4. 倒れる条件 = 「v0.2 形式の記憶 DB を持ったまま、Chronicle 画面等を開かずに最初に話しかける」。**v0.2.x → v0.3.7 以降へ直接上げる利用者は全員この道を踏む。**

三つの原因の整理 (CLAUDE.md「After a failure we caused」):

- **近因**: 会話の頭の二経路が Chronicle の表を初期化なしに読む。
- **判断の失敗**: v0.3.7 の最終防衛ライン実装時、「Chronicle の表が用意済み」を前提にし、v0.2 形式の DB を隔離環境で通していない (テストは全部 VIEW 済みの DB か mock)。
- **通した条件**: 記憶 DB のサブテーブル初期化が「adapter で eager」と「消費者が都度 lazy」の二流儀混在で、どちらが契約か決まっていなかった。

## 修正

`SAIMemoryAdapter.__init__` の eager 初期化の並びに `init_arasuji_tables(self.conn)` を追加する。これで会話・画面・編纂のどの経路から入っても「開いた時点で移行済み」になり、二流儀混在も「adapter で eager」に一本化される。

転移テスト (同じ型が他に残っていないか、2026-09-07 実施): eager 列に居ない init は他に 5 つあるが、continuity / pocketbook / chunk_page_edge は `init_db` の中で既に eager、extraction_backlog / body_conversion は全消費者が使う直前に自分で init してから読む自給自足型で、**「他モジュールから初期化なしの生 SQL が飛んでくるのに lazy」なのはあらすじだけ**だった。

## 検証

- 回帰テスト: v0.2 形式の memory.db (旧 `arasuji_entries` 実テーブル、`origin_track_id` 列なし) を adapter で開き、(a) 開いた直後に `arasuji_entries` が VIEW になっていること、(b) 会話の頭が踏む読み (`get_latest_primary_entry_before_message` / `get_entries_covering_messages`) が例外を出さないこと。
- 隔離環境で v0.2 形式の memory.db を作り、実 API 経由で最初の一言に見送りが出ないこと。

## 経緯

- 2026-09-07: 報告受領・原因特定 (詳細は handoff)。まはー裁定は「回避策より再発防止策」— 稟乃さんへは修正版 (v0.3.10) の配布で対応する。診断ツール (bat + py) は `C:\Users\shuhe\.claude\support-tools\floor_diag\` に作成済みで、repo へ入れるかは未決。
- 2026-09-07 (実装と検証): adapter の eager 化 + 回帰テスト 2 件 (修正を外すと落ち、入れると通ることを検算済み)。隔離環境 (test_data、fake ではなく NEBULA のローカル LLM) の実 API で A/B を実施 — **修正なし**: v0.2 形式 DB への二言目で `sqlite3.OperationalError: no such column: a.origin_track_id` が `_plan_window_refill` 経路で再現 (報告者と同じ行。見送りメッセージまでは会話履歴が短く床が Chronicle に頼らないため到達せず)。**修正あり**: 同じ手順で起動時に移行が走り (table → view、旧行保存)、一言目・二言目とも LLM 成功、ログ全体で origin_track_id のエラー 0 件。A 面ではあらすじの表を初期化なしに読む第三の消費者 (`get_memory_weave_context`、WARNING 止まり) も見つかり、eager 化で同時に治っている。
- 2026-09-07 (レビュー): ローカル LLM (qwopus-27b) は指摘ゼロ。Codex adversarial は 3 件 — ① 移行 DDL が `_db_lock` の外 (high) → **既存流儀と同じで、この差分で新設された危険ではない**: `__init__` の既存 init 群 (memopedia ほか) も全てロック外で、`init_arasuji_tables` は従来から API ルートが別接続で並行に呼んでおり、並行 CREATE の受容と IntegrityError 再試行は v0.3.5 で実装済み。「移行を version 管理で一回きりに」は [memory_db_connection_leak_on_init_failure.md](memory_db_connection_leak_on_init_failure.md) 末尾から継続の構造課題で別件のまま。② 移行失敗で adapter 全体が開けなくなる (high) → **意図した境界**: memopedia 初期化の失敗と同じ扱いで、開けない記憶 DB は開いた時点で正直に失敗する (旧挙動は「会話だけが毎回黙って見送られる」で、これがこの issue の欠陥そのもの)。移行の再開可能性は v0.3.5 でテストつきで実装済み。③ 回帰テストが実セッション経路を通さない (medium) → storage 関数レベルのテスト + 上記の実 API A/B で補完した。
