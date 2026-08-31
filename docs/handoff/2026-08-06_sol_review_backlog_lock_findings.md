# 2026-08-06 Sol レビュー (2 回目) — 抽出 backlog / ロック配線 / 編纂 (a)(b) / URI 表示

**位置づけ**: Fable セッションの規律 (レビュー 1 回・消し込みは Opus の別セッション) に基づくハンドオフ。まはーの明示指示で回した追加の 1 回。対象 = `2191d44` 以降の branch 差分 (`0b95a26` ロック配線+握り潰し撤去 / `4a4dc00` 抽出 backlog / `a459b86` 変換 UI 改稿 / `ae1bf6d` 編纂 (a)(b)+URI 表示)。

**Verdict: needs-attention (No-ship)。指摘 9 件 (high 6 / medium 3)。全件を HEAD で裏取り済み — 9 件とも事実** (うち 2 件は「構造は事実、発火には並走条件が要る」)。本文結合 (a)(b) の保存則と肥大判定の一貫性は問題なしと明言された。

## 消し込みの優先順 (Fable の見立て)

**最優先 = F4 と F1。** この 2 件が直らない限り「失敗した抽出は次回 Metabolism で自動再処理される」というまはーへの約束が成立しない。F3/F5/F6 は並行性・冪等性の本丸で、まとめて一つの設計 (claim + 単一 tx 適用) で解くべき。F7〜F9 は独立の小修正。

## Findings と裁定

### F1 [high] 抽出の主要な失敗が成功扱いになり backlog に載らない — ✅ 事実

`entity_extractor.extract_entities` (288-301) は LLM 例外・空応答をすべて `[]` で返す。`_parse_extraction_response` の不正 JSON も同様。callback は正常終了するので executor/bands の記録層に届かず、**retry 中なら「成功」として付箋が剥がれる**。正常な entities=[] と依存障害を区別できていない。

**裁定**: 要修正。型付き例外 (例: `ExtractionFailed`) を導入し、LLM 例外・空応答・不正スキーマは raise、正常な空抽出だけ `[]`。client 障害が backlog を作る統合テストを添える。

### F2 [high] backlog に claim/version が無く、二重発火と新しい失敗の消去が可能 — ✅ 事実 (構造)

`retry_extraction_backlog` (515-553) は全行 SELECT → 無保護で長時間 callback → entry_id だけで無条件 DELETE。並行 retry の二重抽出、「他方が失敗を upsert した直後に成功側が DELETE」の lost-failure が構造上可能。発火には同一ペルソナの Metabolism 並走が要る (通常は claim で直列) が、API の build-from-logs ワーカーとの並走はありうる。

**裁定**: 要修正。F3 と同じ設計で解く — backlog に状態 (pending/in_progress) と version を持たせ、単一 SQL で claim、削除は version 一致時のみ。

### F3 [high] retry が部分適用済み Fragment を重複生成する — ✅ 事実

`reflect_to_memopedia` (342-379) は summary 更新・page 作成・各 create_fragment が**個別 commit** (storage.create_fragment 内 commit)。複数 note の途中失敗 → 先行 Fragment は残る → backlog がチャンク全体を再抽出 → 同じ知識を新 UUID で重複挿入。

**裁定**: 要修正。抽出結果確定後に「page upsert + summary + 全 Fragment」を共有ロック内の単一トランザクションで適用。最低限 (entity_id, chronicle_entry_id, 内容 digest) の一意制約で再適用を冪等化。

### F4 [high] backlog retry が Metabolism の早期 return より後にあり、次回に回収されない — ✅ 事実・最重要

retry 呼び出しは session_lifecycle.py:3219 だが、「編纂対象なし → return "ok"」が 2983 にある。**失敗の翌日に新しい編纂対象が無ければ付箋は永久に残る** (確認拒否 / timeout / AUTONOMOUS_CHRONICLE_DISABLED / 先行 claim の return も同様に手前)。「次回 Metabolism の頭」という契約 (まはー承認 2026-08-06 の根拠) を満たしていない。

**裁定**: 要修正 (最優先)。回収を Chronicle の dry-plan・確認・claim から独立させ、adapter/client 準備直後の Metabolism 冒頭へ移す。「新規 Chronicle ゼロの回でも回収される」回帰テストを固定。

### F5 [high] db_lock 配線後も未ロック writer が残る — ✅ 事実

`extract_and_reflect` は Memopedia 生成**前**に `init_memopedia_tables(conn)` を未ロックで commit (425-426)。既存ページの summary 更新は `storage.update_page` 直呼びでロック迂回 (352-356)。backlog の CREATE/UPSERT/DELETE も全て未ロック。Sol の同族列挙 (estimate.py:133 / api memopedia.py:541,750 / arasuji.py:71 / session_lifecycle.py 3 箇所 / 既存の head_pipeline・recall) も妥当。

**裁定**: 要修正。issue の言う「規約」を optional 引数から**接続境界の必須所有物**へ格上げする設計判断が要る (Opus セッションで、まはーと)。

### F6 [high] メソッド単位ロックでは read-modify-write が原子的でない — ✅ 事実 (構造)

`generator.py:714-735`: find_by_title → 文字列連結 → update_page が別々のロック区間。間に別 writer が入ると lost update / 同名二重作成。entity_extractor の find_by_title→create_page も同型。発火には同一ページへの並行 writer が要る。

**裁定**: 要修正。Memopedia に「検索+再検査+作成/追記」を 1 ロック 1 tx に束ねた原子的 upsert/append API を追加し、呼び出し側を寄せる。

### F7 [medium] 選択反映前でも stale preview のまま変換確認へ進める — ✅ 事実 (UI 契約)

MemopediaConversion.tsx: 実行ボタンは `restating` / decisions≠syncedRef を無視する。apply はサーバが decisions で再検算+指紋検査するので**危険な変換は通らない**が、画面の集計と実行内容が食い違いうる。

**裁定**: 要修正 (軽微)。未同期・再下見中・再下見失敗時は実行ボタンを無効化。

### F8 [medium] URI のページ・Fragment・子一覧が同一スナップショットでない — ✅ 事実 (構造)

`_format_memopedia_page`: page 取得 (ロック内) / render_page_body 内の get_page (未ロック) / get_fragments (別ロック区間) / get_children (未ロック) が別時点。読み取り専用なので実害は「混成表示」に限られる。子取得例外の空化も観測不能化として指摘どおり。

**裁定**: 要修正 (F5/F6 の原子的 API 設計と同じ束で)。page+fragments+children を 1 ロックで返す snapshot API を Memopedia に。

### F9 [medium] render_page_body 切替で URI の手書き本文が逐語でなくなる — ✅ 事実 (今日の退行)

render_page_body は `page.content.strip()` を出力に使う (core.py:1097-1098)。旧 URI 経路は raw content だった。先頭インデント・末尾改行・行末スペース (hard break)・空白のみ本文が URI 出力から落ちる。DB は壊さないが、LLM が読む本文が変質する。

**裁定**: 要修正 (軽微)。存在判定だけ strip、出力は原文。空白保存の回帰テストを追加 (memory_atlas 側の挙動変更を含むため、そちらの表示も確認)。

## 消し込みの結果 (2026-08-06、Opus セッション)

**Sol の 9 件のうち 8 件を実装。F5 だけは「instance は塞いだが、構造の格上げはまはーの判断待ち」。**
その後 Codex (Luna) のレビューを回し、出た指摘のうち大半を直した (巡ごとの内訳は下表)。

実装の芯・直さないと決めた 2 件・残っている判断は、issue 側
([memopedia_writers_bypass_adapter_lock.md](../issues/memopedia_writers_bypass_adapter_lock.md)
の「消し込み」節) に正典を置いた。ここには**レビューが見つけた、当初の 9 件に無かったもの**だけ残す。

### Codex 3 巡で新しく見つかったもの (裏取り済み・全部事実)

| 巡 | 深刻度 | 何が壊れていたか | 処理 |
|---|---|---|---|
| 1 | critical | 拾い直しが編纂の確認ダイアログを迂回して LLM を呼ぶ | 関所を追加。3 巡目に `AUTONOMOUS_CHRONICLE_ENABLED` 一本へ整理 |
| 1 | high | `{"entities": null}` 等の壊れた応答が「抽出ゼロ」として成功扱い | 応答の検証を厳格化 |
| 1 | high | 出所を持たない再構築で冪等判定が効かない | 出所なし同士で突き合わせ (3 巡目に日付も追加) |
| 2 | high | `_current_pulse_type` の残留値を認可の根拠にしていた | 判定から撤去 |
| 2 | high | 途中で死んだ拾い直しが `attempts` を増やさず、上限をすり抜けて毎時間 LLM を呼ぶ | 回数を取り置きの成立時に数える |
| 2 | high | 失敗バッチを飛び越えた再開位置を返し、完了メッセージが「その範囲だけやり直す」と嘘をついていた | 再開位置を実際に処理したところまでに。状態 `partial` を追加 |
| 3 | high | 取り戻された古い実行の**書き込み**は止まらない (後片付けの版検査では遅い) | `precondition` を書き込みトランザクションの中へ通し、`ClaimLost` で拒否 |
| 3 | high | 同じ秒がバッチの境目をまたぐと、境目の行が次回の取得から落ちる | 再開の取得条件を「その時刻から」へ (重ねた分は重複検査が受け止める) |
| 3 | medium | Chronicle を切ると付箋が黙って永久停滞 | 止まっていることを WARN で毎回見せる |
| 3 | medium | `partial` の件数が実処理数と食い違う | 処理・失敗・スキップを別々に数える |
| 4 | high | **私が 3 巡目の修正で作った退行** —「その時刻から」で同じバッチを永久に取り直す | 再開位置を (時刻, 行番号) の組へ |
| 4 | high | 通常経路の付箋の記帳が錠前を通らず、記帳失敗を warning で握り潰していた | `db_lock` を executor / bands へ通し、記帳失敗は ERROR へ |
| 4 | medium | 上限で止まった付箋が「拾い直せる枚数」に入らず、警告の対象から消えていた | 全体の枚数も数えて毎回知らせる。手動入口の早期 return より手前へ移動 |
| 4 | medium | 画面が取得件数・スキップ件数を隠していた | 全部出す |
| 5 | high | **付箋に残せなかった失敗まで「次回やり直します」と報告していた** | 拾い直せる分と分けて持ち、ログ・台帳・画面の三箇所で正直に出す |
| 5 | medium | 再構築の arasuji 初期化がロックの外 | 錠前の内側へ |
| 5 | medium | 拾い直しの例外が WARN 止まり | ERROR へ。付箋が残ることまで書く |

### 繰り返し出たが、採らなかった提案

- **記帳失敗で Metabolism 全体を failed にする**: 付箋に残せなかった時点でその抽出は failed にしても戻らない (確定済みチャンクは `source_ids` で冪等スキップされ `batch_callback` が再発火しない)。全体を failed にすると「Chronicle は成功したのに失敗と記録され、再実行しても戻らない」嘘の状態を作るだけ。**この案件で既に一度出ている裁定と同じ形**なので、繰り返し出ても答えは変わらない。代わりに失われた事実を三箇所で見せる
- **出所なし Fragment への provenance 列** (3 巡連続): ペルソナのデータへのスキーマ変更で、対象は手動の再構築経路だけ。落ちるのは「同じ日・同じページ・同じ一文」の二件目で、知識として失われるものがない。「黙って落ちる」という芯だけ受けて、**重複で作らなかった件数を結果と画面に出す**ようにした
- **失敗したら以降のバッチを止める**: 壊れた 1 バッチが以降を永久にせき止める (この案件で潰してきた「静かに止まる」病そのもの)。やり直しの無駄はあっても取りこぼしはない形を選んだ

**私が自分で見つけた退行**: `extract_entities` が例外を投げるようになったため、ログからの一括再構築が
1 バッチの失敗で丸ごと止まるようになっていた (API 経路と CLI の両方)。バッチ単位で捕まえて数え、報告に出すよう修正。

**検証**: フルスイート 3936 passed / 3 skipped。フロントの型検査も通過。実機検証は錠前の判断のあと。

## 実機検証への影響

- ~~ロック/backlog の「次の Metabolism 一回で実機確認」は **F1/F4 が直るまで意味を成さない**~~ → F1/F4 とも消し込み済み。実機で見るべきは「静かな夜に付箋が拾い直されること」と「拾い直しの通知が画面に出ること」。
- 編纂 (a)(b) の本文結合そのものは Sol も保存則一致を確認。編纂再開の判断材料としては (a)(b) は健在。
- 変換 (aifi 適用済み) への影響なし — F7 は表示契約、apply の安全はサーバ側で担保されていた。
