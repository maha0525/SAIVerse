# Intent: 実行台帳 (Execution Ledger) — 不可逆な実行と記録の分裂を防ぐ共通基盤

**ステータス**: 実装済み・実機検証待ち (v0.3, 2026-07-16 起草) — **段階移行計画 (§7) の Phase 0〜5 が全段実装済み**。残る関連工事は台帳外の wave (W6 head fail-closed 以降、計画書参照)。**Phase 5 (配送系と移動、計画書 W5) = 実装済み・実機検証待ち (2026-07-21)**: S5 = perception flush の成功条件を「message id 取得」に (None の静かな失敗で pending を消さない) / M8 = Building→個人記憶転記の cursor を「連続消費した最大 seq」に後行確定 + `building_msg_ref` provenance で append 済み limbo (停止規律により高々 1 件) を冪等修復 + auto_ingest 経路の ingested_by 非永続化バグ (contextvar 経由 manager 解決) を根治 / ライフ境界通知 (W3 委譲) = kind `life.boundary_{start,end}` — claim → 冪等段 → 「マーカー + applied + 通知 outbox」を `mutate_plan_meta` の `in_session_extra` で単一 commit / B1 = `move.entity` — 位置遷移 + leave/enter イベント (session 内核) + applied + 後処理 outbox (`move.post_dynamic_state` / `move.post_addon_hooks` / `move.post_game_lifecycle`) を単一 commit、commit 後は False を返さない。回帰計 20 件、走行メモ = [`2026-07-21_w5_delivery_ledger_handoff.md`](../handoff/2026-07-21_w5_delivery_ledger_handoff.md)。残 = まはー実機検証。**Phase 4 (Metabolism 残片、計画書 W4) = 実装済み・実機検証待ち (2026-07-21)**: M2 残片 5 欠陥 (親子別 commit の二重生成 / 窓内圧縮 / digest 再圧縮 / source 重複無防備 / dismantle 複合更新) を [体験の構造](experience_structure.md) 工程(2) の episode 整列経路で消化 — Chronicle 生成を整列計画 (alignment) + チャンク実行 (executor、チャンク単一 tx + tx 内重複再検査) + 帯あふれ束ね (bands、親子単一 tx) に世代交代し、旧バッチ経路 (ArasujiGenerator / maybe_consolidate / gap-fill / dismantle) は削除。API 生成ジョブに M1 claim 結線 (旧: claim を通らない別コネクション入口)。Track Chronicle 生成廃止 (§11-10)。Codex レビュー 5 巡 20 件消し込み (受諾 20 / 却下 0)。回帰 = alignment/executor/bands/metabolism 計 66 件追加、本体スイート全緑。走行メモ = [`2026-07-21_w4_metabolism_ledger_handoff.md`](../handoff/2026-07-21_w4_metabolism_ledger_handoff.md)。残 = まはー実機検証。**Phase 3 (schedule、計画書 W3) = 実装済み・実機検証待ち (2026-07-21)**: 発火を `schedule.dispatch` 台帳実行で包み (claim → try_mark_running → 型付き outcome → 「schedule 状態前進 + mark_applied」の world-DB 単一 commit)、oneshot/interval/periodic の完了化は成功証跡後のみ (A13)。failed (副作用ゼロ確定) は backoff 再試行 (120 秒 ×3、periodic は尽きたら翌 occurrence)、unknown は自動再実行禁止で claim がブロック。冪等キー = `{schedule_id}:{INSTANCE_TOKEN}:g{世代}:{occurrence}` — 同一性の三軸を独立成分で持つ: INSTANCE_TOKEN (どの設定**行**か — SQLite の INTEGER PK 再利用対策) + g{SYNC_GENERATION} (設定の**何版**か — 「設定変更 = 新しい論理 occurrence」を全種別で実装、unknown 封印は同一世代内のみ) + occurrence (**いつの分**か — oneshot=予定時刻 / interval=前回+間隔、初回は `first` / periodic=当日時刻)。A12 は `SYNC_GENERATION` 世代列 + 回復 tick #7 の reconciliation (`ScheduleManager._reconcile_schedules` — DB 正典と予約・`_registered`(行,世代) の照合、登録・除去のみで LLM を起動しないため手動モードでも止めない。発火時ゲートは柱7/W9) + API の `scheduler_synced` 明示で閉塞。day_close 境界 (`_apply_life_end_at_day_close`) は lives[0].ended マーカーで冪等化し、部分失敗は判断前に `submitted=False` で打ち切って backoff に乗せる。Codex レビュー 10 巡 27 件消し込み (受諾 24 / 裁定却下 3) — 後半の主要修正: periodic の prepared/failed 回収 (crash で消えた当日 occurrence の再発火経路、attempt 永続化で上限保持) / day_open・day_close 両境界の冪等マーカー (started/ended) + 失敗の判断前伝播 / 精算の (行, 世代) フェンス (periodic は no-op 条件付き UPDATE) / applied 残留の sweep / 世代 bump のサーバー側インクリメント。恒久解 = 境界通知の outbox 化は W5 所掌 (走行メモ = [`2026-07-20_w3_schedule_ledger_handoff.md`](../handoff/2026-07-20_w3_schedule_ledger_handoff.md))。回帰 = schedule 系 +約70件、本体スイート 2816 passed 前後 (最終数は計画書参照)。残 = まはー実機検証。**Phase 1 (判断点) = 実装済み・実機検証待ち (2026-07-19、W1)**: claim_execution による境界 claim (A2 の重複抑止)、判断点 finalize の mark_applied + outbox 化 (A8)、complete_with_artifact の単一トランザクション (A9)、SpellOutcome (A11)、`_submit_meta_lane` の例外再送出 + 証跡ベース成功判定 (A7)、回復 tick #2 (prepared 回収)。§11 の小物 4 点も確定 (下記)。コミット 3f76619 / 7b2436c / e0ee4ff。**Phase 2 (時間割と予算、計画書 W2) = 実装済み・実機検証待ち (2026-07-20)**: コマ発火を `slot.fire` 台帳実行 (予約 tx→ハンドラ→精算 tx) で包み予算精算を原子化 (A5)、精算失敗を running のまま残して回復 tick が settle-close (A6)、day_open 全置換を検証先行の `replace_day_plan` に統合 (A1)。**A5/A6 は outbox 不要 — slots/予算/episode/台帳が全て world DB なので精算は単一 commit** (§6 A5/A6 の「applied の単一 commit」の実装形)。予算の二重台帳 (旧 `consume_budget` + `consume_life_rounds`) はライフのある日は `lives[].used_rounds` 正典に一本化。回帰=`slot.fire` の予約/精算/回復/degrade + A1 原子性 (test_day_plan/test_life_phase2/test_execution_ledger_wiring/test_judgment_points 計約35件追加、本体スイート 2721 passed)。Phase 0 の器 (テーブル2 + 状態機械 + FIFO配送器 + 関所 + 回復骨格) 実装済み・回帰25件。**結線前半も実装済み (2026-07-16)**: manager 所有 (`saiverse/execution_ledger_wiring.py`) + 起動時回復 (前世代 running sweep + pending 全量配送、`start()` 冒頭で pulse 前に同期実行) + 60秒掃除 tick (EventScheduler key=`execution_ledger_recovery`、掃除のみ=手動モードでも止めない) + 実ハンドラ2種 (`saimemory.append` / `perception.push`、冪等キー=outbox_id を配送先 metadata に刻印)・回帰15件。**Beat関所も結線済み (2026-07-17、§6-2 後半)**: `sea/beat_gate.py` が Beat 開始 (最外周ロック取得) 時に `flush_pending_for_persona` を fail-closed で実行 — Phase 0 (基盤 + Beat ロック + 全結線) はこれで完了。まはー承認済み: 基盤化 + world DB 配置 + 記憶の順序一貫性 (v0.2) + 記憶書き込みの Beat 単位直列化 (v0.3)。§11 の小物 4 点は Phase 1 実装時に確定 (了承済み)。実装点の詳細は対の [`beat_execution_context.md`](beat_execution_context.md)
**位置付け**: 2026-07-12〜15 の一次監査で見つかった「偽成功・不可逆先行」型 P1×16 への共通の答え。個別の穴塞ぎではなく、副作用のある実行すべてが従う世界側の物理法則を一つ増やす。
**前提**: [`audit_second_batch_hardening.md`](audit_second_batch_hardening.md)（第二陣の不変条件「外部mutatorを推測で再実行しない」「user utteranceのdurabilityはPulseより先」は本基盤の先行例）/ [`autonomous_behavior_v2.md`](autonomous_behavior_v2.md) / [`life.md`](life.md) / 監査記録: [自律行動](../handoff/2026-07-14_autonomy_judgment_schedule_audit.md) / [SEA runtime](../handoff/2026-07-15_sea_runtime_session_head_tail_audit.md) / [記憶・人格境界](../handoff/2026-07-12_memory_persona_boundary_audit.md) / [Persona/City/Building](../handoff/2026-07-15_persona_city_building_separation_audit.md)

---

## 1. 何を解くか — 病気の定義

残存 P1 の約半分（16件）が同じ構造を持つ:

> **やり直しの利かない処理**（LLM実行・世界状態の変更）と、**その結果の記録**（予算台帳・本人の記憶・完了マーク・通知）が別々のタイミングで確定し、間で失敗すると「実際に起きたこと」と「記録上起きたこと」が分裂する。分裂したことを誰も検出できず、再試行の口も無いか、再試行すると副作用が二重になる。

実測済みの代表例:

- 作業コマの LLM は動いた（コスト発生）のに、ライフ予算の記帳だけ失敗し、コマは `done` になる。残高が減らないので後続コマが予算超過実行される。
- 就寝判断の finalize が世界更新（タスク・欲求・メモ）を適用した後、本人の判断行の SAIMemory 保存に失敗しても `applied=True / warnings=0` を返す。外から再試行すると世界副作用が二重適用される。
- Chronicle 生成に失敗しても Metabolism anchor が前進し、未編纂の生ログが通常コンテキストから退役する（地図の無い土地が不可視になる）。
- メタ判断の runtime 例外が空成功に変換され、外部イベント（Xメンション等）が判断も直接応対もされないまま消える。

根本原因は 3 つの欠落:

1. **実行の identity が無い** — 「この一回の実行」を指す永続 ID が無いので、再試行・照合・重複抑止のどれも原理的にできない。
2. **成功の証跡が bool 一個** — `submitted=True` に「受付／runtime完了／finalize適用」が潰されており、呼び出し側が安全な再試行可否を判定できない。
3. **DB 跨ぎの書き込みに配達保証が無い** — world DB (saiverse.db) とペルソナの memory.db は単一トランザクションにできず、後段の失敗はログに握り潰すしかなかった。

## 2. 解の骨格 — 部品は 2 つ + 原則 1 つ

### 2.1 実行台帳 (execution_ledger)

一回の不可逆な実行に **execution_id** を採番し、world DB のテーブルで状態を追跡する。

```
prepared ──→ running ──→ applied ──→ completed
   │            │            │
   │            │            └─ (outbox 配送が残っている間は applied)
   │            └─ 例外/プロセス死 → unknown（結果不明。自動再実行しない）
   └─ 失敗/期限切れ → failed（副作用ゼロが保証されるので安全に再実行・破棄できる）
```

| 状態 | 意味 | ここで死んだら |
|---|---|---|
| `prepared` | 台帳に登録済み。**副作用はまだ何も無い** | 安全。回復処理が再実行するか捨てる |
| `running` | 不可逆処理（LLM 等）を開始した | **結果不明 (`unknown`)**。自動再実行しない。照合・裁定対象 |
| `applied` | 世界への適用を world DB に commit 済み。outbox 記録も同一 commit に同梱済み | outbox 配送だけ再開すればよい |
| `completed` | outbox まで全配送済み。終端 | — |
| `failed` | 明示失敗。副作用なし（prepared からの遷移）または適用前に検証で棄却 | 安全に再試行できる |
| `unknown` | running のまま観測が途絶えた。**LLM は動いたかもしれない** | 自動再実行禁止。回復処理は「結果の照合」だけを行い、裁定できなければ明示的な異常として残す |

**冪等キー**: 台帳は `(kind, idempotency_key)` に UNIQUE 制約を持つ。同一境界イベント（例: 同じ persona の同じ営業日の day_open）の二重発火は、二本目の `prepared` INSERT が既存行にぶつかった時点で「既に走った/走っている」と判定できる。watchdog と定刻スケジュールのどちらが先でも同じ一意性へ収束する。

### 2.2 送信トレイ (execution_outbox)

world DB と memory.db を跨ぐ書き込みの配達保証。**「memory.db にこれを書く」というやること自体を、世界側の適用と同一トランザクションで world DB に書く**。

- 適用 commit の直後に即時配送を試み、成功したら `delivered`。
- 失敗しても適用は committed のまま、トレイに `pending` で残る。
- **Pulse 前 flush は必須の関所 (fail-closed)**（まはー裁定 2026-07-16）: 対象 persona の記憶に書く可能性のある実行（会話・自律 Pulse・判断点・import・UI 記憶操作）を開始する前に、その persona 宛の pending を必ず全量配送する。配送できなければ**実行自体を開始しない** — 会話ならユーザーへエラー応答、自律・判断なら `prepared` のまま残して回復処理に委ねる。
- 同一 persona 宛の配送は **FIFO**。
- 配送先の書き込みは execution_id を冪等キーとして刻み、再配送しても二重にならない。

この関所により「pending が残ったまま新しい記憶が書かれる」状態は構造的に存在しない。したがって配送遅延があっても、**実行時刻順 = 配送順 = 記憶の並び順** が常に一致し、遅延分が過去の位置に「挟まる」ことは起こらない（v0.1 §9-5 のトレードオフは消滅）。「**適用済み・記録待ち**」は恥ずかしい事故ではなく、正式な状態として認める — ただしその persona の時間は、配達が終わるまで先に進まない。

### 2.3 記憶書き込みの Beat 単位直列化（まはー裁定 2026-07-16、v0.3）

関所だけでは足りない。同一 persona で複数の生成処理（main 会話と META 判断など）が**並行**すると、互いの結果を知らずに書かれた記録が一直線に並ぶ。**一直線に並んだ記録は「前を踏まえて後が書かれた」ことを含意する** — 並びは単なる順序ではなく文脈継承の宣言であり、踏まえていないのに並べたら記録として嘘になる。位置の調整では解けない（「遅れて届いた過去を今の位置に書く」案は同じ理由で不自然として却下）。

解は並行の全廃: **persona の記憶に書く可能性のある生成処理は、persona 単位の Beat ロックで直列化する。**

- **Beat** = 「関所（pending flush）→ コンテキスト読み → 1 回の生成 → 記録の書き込み」の一続き。ペルソナの最小行動単位（既存コードでは spell ループ 1 周 / `full_merged_text` が実体。本基盤で初めて型を持つ）。
- ロックの排他区間はこの一続き**全体**。これにより、**どの記録も直前の記録を読める状態で生成される** — 「一直線 = 踏まえた」が構造的に真になる。
- ロックは Beat 境界でのみ手放す。META 判断・自律 Pulse・作業セッションの各 Beat は、会話 Pulse の Beat の**間に**挟まる（同時には走らない）。待ちは最大 1 Beat（Pulse 全体ではない）。
- persona ごとに独立のロック。世界全体の並列性（複数ペルソナの同時活動）は失わない。
- 副次: 長い作業セッションも Beat 境界でロックを手放すため、「作業中に話しかけたら応答する」が自然に成立する。Stelis の active thread 取り違え（S4）も、並行書き込みの消滅により Beat 内で thread が安定する。
- 需要の確認: 1 Beat（数秒）も待てない同時書き込みの需要は存在しない。「同時に考えているように見える」体験は Beat のインターリーブで十分に実現される。

Beat 直列化と関所の合成により、送信トレイの配送は常に「次の Beat のコンテキスト読みの前」に完了する。**遅延配送された記録も必ず末尾追記になり、次の生成はそれを読んでから走る** — 実行時刻のまま刻んで一切の矛盾が生じない。

### 2.4 回復処理 (recovery) — 台帳と現実を照合する掃除役

回復処理の仕事は 7 つ。**「行動を生む」か否かで二分**し、完全手動モード（debug_controller / 柱 7 の execution gate）が止めるのは「行動を生む」側だけとする — 手動検証中こそ記録は正確であるべきで、掃除は止めない。

| # | 仕事 | 内容 | 分類 |
|---|---|---|---|
| 1 | pending 配送 | 長く沈黙している persona の未配達分を配る（Pulse が来れば関所が流すため、これは「誰も来ない間」の掃除） | 掃除 |
| 2 | prepared の回収 | 実行が始まらないまま残った `prepared`（プロセス死・関所拒絶・EventScheduler drop）を kind ごとの規則で再実行または期限切れ（`failed`）にする。副作用ゼロが保証された唯一の再実行安全状態 | **行動を生む** |
| 3 | running の期限監視 | kind ごとの実行期限を超えた `running` を `unknown` へ落とす | 掃除 |
| 4 | 起動時の世代照合 | プロセス再起動時、前世代 process の `running` を一括で `unknown` へ（第二陣の `saiverse/runtime_marker.py` の process identity を利用） | 掃除 |
| 5 | unknown の照合 | 外部証跡（WorkSession digest / SAIMemory 判断行 / 成果物 ref / 使用量記録）と突き合わせ、証跡があれば `applied` へ復元して配送再開。なければ `unknown` のまま観測面へ。**LLM 再実行はしない** | 掃除 |
| 6 | dead 化と通知 | outbox の再試行上限超過を `dead` にし、観測面へ通知 | 掃除 |
| 7 | schedule reconciliation | PersonaSchedule（DB 正典）と EventScheduler 予約の世代照合（§8） | 行動を生む |

- **住処**: 独立した世界レベルの定期ジョブ（EventScheduler 上）。AutonomyManager watchdog への相乗りは**不採用** — watchdog は自律のための機構であり、`AUTONOMY_ENABLED=False` の会話専用ペルソナにも配送は必要（回復は自律と無関係な世界の仕事）。
- **タイミング**: プロセス起動時（#1〜7 全種）/ 定期 tick（既定 60 秒仮置き、#1,3,6 の軽い掃除）/ Pulse 前（§2.2 の関所）。
- #2 の回収規則（再実行してよい条件・期限）は kind ごとに定義する。例: `judgment.on_event` は未応対イベントなので再判断を起動（= イベント消失 A7 の回復そのもの）、時間割コマは開始時刻から一定超過で「期限切れ・未実行」として就寝判断の材料に回す。

### 2.5 大原則 — LLM は自動再実行しない

> **再試行してよいのは「結果の記録」だけ。LLM 実行（コストと本人の思考）は、基盤は決して自動再実行しない。**

台帳が「判断 LLM ごと転んだ（prepared/failed）」と「判断は済んで記録だけ転んだ（applied）」を区別できるから、この原則が初めて実装可能になる。`unknown`（LLM が動いたか不明）は最も慎重に扱い、成果物・使用量記録などの外部証跡との照合だけを行う。照合で裁定できない場合は明示的な異常状態として残し、ライフビュー等で観測可能にする。

## 3. 不変条件

1. **副作用ゼロの区間を明確にする** — `prepared` までは世界に何も起きていない。不可逆処理の開始は必ず `running` への遷移**後**。
2. **不可逆位置の前進は成果物 commit の後** — anchor 前進・cursor 前進・完了マーク・`last_notified` 更新は、対応する成果物（Chronicle・個人記憶への転記・配送）の commit を確認してから行う。
3. **全ての副作用は execution_id を冪等キーに持つ** — 同じ実行の再処理で副作用は一回分しか起きない。
4. **成功報告は証跡から導出する** — bool を積み上げない。`applied` の根拠は world DB の commit、`completed` の根拠は outbox の全 `delivered`。callback・戻り値・ログは証跡ではない。
5. **台帳は world DB に置き、ペルソナの memory.db に機構の帳簿を混ぜない** — 記憶側は今までどおりの形のデータが（送信トレイ経由で）届くだけ。人格データと施工記録の分離（まはー裁定 2026-07-16）。
6. **本人の記憶に書く内容は実行時点のもの** — 遅延配送されても本文・名義・実行時刻を変形しない（[[feedback_no_truncation_in_persona_memory_text]] / [[feedback_no_assistant_impersonation_in_memory]] と同じ規律。配送遅延は metadata に記録してよいが、本文には混ぜない）。
7. **失敗した行為を本人の行為として記録しない** — spell 失敗・適用失敗は、成功時と同じ顔の記録（`/spell ...` 正準形）に変換しない。失敗はシステム名義の適用失敗行として明示する。
8. **記憶の順序一貫性**（まはー裁定 2026-07-16）— ペルソナの記憶は起こった順に書かれる。未配達の記録（pending）が残る persona では、記憶に書く可能性のある新しい実行を開始しない（§2.2 の関所）。「間に挟まる」挿入は例外裁定の口を残さず、構造的に禁止する。
9. **記憶の一直線性**（まはー裁定 2026-07-16、v0.3）— 一直線に並んだ記録は「前を踏まえて後が書かれた」ことを含意する。したがって同一 persona の記憶に書く生成処理は並行させず、Beat 単位で直列化する（§2.3）。どの記録も、直前の記録を読める状態で生成されたものだけが並ぶ。

## 4. スキーマ案 (v0.1)

```sql
CREATE TABLE execution_ledger (
    EXECUTION_ID    TEXT PRIMARY KEY,      -- uuid4
    KIND            TEXT NOT NULL,          -- 'judgment.day_open' / 'judgment.on_event' / 'slot.fire'
                                            -- / 'schedule.dispatch' / 'metabolism.run' / 'move.entity' 等
    IDEMPOTENCY_KEY TEXT,                   -- kind 内の境界イベント一意キー。NULL 可（一意性不要な実行）
    PERSONA_ID      TEXT,                   -- 対象ペルソナ（世界横断の実行は NULL）
    STATUS          TEXT NOT NULL,          -- prepared/running/applied/completed/failed/unknown
    PAYLOAD_JSON    TEXT,                   -- 実行の入力文脈（再開・照合に必要な最小限）
    RESULT_JSON     TEXT,                   -- 実行結果の要約（精算ラウンド数、生成物 ref、適用内訳）
    ERROR           TEXT,                   -- 最後の失敗理由
    CREATED_AT      INTEGER NOT NULL,
    UPDATED_AT      INTEGER NOT NULL,
    UNIQUE (KIND, IDEMPOTENCY_KEY)
);

CREATE TABLE execution_outbox (
    OUTBOX_ID       INTEGER PRIMARY KEY AUTOINCREMENT,
    EXECUTION_ID    TEXT NOT NULL REFERENCES execution_ledger(EXECUTION_ID),
    TARGET          TEXT NOT NULL,          -- 'saimemory.append' / 'perception.push' / 'building.ingest_mark' 等
    PERSONA_ID      TEXT,                   -- 配送先 persona（FIFO 順序の単位）
    PAYLOAD_JSON    TEXT NOT NULL,          -- 配送内容（本文・タグ・名義・実行時刻を実行時点で凍結）
    STATUS          TEXT NOT NULL,          -- pending / delivered / dead
    ATTEMPTS        INTEGER NOT NULL DEFAULT 0,
    LAST_ERROR      TEXT,
    CREATED_AT      INTEGER NOT NULL,
    DELIVERED_AT    INTEGER
);
```

設計メモ:

- `dead` は再試行上限や配送先消滅（persona 削除等）で人裁定に回す終端。黙って捨てない。
- 台帳 API はヘルパーモジュール（`saiverse/execution_ledger.py` 想定）に閉じ、生 SQL を各所に書かせない。
- `PAYLOAD_JSON` は「再開に必要な最小限」。会話履歴の複製など巨大データは持たない（参照で指す）。
- 掃除: `completed`/`failed` は保持期間（既定 30 日想定）で prune。`unknown`/`dead` は自動削除しない。

## 5. 責任分界表

| 項目 | 決める者 |
|---|---|
| execution_id の採番・状態遷移の合法性 | **基盤**（ヘルパーが強制） |
| 冪等キーの形式（何をもって「同じ実行」とするか） | **各処理**（kind ごとに intent/コードで定義。例: day_open = `persona:営業日`） |
| LLM 再実行の可否 | **基盤既定で禁止**。例外は無い（re-run したければ人間が新しい実行として起動する） |
| outbox 配送のタイミング・順序 | **基盤**（即時→Pulse 前関所→回復 tick→起動時、persona 内 FIFO） |
| 配送内容（本人の記憶に何をどう書くか） | **各処理**（基盤は payload を運ぶだけで内容に触れない） |
| `unknown` / `dead` の最終裁定 | **まはー**（UI/ログで観測可能にする。安全側の自動規則は処理ごとに宣言） |
| 再試行の上限・バックオフ | **基盤既定**（処理ごとの上書き可） |

ペルソナの意志が関与する項目は無い — 実行台帳は完全にシステム側の器であり、ペルソナからは見えない（本人が見るのは、届いた記録と、失敗が明示されたシステム通知だけ）。

## 6. 適用対象 — 監査 finding とのマッピング

| # | finding（要約） | 台帳のどの性質で解けるか |
|---|---|---|
| A1 | day_open 保存拒否が旧予約だけ先に消す | 全置換を 1 実行に。検証成功（applied）後にのみ予約 cancel/再登録 |
| A2 | day_open/day_close の同日重複発火 | `(kind, persona:営業日)` の UNIQUE 冪等キー |
| A5 | life 消費記帳失敗でも done → 予算超過 | 予約（prepared で予算 hold）→ 実行 → 精算+done を applied の単一 commit に |
| A6 | done 保存失敗で Episode 永久 open | done+Episode close を同一 unit-of-work に。unknown 回復は精算/close のみ冪等再試行 |
| A7 | メタ判断 runtime 例外→空成功、イベント消失 | 成功=finalize 完了の永続証跡。on_event は prepared が durable queue を兼ね、完走しなければ回復対象に残る |
| A8 | finalize の SAIMemory 保存失敗→成功扱い・再試行で二重適用 | 世界更新=applied、判断行=outbox。再試行は配送のみ。副作用は execution_id で冪等 |
| A9 | post-session の completed 先行 commit で接地回復不能 | task 完了+artifact ref を単一トランザクション化し、同一 execution からの補修だけ許可 |
| A11 | spell 失敗を committed 成功として本人記憶に記録 | 不変条件 7。成功 spell だけ正準形、失敗はシステム名義の失敗行 |
| A12 | schedule 設定の予約同期失敗を握り潰し | DB を正典とし、台帳の回復 tick に同居する reconciliation で世代照合（§8 参照） |
| A13 | schedule dispatch 失敗→completed で oneshot 消失 | 発火を claim（prepared）してから dispatch。成功証跡後にのみ COMPLETED |
| S2/M2 | Chronicle 失敗でも anchor 前進 | 不変条件 2。anchor 前進は Chronicle commit 後 |
| S3 | diff 通知の last_notified 先行 | 通知 batch を outbox 化し、全 ack 後に last_notified を前進 |
| S5 | perception 保存失敗で buffer 全削除 | append の成功（message ID 取得）を配送成功条件にし、失敗分は pending 保持 |
| M1 | Chronicle 編纂の persona 単位排他なし | `(metabolism.run, persona:窓)` の冪等キーが原子的 claim を兼ねる（全入口が台帳経由） |
| M8 | Building→個人記憶転記の cursor 先行確定 | 不変条件 2。連続成功した最大 seq まで cursor 前進、失敗 seq 以降は次回再試行 |
| B1 | 移動 DB commit 後の後処理失敗で世界分裂 | location transition を applied とし、イベント・hook を outbox 配送に |

## 7. 段階移行計画

基盤を先に置き、**載せ替えていない箇所は現状のまま動く**（混合状態でリポジトリは壊れない）。実害の大きい順:

- **Phase 0 — 基盤**: テーブル 2 つ + migration、ヘルパー（状態機械の強制・冪等 INSERT・outbox 配送器・回復 tick）、**persona 単位の Beat ロック（記憶直列化 §2.3）と main/META 並行実行の解体**、回帰テスト。
- **Phase 1 — 判断点**: 5 種 finalize と on_event 入口（A2, A7, A8, A9, A11）。偽成功・二重適用・記憶欠落という人格に直結する実害から止める。
- **Phase 2 — 時間割と予算**: コマ実行の予約→精算→Episode close（A5, A6）と day_open 全置換（A1）。ライフ設計の安全境界を閉じる。
- **Phase 3 — schedule**: 発火 claim と reconciliation（A12, A13）。
- **Phase 4 — Metabolism**: Chronicle 排他 claim と anchor 順序（S2/M2, M1）。
- **Phase 5 — 配送系と移動**: diff 通知・知覚バッファ・Building 転記（S3, S5, M8）、移動 outbox（B1）。

各 Phase の完了条件は「対象 finding の監査記録に記された『必要な回帰』が固定されていること」。

## 8. 隣接機構との関係（境界を明確にする）

- **EventScheduler**: インメモリの発火**予約**。台帳は実行**事実**。EventScheduler の「callback 例外は予約 drop」という汎用契約は変えない — 不可逆 callback 側が drop 前に台帳へ実行を登録する契約を必須にする。
- **ScheduleManager / PersonaSchedule**: 宣言的状態（いつ発火すべきか）の正典は DB。予約への反映失敗は「実行」ではなく**照合**の問題なので、解は reconciliation（世代照合ループ）。ただし回復 tick と同じ場所に住まわせ、柱としては本基盤と同じ修正 wave で扱う。
- **第二陣の user utterance durability / client_message_id**: 「ユーザー原文を LLM より先に durable 化し、command 全体を冪等キーで束ねる」— 本基盤と同じ思想の先行実装。将来的に chat command を台帳の kind に載せ替えるかは Phase 5 以降の任意課題（現実装で十分機能している間は触らない）。
- **編纂の `_plan_transaction`（Atlas P4-a 修正）**: 単一 memory.db 内の原子性は既にプロキシ conn で解いた。台帳が受け持つのは **DB 跨ぎ**と**不可逆処理**であり、単一 DB 内の複合更新は今後もローカルトランザクションで解く（台帳を万能にしない）。
- **provider retry（LLM クライアント層）**: 出力開始前の read-only リトライは従来どおりクライアント層の責務。台帳の「LLM 再実行禁止」は**実行単位**の話で、1 実行内の低レベルリトライとは層が違う。

## 9. 引き受ける歪み（トレードオフ）

1. **結果整合になる** — 障害時に「適用済み・記録待ち」「実行済み・精算待ち」が観測できる状態として存在する。ライフビュー・判断コンテキストはこれを「異常」ではなく「回復中」として表示する必要がある。今まで無かったことにしていた分裂が見えるようになる、が正しい理解。
2. **副作用のある処理の作法が重くなる** — 「関数を呼んで失敗したらログ」は書けなくなる。台帳登録→ID持ち回り→状態遷移が作法になり、新機能をサッと生やす自由度は落ちる。これは監査 16 件の請求書の支払いであり、意図的に引き受ける。
3. **書き込みが増える** — 実行ごとに台帳へ 2〜3 回の書き込み。ローカル SQLite なので実測影響は誤差の見込みだが、Phase 0 で計測する。
4. **「何を 1 回と数えるか」は消えない** — 基盤が保証するのは「消えない・二重にならない」まで。冪等キーの粒度と unknown 時の安全側規則は、kind ごとに一度ずつ設計判断する。
5. **記憶が書けないペルソナは活動を停止する** — Pulse 前 flush が fail-closed なので、memory.db に書けない障害の間は、そのペルソナの会話・自律・判断のすべてが開始できない（会話にはエラーが返る）。従来の「記憶に残らないまま活動が進む」を「記憶に残せないなら活動しない」へ置き換えた対価であり、意図的に引き受ける（**記憶の無垢 > 可用性**）。障害が解ければ次の関所で flush が通り、自然に再開する。
6. **persona 内の並列性を放棄する** — Beat 直列化（§2.3）により、同一 persona の生成処理は常に一つずつ走る。会話・META 判断・自律・作業が完全同時には動かず、Beat 境界で交互に挟まる。待ちは最大 1 Beat（数秒）で、この待ちを消す需要は無いと判断した（まはー 2026-07-16）。複数ペルソナ間の並列は無傷。

## 10. やらないこと

- **2フェーズコミット・分散トランザクション** — SQLite 2 個に大袈裟。outbox で足りる。
- **LLM の自動再実行** — いかなる形でも基盤はやらない。
- **全処理の一斉移行** — 段階移行のみ。移行済み/未移行の混在を許す。
- **ペルソナ memory.db への台帳・トレイの設置** — 記憶側は受け取るだけ。
- **env flag による新旧経路の並存** — 載せ替えた kind は旧経路を残さない（[[feedback_no_dead_code_via_flags]]）。ダメなら git revert。

## 11. 未確定・レビュー待ち

1. ~~スキーマの列構成（特に RESULT_JSON に何を標準で入れるか）~~ → **確定 (2026-07-19、W1)**: RESULT_JSON 標準 = `{kind, committed, scope, spells:{attempted,succeeded,failed}, warnings}` + kind 固有 (`reaction` = on_event / `episode_ref` = post_session)。照合 (#5) と呼び出し側読み出しに足る最小。
2. `unknown` の UI 表出（ライフビューに出すか、デバッグパネル止まりか） — 未確定 (観測面 `list_unknown` / `list_dead` は実装済み、UI 配置は保留)
3. 保持期間の既定値（30 日仮置き） — 未確定 (prune 未実装)
4. ~~`prepared` の回収規則・実行期限の既定値（kind ごと）~~ → **確定 (2026-07-19、W1)**: on_event / post_session = 120 秒経過で refire (`resume_execution_id`)、day_open / day_close / post_conversation = 1800 秒経過で `mark_failed("expired")`。running 期限は全 kind 共通 3600 秒仮置き。手動モード persona は「行動を生む」refire をスキップ (掃除は止めない)。実装 = `execution_ledger_wiring._collect_prepared_judgments`。

解決済み（v0.3 後続）: ~~Beat ロックの実装点~~ → 柱 2（model 別 Session）・S4（Stelis）と合流した統合工事 intent [`beat_execution_context.md`](beat_execution_context.md) として起草（2026-07-16）。Beat ロックと関所の配置は同 intent §3.4。
解決済み（v0.2）: ~~回復 tick の住処~~ → 独立の世界レベル定期ジョブ（§2.4。watchdog 相乗りは自律 OFF ペルソナへの配送が死ぬため不採用）。~~配送遅延の時系列~~ → Pulse 前 flush の必須関所化で構造的に消滅（§2.2 / 不変条件 8）。
解決済み（v0.3）: ~~並行実行と配送失敗の合成による過去挿入~~ / ~~「配送時刻の位置に書く」案~~ → 後者は「遅れて届いた過去が今記録されるのは不自然」として却下（まはー）。persona 内の生成処理を Beat 単位で直列化することで、並行書き込みそのものを全廃（§2.3 / 不変条件 9）。実行時刻のまま刻んで矛盾が生じない。

## 経緯: 実行台帳 (execution ledger) (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

監査P1×16の共通根「偽成功・不可逆先行」(LLM実行と結果記録が別々に確定し、間で転ぶと現実と記録が分裂)への共通基盤。
**intent v0.2(2026-07-16、まはーレビュー1巡反映)**。
2部品=実行台帳(execution_id+状態機械+冪等キーUNIQUE)+送信トレイ(world DB↔memory.db跨ぎの配達保証)、大原則=**LLM自動再実行禁止**(再試行は記録のみ)。
**v0.2裁定: 記憶の順序一貫性=Pulse前flushを必須関所にfail-closed化**(未配達が残るペルソナは活動を開始しない、記憶が書けないペルソナは活動停止を引き受ける)。
**v0.3裁定: 記憶書き込みのBeat単位直列化**=「一直線に並んだ記録は前を踏まえて書かれたことを含意する」ため、persona内の並行生成(main/META並走含む)を全廃し、Beat(関所→読み→生成→記録)をpersona単位ロックで直列化。
Beat概念に初めて型が付く。
どの記録も直前の記録を読んでから生成される=実行時刻のまま刻んで矛盾ゼロ。
副次: Stelis thread混線(S4)の構造的緩和、「作業中に話しかけたら応答」が自然に成立。
**回復処理の仕様確定**: 仕事7種を「掃除/行動を生む」に二分(手動モードgateは後者のみ止める)、住処=独立の世界レベル定期ジョブ。
段階移行: Phase 0基盤(Beatロック含む)→1判断点→2時間割/予算→3schedule→4Metabolism→5配送/移動。
**Beatロック実装点は[beat_execution_context.md](beat_execution_context.md)に確定、§11小物4点はPhase 1実装時確定でまはー了承済み**。
**Phase 0の器=実装・検収済(2026-07-16、コミットae357e7)**: テーブル2+状態機械強制+冪等begin+persona単位FIFO配送器+関所flush+回復骨格(期限監視/起動時sweep)+回帰25件。
**結線前半=実装済(2026-07-16)**: manager所有(execution_ledger_wiring.py)+起動時回復(前世代running sweep+pending全量配送をstart()冒頭でpulse前に同期実行)+60秒掃除tick(掃除のみ=手動モードでも止めない)+実ハンドラ2種(saimemory.append/perception.push、冪等キー=outbox_idを配送先metadataに刻印、配送失敗は例外で表明=None握り禁止)+回帰15件。
**Beat関所=結線済(2026-07-17、§6-2後半)**: beat_gate が最外周ロック取得時に flush_pending_for_persona を fail-closed 実行 — **Phase 0 (基盤+Beatロック+全結線) 完了**。
**Phase 1判断点=実装済・実機検証待ち(2026-07-19、W1、コミット3f76619/7b2436c/e0ee4ff)**: claim_executionの境界claim(A2重複抑止)・`_submit_meta_lane`の例外再送出+証跡ベース成功判定(A7)・finalizeのmark_applied+outbox化(A8)・complete_with_artifact単一commit(A9)・SpellOutcome(A11)・回復tick #2(prepared回収)。
同工区でpost_session×digest統合+episode_readスペル(体験の構造 工程(1))。
§11小物4点確定。
回帰41件・本体スイート2666 passed。
**Phase 2時間割/予算=実装済・実機検証待ち(2026-07-20、W2)**: コマ発火を`slot.fire`実行で包む三区間化(予約tx→ハンドラ→精算tx)+`replace_day_plan`保存先行+コマ不変id導入。
Codexレビュー7巡消し込み — 予約keyのslot id化・二重claimの勝者一意化・発火照準のid一貫化・置換のid新世代化・slots/meta書き込みの世代CAS化・増分計算のCAS内側化(mutate_plan_meta)。
回帰約67件・本体スイート2749 passed。
**Phase 3 schedule=実装済・実機検証待ち(2026-07-21、W3)**: 発火を`schedule.dispatch`で包み完了化は成功証跡後のみ(A13)+世代/行トークン+reconciliation(A12)。
Codexレビュー10巡27件消し込み。
**Phase 4 Metabolism=実装済・実機検証待ち(2026-07-21、W4)**: M2残片をepisode整列の新設経路で消化(体験の構造 工程(2)と統合) — チャンク単一tx・列のあふれ束ね親子単一tx・evict boundary・digest材料除外・API生成ジョブのM1 claim結線。
**Phase 5 配送系と移動=実装済・実機検証待ち(2026-07-21、W5)**: S5(perception flushのNone検査)+M8(転記cursor後行確定+provenance冪等)+ライフ境界通知outbox化(マーカーと単一commit)+B1(move.entity台帳化) — **これで段階移行計画Phase 0〜5が全段実装済み**。
残: まはー実機検証
