# イベント配送・実行環境の到達保証の穴（Pulse queue / phenomenon worker / プロセス排他）

**発見**: 2026-07-31（[判断点の席の競合制御](judgment_seat_contention_and_event_loss.md) の Codex レビュー六巡目。同案件の範囲外として切り出し）
**状態**: 未着手（どちらも当該案件より前から在るプラットフォームの性質。頻度・実害の観測を待って優先度を判断する）
**関連**: `sea/pulse_controller.py`、`phenomena/manager.py`

## ① Pulse queue は上限超過で最古の request を黙って破棄する

`PulseController` の per-persona queue は上限（`QUEUE_LIMIT`）を超えると**最古の request を pop して捨てる**（ERROR ログのみ）。queued は「受付済みで消えない」として accepted 扱いする裁定（schedule W3 D4、phenomenon 応対の成否判定も同じ裁定に乗っている）と矛盾する瞬間がある — イベントが集中すると、accepted と報告済みの応対が後続に押し出されて消え、完了通知も再試行もない。

- 発生条件: 同一ペルソナに未処理 request が上限を超えて滞留（通常運転では稀。イベント連打・長考 Pulse 中の集中投下で起きうる）。
- 対処の方向: 破棄でなく受付拒否（submit 側へ False を返す）にすれば、送信側の失敗経路（backoff / 台帳）が既に在るので乗るだけで済む可能性が高い。破棄を残すなら「accepted の語義」から queued を外す再裁定が要る。

## ② PhenomenonManager の非同期 worker は phenomenon の戻り値を捨てる

`emit()` → 非同期 queue → worker の経路では `_execute_phenomenon` の戻り値（`inject_persona_event` の `"error: ..."` 等）は誰にも読まれず、成功として記録される。phenomenon が失敗を戻り値で表現しても、非同期経路では観測されない — 失敗の伝播は同期呼び出し（`invoke()`）にしか効かない。

- 対処の方向: worker が戻り値の `error:` 前置きを検出して WARNING/ERROR を出す（最小）。構造化結果 or 例外契約への移行（本修正）。

## ③ 正常戻りに畳まれた実行拒否が completed として記録される

`PulseController._execute_unlocked` は `_do_execute` が例外を投げなければ結果内容を問わず `runtime_outcome="completed"` を記入する。ところが persona 不在は空配列の正常 return、`SEARuntime.run_meta_user` は Playbook 未検出をエラー文字列の配列で正常 return する — つまり**応対が起動していないのに completed** になり、schedule の精算も phenomenon の成否判定も「配送済み」と読む。アドオンの meta_playbook が未 import の構成で顕在化する。

- 対処の方向: 実行拒否（persona 不在 / Playbook 未検出）を構造化 outcome か専用例外で `runtime_outcome` に伝播し、Playbook が実際に完走した場合だけ completed にする。

## ④ cancelled の語義メモ（挙動は許容、語義がずれている）

`_classify_dispatch_outcome` は `runtime_outcome="cancelled"` を「復帰 queue に残っていて消えない」として accepted 扱いする（W3 D4）。しかし復帰 queue に積まれるのは割り込みによる中断だけで、`cancel_active_generation()`（ユーザーの明示停止）は queue に戻らない。**ユーザーが止めたものを自動再実行しないのは挙動として妥当**なので実害は薄いが、「消えない」という根拠説明は明示停止経路には当てはまらない。cancelled に「復帰予約の有無」を持たせれば語義が揃う。

## ⑤ 実行開始前に割り込まれた Pulse の pre_spells が失われる

割り込みは「current 登録〜最初の LLM node 到達」の間にも起きうるが、pre_spells の実行はその node 内で始まる。この窓で中断されると pre_spells は一件も実行されないまま終わり、復帰 request にも載らない（復帰 request に pre_spells を**載せない**のは意図的 — 割り込み時点ではほぼ実行済みで、載せると副作用 Spell が割り込みのたび再実行されるため。2026-07-31 の席競合案件・七巡目で「常にコピー」を試して二重実行の穴になり、従来挙動へ戻した経緯がある）。

- 根治には request 単位の pre_spells 実行状態（pending / started / finished）と、割り込みと Spell 開始を同じロックで裁定する同期境界が要る — PulseController / SEARuntime の中核設計。
- 窓は狭く（pre_spells 付き Pulse × 開始直後の割り込み）、従来からの挙動。

## ⑥ プロセス排他はホーム単位 — 別 SAIVERSE_HOME から同じ DB を二重運転できる（設計課題）

runtime marker は `SAIVERSE_HOME/.runtime` に住むため、**別のホームを指す環境変数 + 明示 DB パスの合わせ技**で同じ DB を見る 2 プロセスは互いのマーカーを見ない。席競合案件（2026-07-31）で入れた CITYNAME 自動修復の関所（`another_running_process_owns_db`）も同じ走査範囲なので、この合わせ技には効かない。関連して、`main.py` は marker 取得の後・City 検査の前に backup / migration / スキーマ確認を走らせるため、拒否が確定する前に live DB へ書き込みが起きうる。

- 根治の方向: **DB 隣接の所有レコード**（DB ファイルの隣に置く、稼働 City 一覧を持つロックファイル）で排他をホーム非依存にする + `main.py` の起動前検査（preflight）を「marker → 所有検査 → backup/migration」の順に並べ替える。正規の複数 City 同居（別 City 名・同一 DB）は許し続ける必要があるので、単純な排他ロックでは足りない — 小さな intent を切って設計する。
- 発生条件はオペレーターの明示的な多重設定で、通常運転では起きない。優先度はそれ相応。
- 派生: この二重運転が成立すると、判断の per-persona Lock も分断されるため、`build_judgment_args` 内の帳簿系副作用（day_open の `decay_desires` — 状態遷移は日付基準で冪等だが、履歴 append を伴う）が並走で二重になりうる。根治は⑥の排他が塞ぐのが正道（args 構築から副作用を分離する再設計は、単一プロセスでは不要な複雑さ）。

## メモ

- どちらも「送った側は成功と思っているが、届いていない」形の穴で、実行台帳の outbox（at-least-once + dead 終端）が守っている経路とは別の、**Pulse 起動そのものの到達保証**の話。
- 判断点側（席の競合制御の案件）は、この穴の手前までを型付き成否（`dispatch_schedule_fire` の観測）で判定するよう直してある。
