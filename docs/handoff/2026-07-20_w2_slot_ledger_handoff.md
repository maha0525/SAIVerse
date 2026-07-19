# W2 (実行台帳 Phase 2 — 時間割と予算) セッション走行メモ (2026-07-20)

**用途**: このセッション (Fable/メティス) の確定設計と委譲・検収の再開点。工程の真実は [完了計画書](../overview/audit_remediation_plan.md)、finding の真実はレビュー台帳と [自律行動監査](2026-07-14_autonomy_judgment_schedule_audit.md)。ここはセッション固有の走行メモ。

**スコープ** (計画書 W2): A1 (day_open 全置換原子性) / A5 (作業コマ予算精算原子性) / A6 (done 保存失敗→Episode 永久 open)。

**進行**: 調査 (済み) → 設計 (本書 = 確定) → 実装 (Chunk 委譲) → 検収 (メイン) → コミット。

---

## 調査で確定した現状 (行番号は 2026-07-20 時点)

### ストレージ境界 — W2 設計の背骨

- **slots (`slots_json`) も予算メタ (`meta_json`) も同じ `PersonaDayPlan` 行・同じ world DB (saiverse.db)**。今は `_write_slots` (→`_upsert_plan_slots`) と `update_plan_meta` が別々の Session で別々に書くが、**同一行なので単一トランザクションに統合できる**。
- **episode (`Episode` テーブル) も台帳 (`execution_ledger` / `execution_outbox`) も world DB**。→ A5/A6 の精算 unit-of-work (予算 + slot done + episode close + 台帳 applied) は **すべて world DB 内の単一 commit に閉じる。outbox は不要** — memory.db を跨ぐ書き込みが精算段階に無いため (ハンドラ `run_work_session` が memory.db に書くのは「running 区間」で、精算とは別)。
- 台帳 API (`saiverse/execution_ledger.py`): `mark_applied(execution_id, result=, outbox_items=, session=)` は `session=` を渡すと commit せず呼び出し元の 1 commit に台帳更新を同梱できる (W1 で確立)。**`mark_running` には現状 `session=` が無い** → W2 で追加が要る。

### A1 — day_open finalize の cancel→save 逆順 (`builtin_data/tools/judgment_finalize.py:280-321`)

- `_finalize_day_open` は `cancel_scheduled_slots()` (:282) を `save_day_plan()` (:284) の **前** に呼ぶ。`save_day_plan` はライフ範囲正規化後にコマが 0 件だと `ValueError`。この時 DB の旧 plan は不変だが EventScheduler 予約だけ消える → 「plan は見えるが自動発火しない」孤児。
- 正しい原子性の手本 = `replace_remaining_slots` (`day_plan.py:1989-2053`): **検証で先に raise** → 通ってから `cancel_scheduled_slots` → `_upsert_plan_slots` → `schedule_day_plan` 再 push。「失敗時は plan も予約も一切変更しない」と明記済み。

### A5 — 予算精算の二重書き + 握り潰し (`day_plan.py:_fire_slot:2348`, `consume_budget:695`, `consume_life_rounds:1389`)

- `_fire_slot` 手順: 予算ゲート → 移動 → `_update_slot(FIRED)` (:2428、ハンドラ前に永続化) → episode open → ハンドラ → `consume_budget` + `consume_life_rounds` (:2482/2490、**両方別々の try で握り潰し**) → 無条件 `_update_slot(DONE)` (:2499) → episode close。
- ライフのある日は `get_budget_state` (:667) が **`consume_life_rounds` の `lives[].used_rounds` を正典**とし `consume_budget` の旧日次台帳は無視する。→ `consume_life_rounds` だけ転ぶと正典が 0 のまま = 残高が減らず後続コマが予算超過実行。
- 予約額 = `_effective_budget_rounds(slot)` (:2202、`budget_rounds` or `DEFAULT_BUDGET_ROUNDS`)。予算ゲート対象は `_BUDGET_GATED_KINDS` (:227、ハンドラ登録の `consumes_budget=True`) のみ。

### A6 — done 保存失敗が Episode を永久 open (`day_plan.py:2499-2502`, `2270 _close_slot_episode`)

- `_update_slot(DONE)` (:2499) が例外 → 直後の `_close_slot_episode` (:2502) に到達せず episode が open のまま。slot は `fired` のまま。
- watchdog (`reschedule_pending_slots` / `find_lost_slot_reservations:1938`) は二重発火防止のため `fired` を**意図的に無視** → 自動回復対象から永久に外れる。
- open episode は「いま」の唯一の正典で、SEA の全メッセージ保存が最後の open episode を `origin_episode` として自動継承する → 以後の記録が完了済み slot へ誤帰属。
- episode (`saiverse/episodes.py`): `open_episode:229` / `close_episode:297` / `set_digest_ref:364`。いずれも `manager.SessionLocal()` の自前 Session。**`session=` は無い** → W2 で追加が要る。

### 回復 tick (`saiverse/execution_ledger_wiring.py`)

- `_recovery_tick:139` = running 期限監視 (#3、`recover_stale_running(max_age_seconds=3600)`) + prepared 回収 (#2、判断点のみ) + pending 配送 (#1)。60 秒周期。
- **`recover_stale_running` (:793) は全 running を一括 unknown 化する** → slot.fire を掴むと episode が永久 open で unknown 化する。**slot.fire は汎用 sweep から除外が要る** (W2)。

---

## 確定設計 (私 = Fable の裁定)

### 全体方針

- **A5 と A6 は同一患部 (`_fire_slot`) — 一つの実行単位で解く**。各コマ発火に台帳実行 `kind="slot.fire"` を採番し、`予約(mark_running) → ハンドラ(running) → 精算(mark_applied)` の三区間に分ける。**精算は outbox 不要の world-DB 単一 commit** (ストレージ境界の帰結)。
- **A1 は別物 — world DB 内の順序修正**。台帳結線は不要 (finalize 全体は既に W1 の judgment execution 下にある)。原子的な全置換 API に統合する。

### D1. `slot.fire` の kind と冪等キー (A5/A6)

| 項目 | 値 |
|---|---|
| kind | `slot.fire` |
| idempotency_key | `{persona_id}:{plan_date}:{index}` |
| payload | `{"persona_id", "plan_date", "index", "kind"(コマ種別), "reserved_rounds"(gated のみ), "episode_ref"(予約 tx で確定)}` |

- コマは 1 日 1 index で一度だけ発火する自然な境界。冪等キーが EventScheduler 二重発火・watchdog 再 push の重複を台帳で吸収する。
- **slot ⇄ 実行の紐付けは台帳 payload が持つ (slot dict に新フィールドを増やさない)** — recovery は payload から `episode_ref` と slot 座標を復元する。`reserved_rounds` も payload に凍結 (recovery の保守精算で使う)。

### D2. `mark_running` に `session=` を追加 (台帳 API)

`mark_applied` と対称に `mark_running(execution_id, *, session=None)`。`session` 指定時は commit せず呼び出し元の 1 commit に prepared→running 遷移を同梱する。予約 tx を単一 commit にするために必要。

### D3. episode open/close に `session=` を追加 (episodes.py)

`open_episode(..., session=None)` / `close_episode(..., session=None)`。指定時は commit せず与えられた Session 上で Episode 行を INSERT/UPDATE する。予約 tx で episode open、精算 tx で episode close を同梱するために必要。**自前 Session 経路 (session=None) は現状不変** — 既存呼び出しは無傷。

### D4. `_fire_slot` の三区間化 (A5/A6 の核)

予算ゲート・繰り下げ・移動は現状のまま。移動の後、ハンドラ有りの全コマ (gated/非 gated 問わず、A6 は episode を開く全コマに効く) を台帳実行で包む:

1. **claim**: `claim_execution("slot.fire", key, persona_id, payload)` → `(exec_id, runnable, _)`。`runnable=False` → 既に発火済み (running/applied/completed) → log + return (二重発火ガード)。`prepared` 再利用 (crash 後の watchdog 再 push) は runnable=True で resume。
2. **予約 tx (world-DB 単一 commit)**: 一つの `manager.SessionLocal()` で —
   - `mark_running(exec_id, session=db)` (prepared→running、不変条件 1)
   - slot `pending/deferred → fired` (slots_json、同 session)
   - gated なら予算を `reserved = _effective_budget_rounds(slot)` だけ消費 (meta_json、同 session。**`consume_budget`/`consume_life_rounds` の二重書きを廃し、ライフのある日は `lives[].used_rounds` 正典に一本化**)
   - `open_episode(..., session=db)` → episode_ref、それを台帳 payload に書き戻す (同 session)
   - commit。**失敗時は全ロールバック** (slot pending・予算不変・episode 無し・台帳 prepared のまま) → ハンドラを呼ばず return。watchdog が pending を再 push、claim が prepared を再利用して安全再実行。
3. **ハンドラ実行** (running 区間): `handler(...)` → `actual_used`。memory.db への書き込みはハンドラの自己責任。
4. **精算 tx (world-DB 単一 commit)**: 一つの Session で —
   - gated なら予算を `actual_used - reserved` だけ調整 (通常は返金。meta_json)
   - slot `fired → done` (slots_json)
   - `close_episode(..., session=db)`
   - `mark_applied(exec_id, session=db, result={"kind":"slot.fire","reserved":R,"used_rounds":actual,...})` (running→applied、outbox 無し)
   - commit 後に `mark_completed(exec_id)` (outbox 無しなので applied→completed 即遷移可)。
   - **失敗時は全ロールバック**: slot fired・episode open・台帳 running・予算は予約額のまま (予約 tx で確定済み)。→ **後続コマは予約額を消費済みとして見る (A5 の「精算失敗時も予約額は残す」を満たす)**。recovery が settle-close する。
5. **ハンドラ例外** (防御経路、`run_work_session` は raise しない契約): finally で episode を best-effort close + `mark_unknown(exec_id)` (LLM が動いたか不明)。slot は fired のまま (「実行したが完了記録なし」の観察)。予約額は保持。

### D5. slot.fire の回復 (A6、tick に新設)

`_collect_stale_slot_executions` を `_recovery_tick` に追加:

- running の `slot.fire` で `SLOT_SETTLE_DEADLINE`(仮 300 秒 = 作業セッションが精算せず 5 分走り続けたら crash とみなす) 超過を列挙。
- 各行: payload から `episode_ref`/slot 座標を読み → **精算 tx と同形の settle-close を LLM 再実行なしで実行** (episode close + slot done + `mark_applied`(予約額は保持、返金しない保守精算) + `mark_completed`)。running のみ触るので冪等 (二重 pass の 2 本目は applied を見て skip)。
- **`recover_stale_running` の汎用 sweep から `slot.fire` を除外** (`exclude_kinds=("slot.fire",)` 追加)。slot.fire が 3600 秒 unknown sweep に掴まれて episode 永久 open で unknown 化するのを防ぐ。slot 回復は tick 内で汎用 sweep より前に置く。
- 「行動を生む」ではなく「掃除」(LLM 再実行しない) なので手動モードでも止めない。

### D6. A1 — `replace_day_plan` (原子的全置換)

`day_plan.py` に `replace_day_plan(manager, persona_id, plan_date, new_slots) -> (pushed, notes)` を新設 (`replace_remaining_slots` の手本に倣う):

1. `save_day_plan` と同じ検証・ライフ範囲正規化を **先に** 実行 (0 件なら `ValueError`、この時点で DB もスケジューラも一切未変更)
2. 通ってから `cancel_scheduled_slots`
3. `_upsert_plan_slots`
4. `schedule_day_plan` 再 push

`_finalize_day_open` は `cancel_scheduled_slots` + `save_day_plan` + `schedule_day_plan` の三連呼び (:282-296) を `replace_day_plan` 一本に置換。`ValueError` 時は **何も変更されない** ので `applied=False`・エコーを「既存の時間割を維持しました」に修正 (現状の「編成されていません」は旧 plan が残るので実状態と不一致)。`init_budget_ledger` は保存成功後に現状どおり呼ぶ。

### 実装 Chunk (順次委譲、並列なし)

- **Chunk A (台帳/episode API)**: D2 (`mark_running` に session=) + D3 (`open_episode`/`close_episode` に session=) + `recover_stale_running` に `exclude_kinds`。回帰: session 版が commit せず呼び出し元 commit に同梱される / session=None 既存経路が無傷 / exclude_kinds で slot.fire が sweep されない。
- **Chunk B (`_fire_slot` 三区間化)**: D1 + D4。予約 tx・精算 tx・ハンドラ例外・二重発火ガード・予算二重書き廃止。回帰 (A5): 予約 tx 失敗でハンドラ 0 回・slot/予算不変 / 精算失敗で予約額残存・後続コマ予算超過なし / 精算再試行で used 一度だけ増・slot 一度だけ done。回帰 (A6): done 保存失敗・episode close 失敗の各単独/両方で再起動後 recovery が一度だけ done/closed へ収束 / recovery 二重処理でハンドラ再実行・予算二重・episode 二重 close 無し / stale fired 中と回復後で後続 `origin_episode` が完了 slot へ誤帰属しない。
- **Chunk C (slot 回復 + A1)**: D5 (`_collect_stale_slot_executions`) + D6 (`replace_day_plan` + finalize 差し替え)。回帰 (A1): 検証失敗/全除外/保存失敗の各ケースで旧 plan・旧予約とも不変 / 成功時は旧 index 予約が残らず新 plan のみ予約 / エコーが実状態と一致。回帰 (D5): stale running slot.fire が deadline 超過で settle-close される / 二重 tick で冪等。
- **Docs (メイン)**: execution_ledger.md ステータス (Phase 2 済み) / life.md の予算精算節 (二重台帳廃止・予約精算) / judgment_points.md day_open 全置換の原子性 / 計画書 W2 状態 / レビュー台帳 A1/A5/A6 消し込み / in_flight。

### 検収チェックリスト (メイン)

- [ ] Chunk A: `mark_running(session=)` が commit せず呼び出し元 commit に乗るか / episode の session 版が Episode 行を同 Session で INSERT/UPDATE するか / 台帳なし・episode session=None の既存テストが無傷か
- [ ] Chunk B: 予約 tx が単一 commit か (slot fired + 予算 + episode + running が全 or 無) / 予算が `lives[].used_rounds` 一本化されたか (旧 `consume_budget` 二重書き消滅) / 精算失敗時に予約額が残るか
- [ ] Chunk C: `replace_day_plan` が検証成功後にのみ cancel するか / slot.fire が汎用 unknown sweep に掴まれないか / recovery が LLM を呼ばないか
- [ ] 全体スイート (基準 = 直近 HEAD の passed 数、avatar 除外) + `ruff check` (変更ファイル)

---

## 引き受ける歪み

- **crash 回復時の予算は保守側 (返金しない)**: 精算 tx が転んで recovery が拾うと、予約額 (= コマの実効予算 = 使える上限) がそのまま消費として残る。実測が上限未満でも返金しない。予算超過を防ぐ安全側で、crash は稀なので受け入れる (A5 の設計思想と一致)。
- **予約 = 実効予算の先取り消費**: Beat 直列化でコマは同時発火しないため「hold」は主に crash 一貫性のため。予約 tx commit 後に process が死んでも予算は減った状態で残り、再起動後の残高が「作業は行われた」を反映する (= A5 バグの逆)。
