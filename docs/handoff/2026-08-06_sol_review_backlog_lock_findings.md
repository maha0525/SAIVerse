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

## 実機検証への影響

- ロック/backlog の「次の Metabolism 一回で実機確認」は **F1/F4 が直るまで意味を成さない** (静かな夜は回収が走らず、失敗も付箋に載らない)。実機検証は消し込み後に。
- 編纂 (a)(b) の本文結合そのものは Sol も保存則一致を確認。編纂再開の判断材料としては (a)(b) は健在。
- 変換 (aifi 適用済み) への影響なし — F7 は表示契約、apply の安全はサーバ側で担保されていた。
