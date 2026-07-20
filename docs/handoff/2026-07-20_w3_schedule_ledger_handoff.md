# W3 (実行台帳 Phase 3 — schedule) セッション走行メモ (2026-07-20)

**用途**: このセッション (Fable/メティス) の確定設計と委譲・検収の再開点。工程の真実は [完了計画書](../overview/audit_remediation_plan.md)、finding の真実はレビュー台帳と [自律行動監査](2026-07-14_autonomy_judgment_schedule_audit.md) (末尾 2 件が対象)。

**スコープ** (計画書 W3): A12 (schedule 設定の予約同期失敗の握り潰し・稼働中 reconciliation 不在) / A13 (dispatch 失敗後も completed 化・oneshot 再試行不能)。

**進行**: 調査 (済み) → 設計 (本書 = 確定) → 実装 (Chunk 委譲) → 検収 (メイン) → コミット。

---

## 調査で確定した現状 (行番号は 2026-07-20 時点)

### 患部の構造

- **`saiverse/schedule_manager.py`**: push 駆動。`_do_register` (:139) が `_compute_next_fire_at` → `EventScheduler.schedule(key=persona_schedule:{id})`。発火 callback `_handle_fire` (:326) は schedule_id **だけ**を closure に持ち、`_execute_schedule` (握り潰し) → `_update_schedule_after_execution` (**無条件** COMPLETED / LAST_EXECUTED_AT) → `_do_register` (次回) を順番実行。
- **受付契約の潰れ**: `_execute_schedule` (:423) は判断点経路 (`handle_scheduled_judgment` の戻り dict) を**捨て**、汎用経路 `pulse_dispatcher.dispatch_schedule_fire` (:92, 戻り値 None・例外握り潰し) → `PulseController.submit` は「実行=List[str] / queue・skip=None」だが、**実行中の例外・キャンセル・Beat 関所閉鎖はすべて空リスト `[]` に潰れる** (`_execute_unlocked` :312-353)。呼び出し側から「LLM が動いたか」を判定する術が現状ない。
- **schedule 種別の Pulse config** (`sea/pulse_controller.py:53-57`): `on_blocked="wait"` — busy なら**queue され消えない**。割り込み中断も復帰 queue 行き。「skip で消える」経路は schedule には無い (auto と違う)。
- **EventScheduler** (`saiverse/event_scheduler.py`): `schedule()` は**同一 key 上書き** (旧予約 lazy cancel、:133)。`cancel(key)` / `has_key(key)` あり。**per-key の fire_at 照会は無い**。callback 例外は「WARN + 予約 drop」(:317-328)。callback は単一 dispatch スレッドで直列実行 — **回復 tick も同じスレッド**なので、実行中の schedule Pulse と reconciliation が同時に走ることはプロセス内では構造的に無い。
- **CRUD 側** (`api/routes/people/schedule.py` / `life_settings.py:244-254`): DB commit 後に `register_schedule` を try/except log で呼ぶ (握り潰し、response は完全成功)。稼働中 reconciliation は無し (`start()` の全件 register のみ)。
- **PersonaSchedule** (`database/models.py:431`): 世代列は無い。UPDATED_AT は onupdate だが秒精度で世代токен に使えない。行を作る書き手は 3 箇所: `api/routes/people/schedule.py:103` / `api/routes/people/life_settings.py:148` (`_upsert_day_row` が更新も担う) / `builtin_data/tools/schedule_add.py:90`。
- **判断点経路の戻り語彙** (`saiverse/autonomy_wiring.py`): `submitted=False` の reason = `persona autonomy disabled` / `playbook not imported` / `duplicate:{status}` / `precondition not met` / `precondition raised` / `not a judgment playbook` / `kind not schedulable` / `conversation had no exchange...`。day_open/day_close は **W1 で judgment.* の台帳 claim 済み** — schedule 側の台帳はその外殻 (発火 claim) を受け持つ。
- **回復 tick** (`saiverse/execution_ledger_wiring.py:170`): #7 (schedule reconciliation) は未実装と明記 (:23-24)。prepared 回収は judgment.* のみ。
- **台帳 API**: `claim_execution` (failed キー退避・prepared 再利用) / `try_mark_running` (席取り CAS、session= 可) / `abandon_prepared` / `mark_applied(session=)` — W2 までで完備。**(kind, key) での読み取り口が無い** → 新設が要る。

---

## 確定設計 (私 = Fable の裁定)

> **NOTE (2026-07-21)**: 本節は着工時の設計。Codex レビュー 5 巡 (末尾の消し込み表) で細部が進化した — 冪等キーは `{schedule_id}:{occurrence_epoch}` から **`{schedule_id}:{INSTANCE_TOKEN}:{occurrence_token}`** (行一生トークン + 初回 interval は `first@g{世代}`) へ、`register_schedule` は bool から **tri-state** へ、`_registered` は世代のみから **(行トークン, 世代)** へ。最終形はコードと消し込み表が正。

### 全体方針

- **A13 = 発火 1 回を台帳実行 `schedule.dispatch` で包む**。claim → 席取り → 型付き dispatch → **精算 tx (schedule 状態前進 + mark_applied を world DB 単一 commit)**。W2 slot.fire と同じ骨格、episode・予算が無いぶん軽い。
- **A12 = DB 正典 + 世代 + reconciliation**。`SYNC_GENERATION` 列を追加し、予約 closure が世代を運ぶ。回復 tick に世代照合ループを新設し、register/unregister 失敗は 60 秒以内に自己回復する。
- 両者は同じ closure (occurrence + 世代) を共有するため**一体で実装する**。

### D1. `schedule.dispatch` の kind と冪等キー (A13)

| 項目 | 値 |
|---|---|
| kind | `schedule.dispatch` |
| idempotency_key | `{schedule_id}:{occurrence_epoch}` |
| payload | `{"schedule_id", "persona_id", "schedule_type", "occurrence_epoch", "generation", "meta_playbook"}` |

- **occurrence_epoch** = `_do_register` が計算した発火時刻 `next_fire.timestamp()` の int。closure に凍結して発火時に使う。oneshot = SCHEDULED_DATETIME 固定 / interval = LAST_EXECUTED_AT+間隔 (成功まで不変 = 再試行が同一 occurrence に収束) / periodic = その日のその時刻 (翌日は別 occurrence)。重複予約・reconciliation の再登録・watchdog 経路がどこから来ても同じ occurrence キーへ収束する。

### D2. 予約 closure の拡張 + 世代列 (A12/A13 共有)

- migration: `persona_schedule.SYNC_GENERATION INTEGER NOT NULL DEFAULT 0` (追加系 ALTER の軽量パス)。
- **書き手の契約**: 発火時刻・内容に影響する設定変更 (create / update / toggle / life PUT / schedule_add tool) は**同じ commit 内で** `SYNC_GENERATION += 1`。実行簿記 (COMPLETED / LAST_EXECUTED_AT) は世代を上げない (直後に同スレッドで再 register するため不要)。
- `_do_register` は closure に `(schedule_id, occurrence_epoch, generation, attempt=0)` を焼き込む。`_registered_ids: Set[int]` は `_registered: Dict[int, int]` (id → 登録世代) へ置換。

### D3. `_handle_fire` の台帳化 (A13 の核)

```
_handle_fire(schedule_id, occurrence_epoch, generation, attempt):
  全体を try/except で包む (callback 例外 = EventScheduler の予約 drop を自作しない)
  1. DB 再読。無い/disabled → discard (現状どおり)
  2. 世代照合: schedule.SYNC_GENERATION != generation
     → 旧世代予約。LLM を起動せず _do_register(最新 DB) して return  [A12 回帰②]
  3. claim_execution("schedule.dispatch", key, persona_id, payload)
     runnable=False → 二重発火 dedup。次 occurrence の key が blocked key と
     同一なら再登録しない (oneshot の hot loop 防止)。異なれば _do_register。return
  4. try_mark_running(exec_id) — 敗者は台帳に書かず return
  5. outcome = _execute_schedule(schedule, session)  ← 型付き (D4)
  6. settle:
     - executed / accepted / settled_skip →
       単一 tx: _update_schedule_after_execution(session) + mark_applied(exec_id,
       session=db, result={"action", "reason", "schedule_type"}) → commit →
       mark_completed (outbox 無し) → _do_register (次 occurrence)
     - failed (副作用ゼロ確定) → mark_failed + backoff 再予約 (D5)
     - unknown (LLM が動いたか不明) → mark_unknown。schedule 状態は前進させず、
       再予約もしない (occurrence は unknown claim がブロック。裁定 = list_unknown)
```

### D4. 型付き outcome (`_execute_schedule` の再定義)

観測点を 2 つ増やす:

- **`ExecutionRequest` に結果フィールドを追加** (`sea/pulse_controller.py`): `dispatch_action: Optional[str]` (submit が "execute"/"queued"/"skipped" を記入) と `runtime_outcome: Optional[str]` (`_execute_unlocked` が "completed"/"gate_closed"/"cancelled"/"error" を記入)。**submit の戻り値契約・既存呼び出しは不変** — request オブジェクト経由の観測のみ追加。
- **`dispatch_schedule_fire` は握り潰しをやめ** request を作って submit し、上記フィールドから型付き outcome を返す (呼び出し元は ScheduleManager のみ — 裏取り済み)。

分類表 (schedule は on_blocked=wait なので「queue され消えない」が前提):

| 観測 | class | 台帳 | schedule 状態 |
|---|---|---|---|
| 実行完走 (runtime_outcome=completed) | executed | applied | 前進 |
| queued / cancelled(復帰 queue 済) | accepted | applied (action 記録) | 前進 |
| 判断点 submitted=True | executed | applied | 前進 |
| 判断点 submitted=False の**ゲート系** reason (duplicate / precondition not met / autonomy disabled / playbook not imported / not schedulable / no exchange) | settled_skip | applied (reason 記録) | 前進 |
| gate_closed (Beat 関所、副作用ゼロ) | failed | failed | 維持 → 再試行 |
| submit 前の例外 (persona/building 不在、controller 不在) / 判断点 "precondition raised" | failed | failed | 維持 → 再試行 |
| runtime 例外 (error に潰れた) | unknown | unknown | 維持・自動再実行なし |

- ゲート系を settled_skip (前進) にする理由: 「意図して実行しない」と裁定済みの occurrence を failed 再試行で叩き続けるのは無意味で、現行の前進挙動とも一致する。判断点の実体回復は W1 の judgment.* 台帳 + watchdog が持つ。
- unknown の oneshot は claim ブロックで再発火しない (**LLM 自動再実行禁止**、intent §2.5)。裁定は list_unknown 観測面。

### D5. 失敗の backoff 再試行 (A13)

- `SCHEDULE_DISPATCH_RETRY_BACKOFF_SECONDS = 120` / `SCHEDULE_DISPATCH_MAX_ATTEMPTS = 3`。
- failed 時: attempt+1 ≤ MAX なら `scheduler.schedule(clock.now()+backoff, 同一 occurrence closure(attempt+1), 同一 key)`。claim が failed キーを退避するので再実行は安全 (副作用ゼロ状態のみ)。
- 尽きたら: periodic は occurrence を諦め次回 (翌日) を register。oneshot / interval は register せず **reconciliation の周期 (60s) に委ねる** — претря は続くが cadence が落ちる (引き受ける歪み①)。
- 仮想クロック整合のため再試行の現在時刻は `saiverse.clock.now()`。

### D6. reconciliation (A12 の核、回復 tick #7)

`ScheduleManager._reconcile_schedules()` を新設し、`execution_ledger_wiring._recovery_tick` から呼ぶ (intent §2.4 #7 の「回復 tick と同居」):

1. **復元**: DB の enabled 全件について、`has_key` が無い**または**登録世代 (`_registered[id]`) ≠ DB 世代 → `register_schedule(id)`。ただし **occurrence ブロック確認**: 計算した次回 occurrence の `(kind, key)` を台帳で読み (新設 `find_execution`)、status ∈ {running, applied, completed, unknown} なら登録しない — 実行中 (>60s の長 Pulse) への二重登録と、unknown 裁定待ち oneshot の自動再実行をここで塞ぐ。
2. **除去**: `_registered` にあるが DB 行が無い/disabled → `cancel` + map から除去 (delete/disable の unregister 失敗の回復)。
3. **手動モードは特別扱いしない**: 予約の復元は「宣言的正典の同期」で発火そのものではない。発火時ゲートの一貫化は W9 (柱7) の所掌 — ここで部分実装しない (まはーに明示报告)。

台帳側の新設: `ExecutionLedger.find_execution(kind, idempotency_key) -> Optional[dict]` (読み取り専用)。

### D7. API 応答の同期状態明示 (A12 回帰④)

- `create / toggle / update / delete` (schedule.py) と life PUT (life_settings.py) の register/unregister を try/except で受け、response に `"scheduler_synced": bool` を追加 (HTTP は 200 のまま — DB が正典で reconciliation が 60 秒以内に回復するため 5xx にしない。監査の「区別しろ」は満たす)。`LifeSettingsResponse` にも同フィールド追加。フロントの表示は必須にしない (任意の後続)。

### 実装 Chunk (順次委譲、並列なし)

- **Chunk A (基盤小物)**: migration `SYNC_GENERATION` + 書き手 3 箇所の世代 bump + D7 API 応答 + `ExecutionRequest` 結果フィールド + `dispatch_schedule_fire` 型付き戻り + `ExecutionLedger.find_execution`。回帰: 世代 bump が同一 commit / find_execution / dispatch_schedule_fire の各 outcome / API scheduler_synced。
- **Chunk B (`_handle_fire` 台帳化)**: D1 + D3 + D4 + D5。回帰 (A13): dispatch 前例外→oneshot COMPLETED=False + failed + backoff 再予約→成功で一度だけ COMPLETED / queued=accepted 前進 / runtime 例外→unknown + 前進なし + 自動再実行なし / interval 失敗で LAST_EXECUTED_AT 不変 / periodic 判断点例外→同日 backoff / duplicate→settled_skip 前進 / 同一 occurrence 二重発火→dispatch 1 回 / 旧世代 callback→LLM 0 回。
- **Chunk C (reconciliation + 総仕上げ)**: D6 + wiring 結線。回帰 (A12): register 失敗→再起動なしに tick 1 周で登録 / unregister 失敗→callback 無行為 + tick で除去 / 実行中 occurrence への二重登録なし / unknown oneshot を再登録しない。`gen_reference_docs` (database-schema / api-endpoints) 再生成。
- **Docs (メイン)**: execution_ledger.md ステータス (Phase 3) / 計画書 W3 / レビュー台帳 A12/A13 / in_flight。

### 検収チェックリスト (メイン)

- [ ] 世代 bump が**全書き手** (schedule.py×3 操作 / life_settings / schedule_add) で同一 commit に乗っているか
- [ ] `_handle_fire` の settle tx が単一 commit か (状態前進 + applied が全 or 無)
- [ ] closure→使用の間に別読込で照準がズレる箇所がないか (W2 第四陣の教訓: 変換と使用の一貫性)
- [ ] read-modify-write で並走 commit を消す口がないか (W2 第五〜七陣の教訓: schedule 行は単票なので CAS 不要のはずだが、settle と CRUD の並走を確認)
- [ ] reconciliation が LLM を直接起動しないか (登録のみか)
- [ ] 全体スイート (基準 = 直近 HEAD 2749 passed、avatar 除外) + `ruff check` (変更ファイル)

---

## 引き受ける歪み

1. **恒久故障の oneshot/interval は reconciliation 周期で再試行し続ける**: backoff 上限後も 60 秒ごとに claim→failed を繰り返す (failed キー退避で台帳行が増える)。dispatch 失敗は異常系で、ログ・台帳から観測可能。止めたければ disable すればよい (DB 正典)。
2. **queued の受付は in-memory queue が根拠**: accepted (queued) で schedule 状態を前進させるため、queue 保持中のプロセス死でその occurrence は実行されず completed になる。durable queue 化は台帳 kind への chat command 載せ替え (intent §8) と同じ将来課題で、今回は「受付証跡 = queue 投入」を採る。従来 (無条件 completed) より狭い窓。
3. **旧世代予約の発火は「予約 drop + 再登録」で処理**: 旧時刻に何も起きない (無害な空振り)。旧時刻に新設定を実行する現行動作は「誤実行」(監査の指摘) なので消える。

## 既知の隣接バグ (W3 スコープ外、issue 化済み)

- `PulseController._queue_for_resumption` (:287) は復帰 request 生成時に `args` / `pre_spells` を**引き継がない** — 割り込まれた schedule Pulse の復帰で Playbook 引数が落ちる。W3 では触らず `docs/issues/pulse_resumption_drops_args_prespells.md` に残した。

---

## Codex (Sol) レビュー消し込み (2026-07-20〜21、計 10 巡 27 件 — 受諾 24 / 裁定却下 3)

全件 head 実コードで裏取りしてから修正・回帰追加。第四陣までは Fable サブエージェント委譲、レート制限 (07-21) 以降はメイン直接実装 ([[feedback_delegate_impl_to_subagents]] 改訂の契機)。**第七陣から新方式** (まはー承認 2026-07-21): adversarial-review に「全件列挙 + 同族横展開」を指示 — 一巡で 5 件 (旧方式の 3〜4 巡分) が出て収束が加速した。

| 巡 | # | 指摘 | 修正 |
|---|---|---|---|
| 1 | P1 | **初回 interval の occurrence key が不安定** — LAST_EXECUTED_AT=None の interval は次回時刻を「現在時刻」から計算するため、初回が unknown で終わっても reconciliation が**別 epoch の別キー**で再登録し、unknown の自動再実行禁止 (intent §2.5) をすり抜けて LLM 二重起動 | occurrence の識別子を epoch int から**トークン文字列**へ (`_occurrence_token`)。初回 interval は安定 sentinel に固定 — unknown は同キーをブロックし続け、oneshot と同じ「裁定待ち」に。初回成功で LAST が入れば epoch ベースへ |
| 1 | P2 | **非例外の登録失敗が scheduler_synced=True** — 有効なのに設定不備 (TIME_OF_DAY 欠落等) で `register_schedule` が False を返しても API は同期成功と応答 | `register_schedule` を tri-state 化 (`registered` / `no_reservation_needed` / `not_registrable`)。ルートは前二者を synced=True、後者と例外を False に |
| 2 | P1 | **day_close 再試行で境界副作用が重複** — `_handle_life_end` (keep-alive cancel + TTL 同期 + 「（活動終了）」通知) は非冪等で、判断 claim が prepared のまま転ぶと backoff 再試行が再適用 | day_open と対称の冪等ガード: lives[0] の永続マーカー `ended` (`mark_life_ended`、mutate_plan_meta の CAS 内で書く) で (persona, 営業日) につき一度。「確認 → 適用 → マーク」順 |
| 2 | P2 | **reconciliation が「次回発火不能になった残留予約」を未回収** — enabled のまま next_fire 計算不能に更新されると `continue` だけで旧予約が残る | next_fire=None の enabled schedule は予約 cancel + `_registered` から除去してから continue |
| 3 | P1 | **SCHEDULE_ID 再利用で台帳キー衝突** — AUTOINCREMENT なしの INTEGER PK は削除済み最大 ID を再利用し、旧行の completed 台帳が新 schedule の claim を永久ブロック (「消して作り直す」の普通の操作で新 schedule が静かに死ぬ) | 行の一生に固有な `INSTANCE_TOKEN` 列を追加 (additive migration + randomblob backfill、作成時に uuid 採番・更新では不変)。キーを `{id}:{instance}:{occurrence}` に。発火時の行同一性照合 (closure トークン ≠ DB 行トークン → 実行せず再登録) も追加 — 世代照合だけでは「旧行 gen=1 削除 → 新行 gen=1」を素通りするため |
| 4 | P2 | **初回 interval の unknown 封印が設定変更でも解けない** — sentinel が世代を含まず、gen が進んでも同キーのままブロック継続 (裁定 UI 未配置で回復手段が「削除して作り直す」だけ) | sentinel を `first@g{N}` に — **ユーザーの設定変更 = 新しい論理 occurrence** (「再実行は人間が新しい実行として起動する」intent §5 の schedule 版)。同一世代内のブロックは維持 |
| 4 | P2 | **ライフ終了の部分失敗が ended 封印される** — 下請け 3 段は各自例外を握るため、通知の追記が失敗しても成功扱いでマークされ永久喪失 | 3 段が成否 bool を返し、`_handle_life_end` は「冪等段 (cancel/TTL) が先・非冪等な通知が最後、途中失敗は通知前に打ち切り」の順序契約で全段成功のみ True。True のときだけマーク |
| 5 | P1 | **reconciliation の同期済み判定が行同一性を見ない** — (予約あり, 世代一致) だけの照合は、削除→ID 再利用→新規作成で新旧 gen が偶然一致すると旧予約を「同期済み」と誤認し新行を登録しない | `_registered` を `(INSTANCE_TOKEN, 世代)` の組に — 行と世代の両方が一致して初めて同期済み |
| 5 | P2 | **境界失敗が発火結果へ伝播しない** — 第四陣修正の「マークせず戻れば再試行が回復」は嘘だった: 判断はそのまま走って成功し、schedule と判断台帳が completed になり再試行は来ない | `_apply_life_end_at_day_close` を bool 化し、False なら**判断を走らせずに** `submitted=False` (reason=`life-end boundary failed`) + 判断行を failed に。schedule 側 backoff が再試行し、境界回復後に claim がキー退避で新 prepared を取る |

| 6 | P1 | **periodic の prepared 回収漏れ** — claim → 席取りの間の crash で prepared だけ残ると (予約は in-memory で消滅)、reconciliation は「現在時刻から計算した次回 = 翌日分」しか照合せず、**当日分の occurrence が永久に取りこぼされる** (prepared 回収規則も judgment.* 限定だった) | 回復 tick に `_collect_prepared_schedule_dispatch` を新設 — payload に凍結された (行, occurrence, 世代) で `refire_occurrence` が同一 occurrence の予約を積み直す (claim の prepared 再利用 + 席取りで二重実行なし)。行消滅/disabled/世代・行トークン不一致は failed 終端。手動モード persona はスキップ (judgment 回収と同じ規律) |
| 6 | P2 | **`mark_life_ended` 失敗の無条件 True** — マーク失敗 + 後段の判断失敗の重なりで境界が再適用される / 判断成功なら冪等ガード欠落のまま | **部分受諾 (裁定付き)**: False 伝播は不採用 — 判断を打ち切ると schedule 再試行のたびに境界再適用 = 重複を**保証**してしまう (記憶の無垢に反する)。判断 claim (persona:営業日) は境界より前にあり、判断成功後の再突入は claim dedup で境界に到達しない — マーカー欠落の実害は「マーク失敗 + 判断も失敗」の重なりと force 発火だけ。対応 = 即時リトライ 1 回 + 恒久失敗は ERROR で表明。**恒久解 = 境界通知の outbox 配送化 (マーカーと world-DB 単一 commit + 冪等配送) を W5 配送系の所掌として明記** |

| 7 | high | **精算に行・世代条件が無い** — LLM 実行中のユーザー更新を旧発火の精算が上書き (新世代の oneshot に COMPLETED=True → 直後の再登録が新予約を cancel) | `_update_schedule_after_execution` を (行トークン, 世代) 条件付き UPDATE 化。0 件 = 実行中に設定が置き換わった → 状態前進せず、台帳 applied に `superseded_during_run` を記録し、最新設定で再登録 |
| 7 | high | **通常 occurrence キーに世代が無い** — 世代を埋めたのは初回 interval の sentinel だけで、unknown な oneshot は「日時以外の変更」では永久封印のまま (「設定変更 = 新しい論理 occurrence」がキー構造に実装されていない) | 冪等キーを `{id}:{instance}:g{世代}:{occurrence}` に統一 — 世代を全 schedule 種別の独立成分へ昇格。initial-interval sentinel は素の `first` に戻す |
| 7 | high | **世代 bump が read-modify-write** — 並行する二更新が同じ世代番号を得て、reconciliation が「予約=DB 世代」の偽同期判定 | 書き手 3 箇所を SQL 式のサーバー側インクリメント (`func.coalesce(gen,0)+1`) へ — DB が直列化して必ず別番号 |
| 7 | medium | **applied 残留** — mark_applied commit と mark_completed が別 tx で、間の crash で outbox 無し applied が非終端のまま観測面に出ない | `ExecutionLedger.sweep_applied` 新設 (outbox 全 delivered / 無しの applied → completed、dead 残りは据え置き) — 回復 tick + 起動時に結線。**汎用掃除なので全 kind が恩恵を受ける** |
| 7 | medium | **完了後例外が outcome を error 上書き** — 実行完走後の後処理例外で成功した発火が unknown 封印される | dispatcher の except で `runtime_outcome=="completed"` 済みなら completed を保持し、後処理の失敗は error に併記 |

| 8 | high | **periodic の failed + crash 窓** — mark_failed (durable) と backoff 予約 (揮発) の間で死ぬと failed 行だけ残り、reconciliation は翌回を登録するため当日分が永久欠落 (第六陣 prepared 回収の同族) | `_collect_failed_periodic_schedule_dispatch` 新設 (`list_failed` 追加) — 正準キーの failed periodic を fence 照合 + 予約不在 + occurrence 24h 以内の条件で refire。oneshot/interval は reconciliation の同一 occurrence 再登録で自然回復するため対象外 |
| 8 | high | **day_open 境界の握り潰し** — day_close に入れた失敗伝播が day_open に無く、ライフ確定/開始節目 (TTL override・活動開始通知) の失敗が judgment 成功で封印される | day_close の完全な鏡像化: `started` マーカー (`mark_life_started`、CAS 内書き込み) + `_handle_life_start`/`_sync_cache_ttl_for_life_start` の bool 化 (冪等段先行・通知最後) + 境界失敗は判断前に `submitted=False` (reason=`life-start boundary failed`)。従来の「初確定のときだけ」ガードは「確定済みだが節目失敗」を永久スキップする欠陥だった |
| 8 | medium | **periodic が精算フェンスを素通り** — 前進すべき状態が無いため常に True を返し、実行中の行置換でも superseded_during_run=false と誤記録 | periodic も (行, 世代) の存在照合を行い、不一致は superseded として記録 (書き込みは行わない) |
| 8 | high | **旧世代の commit 後 register が予約を上書き** | **受諾しない (裁定)**: 発火時の世代・行照合が旧予約を無害に空振りさせ、reconciliation が ≤60 秒で正典へ収束させる — A12 の「DB 正典 + eventually consistent な予約」の設計そのもの。per-id ロック直列化は複雑さに見合わない |
| 8 | high | **queued 受付 = 完了が queue 破棄で消える** | **裁定済み (引き受ける歪み②)**: durable queue 化 / 実行完了時精算は将来課題 (intent §8 の chat command 台帳化と同族)。queue 溢れ (QUEUE_LIMIT) の消失もこの歪みに含まれることを明記 |
| 8 | medium | **prepared 年齢判定の仮想時計混在** | **却下 (前提誤り)**: 台帳の `_now_epoch` 自体が「仮想クロック尊重のため必ず clock.now() を通す」設計 (docstring 明記) で、created_at と収集側は**同じ時計** — 混在という前提が head と不一致 |

| 9 | high | **failed periodic 回収が再起動後に機能しない** — 再起動時は `start()` が翌回予約を先に登録するため、`has_key` 除外が本命の crash-restart シナリオを永久スキップ (第八陣の私の修正が主目的を外していた。テストも誤動作を「意図」として固定) | 予約有無での判定を撤去。claim payload に `attempt` を永続化し、「正準キー + 行齢 > 猶予 300 秒 (揮発 backoff が生きていれば必ず発火済み) + attempt < 上限」で回収 — refire は翌回予約を同一 key で上書きし、精算後の `_do_register` が翌回を復元。再起動経路の回帰を追加 |
| 9 | high | **TTL 解除予約の cancel 失敗が started 封印される** — cancel 失敗→「既存 override あり」で True→通知+マーク確定→残った旧予約がライフ中に override を解除しても再試行が来ない (「部分失敗の成功封印」が同じ関数内に残っていた) | cancel 例外は即 False (通知・マークの前に打ち切り) |
| 9 | medium | **periodic フェンスの SELECT が commit と原子でない** — count 後・commit 前の世代 bump で superseded を誤記録 | no-op 条件付き UPDATE (書き込みロック) に変更 — 照合と精算 commit が直列化される |
| 9 | medium | **list_failed の無制限ロード** — failed 履歴の肥大が単一 dispatch スレッドの回復 tick を劣化 | `newer_than_seconds` で CREATED_AT 下限を DB 側に。保持期間 prune は intent §11-3 の未実装項目のまま (別件) |

| 10 | high | **回収 refire が attempt を 0 リセット** — crash 窓の繰り返しで backoff 上限が実質無制限。欠落・不正 attempt も回収対象になる | `refire_occurrence` に attempt を渡す (failed 回収 = 失敗した試行 + 1 / prepared 回収 = そのまま)。attempt 欠落・不正・上限到達は自動回収しない (厳格判定) |
| 10 | high | **猶予の起点が claim 時刻 (CREATED_AT)** — dispatch が 240 秒超かかって失敗すると「失敗直後なのに猶予超過」となり、生存中の backoff 予約を奪う | 猶予を UPDATED_AT (mark_failed が刻む失敗時刻) 起点に変更 |
| 10 | medium | **list_failed の CREATED_AT 絞りは索引に乗らない** — (STATUS, UPDATED_AT) 索引しか無く、履歴総量に比例する走査が残る | 絞り・並び順を UPDATED_AT へ (失敗の鮮度としても正しい)。保持期間 prune は intent §11-3 のまま |

**打ち切り裁定 (2026-07-21、メティス)**: 第十巡で消し込みループを終了する。根拠 — ①第八巡以降の指摘は全て「直前の巡で私が書いた回収コード」に閉じており、対象が単調に縮小 (差分全体 → 修正差分 → 回収器 50 行)。②残る指摘類型は記録済み裁定 (queued 揮発性・outbox W5 送り・register の eventually consistent) の再指摘域。③修正は全て回帰固定済み・本体スイート全緑。次の検証段階は実機 (まはー) — レビューの追加巡より実挙動の観測が情報量で勝る段階に達した。

**教訓 (W2 の教訓の再演 + 新規)**: ①「再試行 / watchdog / reconciliation が回復する」と書くなら、**その回復が実際に発火する経路まで検証する** — 回復を当てにした分岐は、回復側がその異常を観測できなければ嘘になる (W2 第三陣と同型を第五陣 P2 でまた踏んだ。except 節・early return に「〜が回復する」と書いた瞬間が検証ポイント)。②識別子設計は「同一性の軸」を列挙してから — occurrence (いつの分か) / 行 (どの設定行か) / 世代 (設定の何版か) は独立の軸で、一つでも欠けるとその軸の取り違えが冪等キーの穴になる。③冪等ガードのマーカーは「適用の成功」を封印するのであって「適用の試行」を封印してはならない — 部分失敗を成功として封印すると、重複防止が喪失の永続化に化ける。
