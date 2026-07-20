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

---

## Codex (Sol) レビュー修正 (2026-07-20、コミット eeb4ff2 に対して)

初回コミット後の Codex レビューで 5 件の妥当な指摘。全て head 実コードで裏取りして修正・回帰追加した。特に **Finding 2 は私 (Fable) の検収漏れの本丸** — ハンドラ境界を跨いで可変 index を信頼していた。

| # | 指摘 | 修正 |
|---|---|---|
| **1** (P1) | `replace_day_plan` が cancel 後の DB 保存/再予約失敗で旧予約を孤児化 (検証前 ValueError しか塞げていなかった) | 旧 plan を控え、保存/再予約が転けたら **cancel 済み新予約を落として旧 plan/旧予約を復元** して再送出。回帰 = 保存失敗・再 push 失敗の各で旧 plan/旧予約不変 |
| **2** (P1) | ハンドラ中の `replace_remaining_slots` (post_session が `_handle_worker_slot` 内で同期発火) で配列が前詰めされ、精算が **発火時 index で別コマを done**・冪等キーも旧 index で別コマを誤 dedup | コマに**不変 `id`** を導入 (`_validate_and_normalize_slots` で採番、既存保持)。冪等キー・payload・精算・回復を **id ベース**に (`_find_slot_index_by_id`)。旧 plan のコマは `_fire_slot` で backfill。回帰 = 前詰め後も id で正しいコマが done・別 id コマは二重発火扱いされず発火可 |
| **3** (P1) | ハンドラ例外 + episode close 失敗が重なると episode open のまま unknown へ進み、回復 (running のみ) が拾えず永久 open | 回復 tick/起動時に `_close_orphaned_unknown_slot_episodes` を追加 — unknown な slot.fire の孤児 open episode を閉じる (slot=fired・台帳=unknown は保つ、冪等)。回帰 = 孤児 episode が閉じられ二度目は no-op |
| **4** (P2) | `touch_desire` が予約 tx より前 → 予約失敗の再試行ごとに touch_count 増 | touch_desire を**予約 tx 成立後**へ移動 (予約成立 = 「取り組みに向かった」の確定点)。回帰 = 予約失敗で touch 0 回・成立で 1 回 |
| **5** (P2) | 負の `used_rounds` が返金として受理され過剰返金・台帳に負値保存 | 精算で **非負 int のみ有効** (旧 consume 系と同じ)。負/非 int は delta=0 で予約額を残し、result は None + WARN。回帰 = -3 で used 不変・result None |

回帰追加計 9 件 (test_day_plan.py)、本体スイート **2728 passed 全緑**、ruff clean。**教訓**: 検収の第二パスで「ハンドラ/await 境界を跨いで可変な配列 index を信頼していないか」を明示的に見る。実行単位の identity は index (位置) でなく不変 id で持つ。

### 第二レビュー (2026-07-20、上記修正への再指摘)

初回修正 (aeb76ae) に Codex が更に 2 件。**どちらも「初回修正の詰めの甘さ」** — 私の直しが一過性障害/単一スレッド前提に閉じていた。

| # | 指摘 | 修正 |
|---|---|---|
| **1-cont** (P1) | A1 の復元が **同じ `_upsert_plan_slots` の成功に依存** — 継続的な DB 障害では復元も転けて旧予約が消えたまま (元 A1 が再発)。回帰も「一過性障害」しか見ていなかった | **保存先行**へ再設計: 検証 → **保存** → (成功後に) cancel → 再 push。保存で失敗すれば旧予約は **未 cancel = 無傷** (継続障害でも孤児ゼロ)。再 push 失敗は DB が既に新 plan なので watchdog 回復に委ね例外にしない。`cancel_scheduled_slots` に `count=` を追加 (保存後 cancel が旧 index 範囲も網羅)。回帰 = 継続保存失敗で旧 plan/旧予約不変・継続 push 失敗で新 plan へ収束 (find_lost で回復可) |
| **2-cont** (P1) | 不変 id を claim/精算には通したが **予約 tx は発火時 index を無条件 fired** — claim 後・予約前の組み替えで別コマを fired にし元コマのハンドラが走り台帳だけ completed | 予約 tx に slot_id を渡し **同 session 内で id から現在位置を引く**。対象が消えて/発火不能なら `_SlotVanished` を投げ、**mark_running より前に中断** (副作用ゼロ) → 台帳 failed・ハンドラ不実行。回帰 = claim 後・予約前の組み替えでハンドラ 0 回・failed・別コマ pending |

回帰 +3 件、本体スイート **2729 passed 全緑**、ruff clean。**深めた教訓**: ①「復元」戦略が失敗と同じ操作に依存していないか (障害は継続しうる — 一過性前提の回帰では捕まらない)。②不変 id 化は claim/精算だけでなく **状態を書く全区間 (予約含む) を貫く** — 一箇所でも位置ベースが残ると窓が開く。

### 第三レビュー (2026-07-20、c321556 への再々指摘。修正セッションは別 — Opus 暴走後に Fable が引き継ぎ)

Codex が更に 2 件。どちらも再現実験付きで、コードの構造と一致することを裏取りした上で受け入れた。

| # | 指摘 | 修正 |
|---|---|---|
| **1-cont²** (P1) | 保存先行後の cancel が**最初から**失敗すると旧時刻の予約が残留。予約 key が index ベースで新 plan と**同じ文字列**のため、(a) watchdog の `find_lost_slot_reservations` は「key の有無」しか見ず途絶を見逃し (`== []`)、(b) 旧 13:00/15:00 のイベントが新 plan の 18:00/20:00 コマを前倒しで誤発火させる (except 節の「watchdog が回復する」が成立していなかった) | **EventScheduler 予約 key を不変 slot id ベースへ移行** (`_slot_key`)。発火 callback は `_fire_slot_by_id` が現 plan から id で index を解決 — 残留予約は「その id のコマはもう無い」で**無害に空振り**し、watchdog は新コマの key 不在を正しく検出→再 push で自己回復。cancel は best-effort の掃除に格下げ (`extra_ids=` で旧 plan の id を渡す)。episode の origin_ref は**回復互換のため index 形式のまま分離凍結** (`_slot_origin_ref` は `_slot_key` を共有しない)。legacy plan (id 無し) は push 時に採番 (`_ensure_slot_ids`)、plan 内の id 重複は検証で振り直し。回帰 = cancel 全滅でも find_lost が新コマを検出・残留発火が空振り・reschedule で収束 |
| **2-cont²** (P1) | `claim_execution` の prepared 再利用は**同じ execution_id を並走発火の両方に runnable として返す**。先発が予約 commit 後、後発は `_SlotVanished` 経路で共有台帳を**無条件 mark_failed** (running→failed は合法) → 先発の精算が failed→applied の IllegalTransition で爆発 (slot=fired・予算予約済み・episode=open・台帳=failed)。両者が予約 tx へ同時進入した場合は二重実行・予算二重予約の窓もあった | prepared→running を**条件付き一括 UPDATE の早い者勝ち**に (`try_mark_running`、session= で予約 tx の 1 commit に同梱)。負けた側は `_ClaimLost` で予約 tx を全ロールバックし**台帳に一切書かず離脱**。`_SlotVanished` 離脱時の failed 落としも `abandon_prepared` (**prepared のときだけ** failed) へ変更 — 勝者所有の running 台帳を壊さない。回帰 = Sol 再現の interleaving (後発離脱中も台帳 running 維持→先発 completed 完走)・席取り敗者の副作用ゼロ・台帳プリミティブ 5 件 |

回帰 +8 件 (test_day_plan 3 + test_execution_ledger 5)、テスト側の旧 index key 前提も id ベースへ更新 (test_day_plan / test_judgment_points 計 6 箇所)。本体スイート **2737 passed 全緑**、ruff clean。**深めた教訓**: ①「watchdog が回復する」と書くなら**watchdog がその異常を観測できるか**まで検証する — 回復系の前提 (key の一意性) が破れていると、回復を当てにした except 節は嘘になる。②冪等 dedup (同じ行を返す) と実行権 (走ってよいのは一人) は**別の概念** — dedup の口が実行権も配っていないか、並走で同じ id を掴んだ両者のその後を追う。

### 第四レビュー (2026-07-20、第三陣修正 f2284a9 への再指摘)

第三陣の id 化そのものへの詰め 2 件。どちらも「id を導入したのに、照準・世代の一貫性が経路の途中で切れている」型。

| # | 指摘 | 修正 |
|---|---|---|
| **P1** | `_fire_slot_by_id` が id→index を解決した**直後**に、`_fire_slot` が plan を**再読込**して index で発火する — 2 回の読込の間に時間割が組み替わると別 id のコマを実行する (再現: 13:00 の id 発火→間で 18:00 コマへ置換→18:00 コマが 13:00 に done) | `_fire_slot` に `slot_id` (照準) を渡し、**発火に使う配列を読んだ本体自身が id を解決する** — 変換と使用の間に別の読込を挟まない。`_update_slot` にも `expected_id` 照合を追加し (不一致なら id で引き直し、消えていれば書かない)、繰り下げ/skip/予算切り詰め/presence 記録/legacy fired・done の全書き込み点に配線 — 「index を掴んでから書くまで」の同族の窓を書き込み側で一括閉塞 |
| **P2** | 検証は有効な既存 id を保持するため、呼び出し元が**旧コマを id ごと写して時刻だけ変えた**入力だと新旧の予約 key が同一 — cancel 障害時の衝突 (第三陣 P1) が復活する。現 day_open 経路は sanitize で id を落とすため直接は踏まないが、`replace_day_plan` の契約として安全性が成立していない | **置換 = id の新世代**を契約化: `_validate_and_normalize_slots` に `fresh_ids_from` を追加し、`replace_day_plan` は全コマ (`fresh_ids=True`)、`replace_remaining_slots` は新コマ区間 (消化済み帳簿は精算・回復の逆引き対象なので id 維持) を必ず採番し直す。入力の形に依存しない構造保証 |

回帰 +3 件 (照準の index ズレ追従 / 消えた id の空振り / 置換の新世代化。cancel 失敗テストも「id ごと写した入力」の最悪形に強化)。本体スイート **2740 passed 全緑**、ruff clean。**深めた教訓**: ①不変 id を「持っている」ことと「解決した結果を使う瞬間まで一貫して照準にしている」ことは別 — **変換 (id→index) と使用の間に再読込を挟んだら、その変換は無効**。書き込み点ごとに「この index は今も同じコマか」を問う。②識別子で安全性を作ったら、**その識別子の供給源 (誰が採番し、誰が持ち込めるか) まで契約に含める** — 入力が旧識別子を持ち込める限り、一意性は仮定でしかない。

### 第五レビュー (2026-07-20、第四陣修正 1c7accc への再指摘)

| # | 指摘 | 修正 |
|---|---|---|
| **P1** | `_update_slot` は id 照合の**後**、`_write_slots` (別 Session) で**読んだ配列全体を無条件で書き戻す** — 読みと書きの間に `replace_remaining_slots` の置換 (A/B→C) が commit されると、古い書き戻しが C を消し A/B を復活させる (lost update = ペルソナの決定が静かに失われるデータ保全問題)。再現あり | slots_json への全書き込みを**世代 CAS** 化: 新設 `_mutate_slots_cas` (読み・変異・保存を同一 tx にまとめ、`UPDATE ... WHERE slots_json = 読んだ payload` の条件付き更新。世代が変わっていれば最新 plan で変異をやり直す、最大 5 回) に `_update_slot` / `_ensure_slot_ids` を載せ替え (`_write_slots` 廃止)。**同族の tx 側も一括閉塞**: 予約 tx / 精算 tx / 回復 settle の slots+予算書き込みを ORM 属性書き込み (無条件 UPDATE) から条件付き更新へ変更 — 世代不一致は `_PlanGenerationConflict` で全ロールバックし、呼び出し元 (`_fire_slot`) が最新 plan で id から引き直して再試行 (置換で対象が消えていれば `_SlotVanished` の既存安全経路へ自然に落ちる)。`replace_remaining_slots` 自身も CAS ループ化 (読んだ世代と同じときだけ置換を commit — 併走する fired 書き込みを消して claim 済みコマを pending 復活させる逆向きの窓も閉塞)。予算調整は `_apply_budget_delta_to_meta` (dict ベース) へ改め、条件付き更新に同梱 |

回帰 +3 件 (CAS ヘルパの再試行 / Sol 再現 = `_update_slot` が置換を消さない / 予約 tx 競合の安全離脱)。本体スイート **2743 passed 全緑**、ruff clean。**残す既知の割り切り**: `replace_day_plan` (起床の全置換) の upsert は無条件のまま — 全コマを新世代 id で総入れ替えする意味論のため、旧世代への並走書き込みを消しても「消えるべきものが消えた」に一致する (併走 fire の予約額は精算側の id 逆引き空振りで安全に閉じる)。**深めた教訓**: 照準 (どのコマに書くか) を直しても**書き戻しの粒度が配列全体**なら、隣のコマの決定を消せる — 「対象の同一性」と「書き込みの世代整合」は別の不変条件で、両方に守りが要る。read-modify-write を見たら常に「この間に誰かが commit したら?」を問う。

### 第六レビュー (2026-07-20、第五陣修正 023549e への再指摘)

| # | 指摘 | 修正 |
|---|---|---|
| **P1** | 予約/精算 tx は読んだ古い **meta_json** を書き戻すのに、CAS 条件は **slots_json しか**比較していない — コマ配列が無傷なら CAS が通り、読み書きの間に `update_plan_meta` (明日メモ等) が commit した更新を古い meta で消す (再現: tomorrow_memo="new" が "old" へ巻き戻り)。予約 tx は非 gated でも `row.meta_json` を無意味に書き戻していた | ①**meta_json は書くときだけ SET に含め、含めるときは読んだ meta も CAS 条件へ追加** (予約 = gated+reserved のみ / 精算 = gated+delta のみ。非 gated は meta に一切触らない)。競合は既存の `_PlanGenerationConflict` 再試行に乗り、**最新 meta から予算を再計算**する。②逆方向 (transfer): 独立系 meta 書き手の唯一の口 `update_plan_meta` (save_lives / consume 系 / 明日メモは全部ここを通る) も **meta CAS + 再試行**化 — メモの書き戻しが並走 tx の予算書き込みを消す鏡像も同時に閉塞。行 INSERT 競合は IntegrityError → 更新経路で再試行。再試行枯渇は RuntimeError で正直に表明 (silent 消失にしない) |

回帰 +3 件 (予約 tx がメモを消さない = Sol 再現 / 精算 tx 側 / update_plan_meta の逆方向)。本体スイート **2746 passed 全緑**、ruff clean。**深めた教訓**: CAS を張るときは「**比較する列**」と「**書く列**」を突き合わせる — 書くのに比較しない列は、その列の並走更新を静かに消す口として残る。守りたい単位 (行) と守れている単位 (列) のズレを確認する。
