# 一部の Memopedia 書き込みがアダプタのロックを取らず、追記を落としうる

**状態**: 検証待ち（2026-08-05 起票 / 2026-08-06 実装 → Sol レビュー 9 件 + Codex 8 巡の消し込み → 錠前を DB ファイルに紐づける工事まで完了）
**方向**: **錠前は DB ファイルの持ち物**（`sai_memory/db_locks.py`）。同じ DB を開いた書き手は、渡されなくても同じ錠前を受け取る（まはー裁定 2026-08-06、案A）
**誰待ち**: まはー（実機検証）。手順は下記「実機で見ること」

## 何が起きていたか

`Memopedia` はコンストラクタで `db_lock` を受け取る。**渡さないと自分で新しい `RLock` を作っていた**。同じ DB 接続（`adapter.conn`）を使っていても、生成箇所ごとに別の錠前で守られることになり、**排他が成立しない**。守っているつもりで守られていないので、壊れるまで誰も気づかない。

**根治（2026-08-06、まはー裁定・案A）**: 錠前を引数の手渡しから**DB ファイルの持ち物**へ格上げした（`sai_memory/db_locks.py`）。`lock_for(conn)` / `lock_for_path(path)` は同じ DB ファイルなら同じ錠前を返すので、**渡し忘れという事故がそもそも起こせない**。`Memopedia(conn)` も `SAIMemoryAdapter._db_lock` も、API のバックグラウンドワーカー（ペルソナ未ロードでも）も、全部ここから引く。

引数 `db_lock` は残してある——Memopedia を通らない書き込み（付箋テーブルなど）に同じ錠前を回すため。渡さなくても同じ錠前になる。

同じ接続へ他所から `commit()` が入ると、開いているトランザクションが途中で確定する。書き込みが競合すれば `SQLITE_BUSY` になる。

**とくに `entity_extractor` が危ない。** 追記が失敗しても、バッチ callback は例外を警告ログにして戻るだけで、再試行も永続キュー化もしない。**ペルソナの記憶追記が黙って落ちる。**

## 現況

生成箇所の大半は既に渡している。**規約は存在していて、数箇所が漏れている**。

| 渡している | 渡していない |
|---|---|
| `saiverse/memory_atlas.py`（8 箇所）<br>`saiverse/uri_resolver.py:968`<br>`sai_memory/curation_ops.py:1126`<br>`builtin_data/tools/memopedia_note.py:55` | **`sai_memory/memory/entity_extractor.py:422`**<br>**`api/routes/people/memopedia.py:36, 796`**<br>`sai_memory/memopedia/generator.py:397`<br>`sai_memory/arasuji/estimate.py:130` |

`scripts/` 配下の単体スクリプトは他の書き手と同居しないので対象外。

## なぜ今すぐ直さないか

`entity_extractor` は素の `conn` だけを受け取る（`apply_extraction_to_memopedia` などの公開関数のシグネチャにロックが無い）。渡すには呼び出し経路を通す必要があり、共有コードへ波及する。**本文 → Fragment 変換の案件（[intent](../intent/memopedia_body_to_fragment.md)）とは別の工事**として切り出す。

## どう直したか（2026-08-06 実装）

1. **`entity_extractor`**: `extract_and_reflect` / `make_batch_callback` に `db_lock` を通し、`Memopedia(conn, db_lock=...)` に。呼び出し元の Metabolism（`sea/session_lifecycle.py`）が `adapter._db_lock` を渡す
2. **`api/routes/people/memopedia.py`**: `_get_memopedia` は `adapter._db_lock` を直接渡す。二つのバックグラウンドワーカー（ページ生成 / ログからの一括構築）は**専用接続**を開くため、起動ルートで `_adapter_db_lock(manager, persona_id)` を解決して引数で渡す —— 接続が別でも、ロックを尊重する書き手同士は直列化される（debug.py の `_memopedia_session` と同じ流儀）
3. **`generator.py` / `estimate.py`**: `db_lock` パラメータを追加し、届く呼び出し元（API ルート）から渡した。`scripts/` 配下は単独 DB なので渡さない（従来どおり）
4. **失敗を失敗として扱う**（本体）: 握り潰しは三層あった —— callback 自身の `warning`、`executor.execute_plan` の `exception`＋continue、`bands._fire_identity_fragment_callbacks` の `exception`＋continue。callback の握り潰しを撤去し、executor は `ExecutionResult.extraction_failures` に entry id を記録、bands は out-param（`extraction_failures` リスト）で同じ器に積む。Metabolism は完了時に **ERROR ログ + 実行台帳の result + 画面通知**で表へ出す

**失敗を「Chronicle の failed」に畳み込まなかった理由**: チャンクは確定済みで、failed 再実行しても `source_ids` の冪等スキップにより抽出は再発火しない。丸ごと failed にすると「Chronicle は成功したのに失敗と記録され、再実行しても抽出は戻らない」という嘘の状態になる。だから成否は分離し、失敗の事実だけを消えない形で残す

5. **拾い直し**（まはー裁定 2026-08-06: 「失敗した部分は次回実行で再処理される」が期待される姿）: 失敗した entry id を付箋テーブル `entity_extraction_backlog`（persona memory.db）に貼り、**次の Metabolism の頭**で entry の `source_ids` からメッセージを読み直して同じ抽出をやり直す。成功したら付箋を剥がす。やり直しは 3 回まで（壊れたデータで毎晩 LLM 課金し続けない天井）—— 上限後も付箋は剥がさず残し、WARN で見え続ける（黙って諦めない）。entry や元メッセージが消えていて拾いようのない付箋だけは剥がして進む

## 消し込み（2026-08-06、Sol レビュー 9 件 + Codex 3 巡）

Sol の 9 件のうち 8 件を直し、Codex のレビュー 3 巡で出た 15 件のうち 13 件を直した。芯は五つ。

1. **失敗を失敗として扱う**（Sol F1 / Codex 二巡）: `ExtractionFailed` を新設。LLM 例外・空応答・読めない応答・`entities` が null や辞書・要素が辞書でない・`name` や `summary` が文字列でない —— これらは全部失敗。空リストで返すのは `entities` が空リストのときだけ。`notes` を素の文字列で返す LLM への読み替え（一文字ずつ Fragment になる穴）もここで塞いだ
2. **拾い直しを Metabolism の頭へ**（Sol F4）: 編纂の計画・確認・claim より手前。畳むものが無い回でも回収が走る。課金の関所は `AUTONOMOUS_CHRONICLE_ENABLED` 一本（Pulse 種別は Pulse の外で残留値になるので認可に使わない）。止まっているときは WARN で見せる
3. **付箋に取り置きと版**（Sol F2 / Codex 二〜三巡）: 版が一致したときだけ剥がす。回数は**取り置きの成立時**に数える（失敗時だけ数えると、途中で死んだ拾い直しが上限をすり抜けて毎時間 LLM を呼ぶ）。時間が掛かりすぎて取り戻された実行は、**書き込みの直前**（Memopedia のトランザクションの中）で `ClaimLost` になり何も書けない
4. **抽出の適用を単一トランザクションへ**（Sol F3/F6/F8）: `Memopedia.apply_entity_notes` / `upsert_page_by_title` / `page_snapshot`。探す・作る・書くを 1 ロック 1 トランザクションに束ね、途中で落ちたら何も残さない。同じ出所・同じ文・同じ日付の Fragment は二度作らない
5. **本文は逐語**（Sol F9）: `render_page_body` は手書き本文を原文のまま出す（存在判定にだけ strip）

**残っている（直さないと決めたもの）**

いずれも「ログからの一括再構築」という手動の保守経路に閉じた話で、本番の Metabolism 経路には掛からない。

- **先行版 `attempts` の意味変換**: 旧列は失敗回数、新しい意味は試行回数。テーブルは 2026-08-06 に作られたばかりで実機で一度も走っていないため、変換のための列を増やすより境界テストで挙動を固定するほうを選んだ
- **出所を持たない Fragment の重複キー**: いまは `(ページ, 本文, 日付)` の完全一致で見ている。ここに再構築の入力そのもの（メッセージの範囲など）を持たせれば厳密になるが、ペルソナのデータへのスキーマ変更になる。**既知の限界を承知で見送った**:
  - 同じ日の別バッチに同じ文が出ると、二件目が重複と判定されて作られない（知識としては同じ一文なので、失われる意味は無い）
  - 同じ範囲を作り直したとき、LLM の言い回しが少し変わると別の文として二重に入る
- **バッチ単位の `source_date`**: 日をまたぐバッチでは、同じ文が同じ日付になり片方が重複と判定されうる。抽出は 1 バッチ 1 回なので同じ文が二度出ること自体が稀で、日付でバッチを割る工事に見合わないと判断した

**再構築の再開位置は (時刻, 行番号) の組**（`start_after` + `start_after_rowid`）。時刻だけだと同じ秒のメッセージの順序を表せず、「その時刻より後」は境目の行を落とし、「その時刻から」は同じバッチを永久に取り直す。両方を実際に踏んだので、組で持つ形が正典（回帰テスト: `tests/test_memopedia_rebuild_cursor.py`）。

## 実機で見ること

見る場所は `~/.saiverse/user_data/logs/<起動時刻>/backend.log`、検索語は `extraction-backlog`。

**① 配線の確認（普通に動かすだけ）**

記憶の整理が走るたびに、この行が出る：

```
[extraction-backlog] 付箋なし — 拾い直しは不要 (persona=...)
```

出ていれば「拾い直しが Metabolism の頭で呼ばれている」ことの証拠。出ていなければ配線が切れている。

**② 拾い直しそのものの確認（失敗を作る必要がある）**

普通に動かしているだけでは走らない（走る条件は「前に抽出が失敗していること」）。意図的に作るなら:

1. 記憶整理用のモデル名を存在しないものへ変更 → 整理を 1 回走らせる
2. 付箋ができる（ログ `entity extraction failed ... 付箋 (backlog) に記録済み`、チャット中なら画面に「⚠ うち N 件で知識の書き出しに失敗しました。次回の記憶の整理で自動的にやり直します。」）
3. モデル名を戻す → 次の整理の頭で `[extraction-backlog] 拾い直しを開始` → `拾い直し結果: {'recovered': N, ...}`
4. 付箋テーブルが空になる:
   `sqlite3 ~/.saiverse/personas/<id>/memory.db "SELECT entry_id, attempts, state FROM entity_extraction_backlog"`

**画面に出る条件**: 拾い直しの通知が画面（考え中のステータス行）に出るのは、**チャット中に整理が走った場合だけ**。自動実行（まはーが見ていない時間）では通知の届け先がないので、ログだけになる。

## 経緯

- **2026-08-05**: 本文 → Fragment 変換の Codex レビュー（四〜七巡目）で繰り返し指摘された。変換は専用接続で `BEGIN IMMEDIATE` を張るため、その間に抽出器の書き込みが `SQLITE_BUSY` になりうる
- 変換側で回避する案（自律稼働が止まっているときだけ変換を許す）も検討したが、**まはー裁定で「抽出器側の問題」として切り分け**。呼び出し側で避けるのではなく、欠陥のある側を直す
- **2026-08-06**: 上記 1〜4 を実装。契約テスト（callback は握り潰さない / db_lock が Memopedia まで届く / executor が entry id を記録する）を追加
