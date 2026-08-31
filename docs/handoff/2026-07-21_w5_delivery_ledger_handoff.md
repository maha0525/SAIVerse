# W5 走行メモ — 実行台帳 Phase 5: 配送系と移動 (2026-07-21)

> セッション固有の走行メモ。工程の真実は [完了計画書](../overview/audit_remediation_plan.md) W5。
> 対象 finding: **S5 完了化** (SEA 監査) / **M8** (記憶監査 Building転記) / **B1** (分離監査 移動分裂) / **W3 委譲** (境界通知の outbox 化、[W3 走行メモ](2026-07-20_w3_schedule_ledger_handoff.md) 第六陣 P2 の恒久解)。

## 患部の事実 (調査済み)

| # | 患部 | 事実 |
|---|---|---|
| S5 | `saiverse_memory/adapter.py` `flush_perception_buffer` | `append_persona_message` の戻り値未検査。`_append_message` は例外を内部で握って None を返す (2551-2553) ため「通常の保存失敗」は例外にならず、pending 全削除 + True に到達する (= 知覚の不可逆消失) |
| M8 | `builtin_data/tools/get_building_messages.py` `auto_ingest_building_messages` | cursor 前進 (L352) が転記より**先**。転記ループは per-message except で握って続行 (L452)。`_mark_ingested` も失敗を握る。DB `mark_ingested` は「新規追加 True / 既マーク・行なし・**DB失敗**」を全部 False に混同。tool 版 `get_building_messages()` (L57-105) も同型の cursor 先行 |
| 境界 | `saiverse/day_plan.py` `_handle_life_start/_end` + `_notify_life_boundary` | 通知は直接 append (しかも戻り値 None 未検査 = S5 と同型)。マーカー (`mark_life_started/ended`) と通知が別 commit — 「通知成功→報告前 crash」の at-least-once 窓と「マーク失敗の無条件 True」(W3 第六陣の暫定) が残存 |
| B1 | `saiverse/occupancy_manager.py` `move_entity` | commit (L254) 後の後処理 (occupants 更新 / leave・enter イベント / dynamic state / addon hook / game lifecycle) が裸。`add_building_event` は try 外で、例外は catch-all に落ちて **commit 済みなのに False を返す** (rollback は commit 後で無力) = DB は移動済み・呼び出し元属性は旧のまま世界分裂。`db_session` パラメータの実利用者はゼロ (runtime/admin/day_plan/tool 全確認) |

補助事実: `add_building_event` は world DB への INSERT のみ (in-memory 更新なし) → 移動 tx に同居可能。`pulse_cursors` は in-memory + `_save_conscious_log` で `persona_pulse_cursor` へ随時永続化 (auto_ingest 内では永続化しない)。`mark_applied(session=)` が「世界側の書き込み + applied + outbox を呼び出し元の 1 commit に同梱する口」(W1 設計)。ledger 不在スタブへの degrade 慣行は W2 確立 (`getattr(manager, "execution_ledger", None)` + WARN 一度)。

## 設計 (Fable 裁定)

### D1. S5 — flush の成功条件を「message id 取得」に

`flush_perception_buffer`: `mid = append_persona_message(...)` を受け、falsy なら WARNING + pending 保持 + False。例外経路は従来どおり。監査の「DB commit 例外・embedding 例外」は `_append_message` 内部で握られ None になる経路なので、None 検査が全てをカバーする。

**引き受ける残差** (文書化する縮退であり隠さない): (a) append-commit と delete-commit は別 commit (`sai_memory/memory/storage.py` の add_message / delete_perceptions が各自 commit する構造) — 間で**プロセスが死んだ場合のみ**次 flush で 1 回だけ重複追記 (消失ではない)。単一 tx 化は storage 層の commit 規律全体の改修になり W5 の血管を越える。 (b) delete 失敗の例外は従来どおり伝播。

### D2. M8 — cursor は「連続消費の最大 seq」まで、limbo は provenance で冪等

- 候補を seq 昇順で処理。「消費」= ルールスキップ (heard_by 外 / ingested 済 / 空 content / occupancy イベント等) or 転記成功 (append + mark 両成功)。**失敗で即停止** (以降の seq は次ラウンド)。cursor = 停止点までの連続消費 seq。
- mark の成否検出: DB `mark_ingested` を「DB 失敗は raise / 行なし・既マークは False のまま」に改める (swallow 廃止。呼び出し元は本 tool のみ — Grep 確認)。tool 側は「例外なし = mark 決着」とみなす (行なし = 消えた行にマーク不要)。
- 冪等: 転記 entry の metadata に `building_msg_ref = "{building_id}:{message_id}"` を刻む。**停止の意味論により append 済み・mark 未の limbo は常に高々 1 件で、必ず次ラウンドの最初の転記候補**。よって「ラウンドの最初の転記候補のみ」adapter へ provenance 照会 (`_find_ledger_message` と同型の LIKE 一回) — 見つかれば append をスキップし mark の修復だけ行う。走査は 1 ラウンド 1 回に有界。
- 再起動: cursor が巻き戻っても ingested_by (DB) がルールスキップ、limbo は上記で守られる → 欠落も重複もない。
- tool 版 `get_building_messages()` の cursor 先行も同じ骨格に載せる。

### D3. 境界通知 — kind=`life.boundary_{start,end}`、マーカーと outbox を単一 commit

- `day_plan` に共通実装 `_apply_life_boundary(manager, persona_id, plan_date, index, life, kind)`:
  1. ledger 不在 → 従来直接経路に degrade (W2 慣行、WARN)。
  2. `claim_execution(kind, key="{persona}:{plan_date}")` → completed/applied = True (決着済) / running = False (並走) / unknown = False + ERROR。
  3. `try_mark_running` 敗者は離脱 False。
  4. 冪等段 (start: TTL override + 前ライフ解除予約 cancel / end: keep-alive cancel + TTL 解除予約) 失敗 → `mark_failed` → False (schedule backoff 再試行、claim は failed キー退避で新 prepared)。
  5. `mutate_plan_meta` に **`in_session_extra`** (勝ち試行の commit 直前に同一 session で呼ぶフック) を新設し、マーカー書き込みと `mark_applied(execution_id, outbox_items=[通知], session=db)` を**単一 commit** に。通知 payload は従来 `_notify_life_boundary` と同一の message dict (target=`saimemory.append`)。adapter 不在 (未ロード) は outbox 空 = 従来の no-op と同義。
  6. mutate が None (並走が先にマーク) → `mark_applied` (outbox なし) + completed → True。
  7. commit 後 `flush_pending_for_persona` (即時配送試行、失敗しても True — durable なので関所 / 回復 tick が届ける)。
- `autonomy_wiring` の `_confirm_life_at_day_open` / `_apply_life_end_at_day_close`: `_handle_life_start/_end` 呼び出し + マーク即時リトライループを新関数に置換 (fast-path の marker 既設 → True は維持)。
- **消えるもの**: マーク失敗の無条件 True (W3 第六陣暫定) / 「通知成功→報告前 crash で再適用」の at-least-once 窓 (通知はマーカーと同時に durable、配送は outbox_id 冪等)。

### D4. B1 — 移動を kind=`move.entity` の台帳実行に (「commit 後に False」の根絶)

1. 事前チェック群は不変 (False はここまでで出し切る)。ledger 不在は従来経路に degrade。
2. `begin_execution` (key なし = 一回性) + `mark_running`。
3. **単一 tx**: 位置遷移 (occupancy log close/open or `User.CURRENT_BUILDINGID`) + leave/enter の building_messages INSERT (`insert_building_message` から session 内核 `insert_building_message_in_session` を抽出、heard_by は移動後の占有を無変異で算出、quarantine 判定は従来と同義に維持) + `mark_applied(session=db, outbox_items=移動後処理)` → commit。
   - outbox: AI = `move.post_dynamic_state` / `move.post_addon_hooks` / `move.post_game_lifecycle`、user = game_lifecycle のみ (現行分岐どおり)。payload は {entity_id, entity_type, from_id, to_id} を凍結。
   - SQLite の単一書き手直列化により、tx 内の max(seq) 読みは先行する位置遷移の flush で write ロック獲得後 → seq 競合レースなし (W4 の BEGIN IMMEDIATE と同根拠)。
4. tx 失敗 → rollback → `mark_failed` → False (**何も起きていない** — 分裂なし)。
5. commit 後: in-memory occupants を確定遷移から更新。**ここ以降 False を返さない**。
6. `flush_pending_for_persona` (即時配送)。ハンドラ 3 種を wiring に新設: dynamic_state = `DynamicStateManager.on_building_entered` (例外 = 再配送) / addon_hooks = exited→entered の順で `dispatch_hook` / game_lifecycle = `on_entity_moved`。配送失敗は pending/dead に残るだけで移動は成功のまま (= 監査の「outbox 再配送状態として記録」)。
7. `db_session` パラメータは削除 (利用者ゼロの死んだ口 — `saiverse_manager._move_persona` / `runtime._move_persona` も追従)。

**スコープ境界 (W7 送り)**: 監査修正方針の「persona/user 属性の更新責務を caller 群から移動 service へ集約」は、`persona.current_building_id` を書き換えない契約が明文化され (day_plan.py L2874 コメント)、5 呼び出し箇所 + `_mark_entry` 儀式が依存する横断契約のため、**柱5 (W7 位置・占有) の canonical 化と同時に行う**。W5 で「commit 後 False」が消えることで、呼び出し側の属性更新は「成功時のみ・成功 = DB確定済み」となり分裂の実害は閉じる (残る窓は「move_entity return 直後のプロセス死」のみ — 再起動時に DB から位置を復元するため自己回復)。immediate flush は move_entity 内 (return 前) なので「on_building_entered は属性が旧のまま走る」前提も正常経路で不変 (再配送時のみ属性が新 — on_building_entered は明示 building_id 引数で動くため成立)。

## 実装チャンク (全チャンク実装済み 2026-07-21)

- A ✔ D1 (adapter `flush_perception_buffer` の None 検査) + 回帰 2 件 (test_core_memory_scene_api.py)
- B ✔ D2 — 変更点: `builtin_data/tools/get_building_messages.py` 全面組み替え (共通核 `_ingest_round` / `_transcribe_message` / `_append_and_mark`、tool 版・auto 版の双子を単一の転記規律に) / `persona/history_manager.py` の `_sync_to_memory`・`add_to_persona_only` に成否契約 + `memory_first` / `database/building_messages.py` `mark_ingested` を DB エラー raise 化 / adapter に `find_message_by_building_ref`。回帰 7 件 (test_building_ingest_m8.py) + 3 件 (test_history_manager.py)。**副産物の実バグ発見**: auto_ingest 経路の `_mark_ingested` が `get_active_manager()` (contextvar、pulse 開始時は None) に頼っており **DB の ingested_by が一度も永続化されていなかった** — cursor 先行バグと相殺して隠れていた。manager 明示渡しで根治
- C ✔ D3 — `mutate_plan_meta` に `in_session_extra` / `day_plan.apply_life_boundary` 新設 (kind `life.boundary_{start,end}`) / `_life_mark_mutator` 共通化 / autonomy_wiring の両呼び出し元を置換 (マーク即時リトライループ撤去)。W3 期テスト 2 件を新契約 (マーカー tx 失敗 = False + failed) に書換 + 新規 3 件 (単一 commit / tx 失敗の全巻き戻し→回復 / 配送一時失敗の一度きり追記)。既存の `_handle_life_*` / `_notify_life_boundary` は縮退経路として存置 (docstring に明記)
- D ✔ D4 — `insert_building_message_in_session` 内核抽出 (外側は従来のリトライ殻) / `move_entity` 書き換え (事前チェック → begin+running → 単一 tx [位置遷移 + イベント + applied + outbox] → commit 後 in-memory 更新 + 即時 flush、commit 後は False を返さない) / `_move_entity_legacy` (台帳なし縮退) / wiring に `move.post_dynamic_state` / `move.post_addon_hooks` / `move.post_game_lifecycle` の 3 ハンドラ / `db_session` 口を全層削除 (occupancy_manager / runtime._move_persona / saiverse_manager._move_persona)。回帰 8 件 (test_move_entity_ledger.py)
- E: 本体スイート + ruff + Codex レビュー + docs 同期 (計画書 / レビュー台帳 / in_flight / execution_ledger intent — 同期済み)

## Codex レビュー 1 巡 4 件消し込み (受諾 4 / 却下 0、2026-07-21)

`/codex:review --scope working-tree` で全件裏取り済み。**P1×2 は同一の根本原因** (`ExecutionLedger._delivery_lock` が非再入 `threading.Lock` で、新設した outbox ハンドラが「別の移動」を誘発し同じスレッドから再度 `flush_pending_for_persona` を呼ぶ経路を見落としていた)。

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P1 | `move.post_dynamic_state` ハンドラ (`on_building_entered`→`inject_diff_notifications`→`mark_applied(deliver=True)`) が配送ロック保持中に自分自身の配送を再度呼びデッドロック | 確認 (`mark_applied(deliver=True)` は内部で `flush_pending_for_persona` を呼ぶ実装) | `ExecutionLedger` に `threading.local` の再入検知を追加 — ネストした呼び出しは配送を試みず `False` で即離脱 (次の即時配送・Beat 関所・回復 tick が拾う)。戻り値を実判定に使う唯一の呼び出し元 (`beat_gate._flush_gate`) は最外周専用なのでネストされる側にならず無傷 |
| P1 | `move.post_game_lifecycle` ハンドラ (`on_entity_moved`→`_move_party_to`→`summon_persona`/`move_user`→別の `move_entity`) が同じ経路で再入デッドロック | 確認 (`_move_party_to` が別 persona/user の `move_entity` を誘発し、その中で自分の `flush_pending_for_persona` を呼ぶ) | 同上の再入検知で一括解消 (根本原因が同一) |
| P2 | `on_entity_moved`/`on_building_entered` は内部失敗を吸収して正常 return する既存 (W5 以前からの) best-effort 設計のため、outbox ハンドラがそれを「配送成功」と誤記帳する | 確認 (両関数とも try/except で握って続行する契約) | 両関数 + `_dispatch_head_event` を bool 化 (「全段成功したか」を集約、各段の best-effort 性は不変)。wiring 側ハンドラが `False` を見て `RuntimeError` を投げ、outbox の再試行対象にする。既存の直接呼び出し元 (縮退経路・既存テスト) は戻り値を無視するため無傷 |
| P2 | Building 転記の provenance 照会 (`_memory_has_building_ref`) が例外時 `False`(未登録)に倒すため、一時的な照会失敗の直後に append が成功すると重複保存されうる | 確認 (自分が書いた `except Exception: return False` が該当) | 照会の**実行失敗**は `_append_and_mark` 側で `_TranscribeFailed` に変換してラウンドを停止 (「未確認」を「見つからなかった」より安全側に倒す)。照会機能そのものが無い環境 (adapter 未対応) は従来どおり `False` (fallback) |

回帰: `test_move_entity_ledger.py` に再入デッドロック固定 (`MoveDeadlockRegressionTest`) + 両ハンドラの失敗伝播テスト2件、`test_building_ingest_m8.py` に照会失敗の停止テスト1件。既存テスト1件 (`test_game_lifecycle_handler_calls_on_entity_moved`) はスタブの戻り値不備で落ちたため修正。

**教訓**: 「commit 後の後処理を outbox 配送で at-least-once 実行にする」設計は、後処理ハンドラ自身が同じ配送機構を再帰的に呼ぶ経路 (ハンドラが誘発する副次的な移動・通知) を見落としやすい。W2/W3 で得た「回復が実際に発火する経路まで検証する」の教訓と同型 — 今回は「配送機構自身の再入安全性」が盲点だった。

## 引き受ける残差 (意図的・記録)

- S5: append-commit と pending delete-commit は別 commit (storage 層が各操作で commit する構造) — 間の**プロセス死**でのみ次 flush が 1 回だけ重複追記 (消失ではない)。単一 tx 化は storage 層の commit 規律全体の改修になるため W5 では見送り。
- 境界/移動の台帳実行が running 中に crash した場合、`recover_stale_running` (期限後) が unknown に落とし claim をブロックする — 境界はマーカー fast-path が既適用を吸収するため、unknown が実害になるのは「本当に未適用のまま死んだ」稀ケースのみで、裁定 (list_unknown) 待ちが正直な状態。prepared 残留は次の claim (prepared 再利用) が自然回収。
- B1 の「persona/user 属性更新の移動 service への集約」は W7 (柱5 canonical 化) 送り — 明文契約 (day_plan.py の「move_entity は current_building_id を書き換えない」コメント) と 5 呼び出し箇所 + `_mark_entry` 儀式に跨るため。W5 の「commit 後 False を返さない」で分裂の実害 (失敗報告と DB の乖離) は閉じた。残る窓は「move_entity return 直後のプロセス死」のみで、再起動時に DB から位置復元されるため自己回復する。

## 検収基準 (監査「必要な回帰」)

- S5: append=None・内部で握られる DB/embedding 失敗で pending 残存 + False、次 Pulse 成功で一度だけ消費。
- M8: 途中 message の append 失敗 / mark 失敗 / DB lock / プロセス再起動で、欠落も重複もなく最終的に全 heard message が取り込まれる。
- 境界: マーカーと outbox が単一 commit (途中失敗でどちらも無い) / 配送失敗 → pending → 再 flush で一度だけ追記 / 決着済み claim の再突入が副作用ゼロ / degrade 経路の従来動作。
- B1: tx 内失敗で位置・イベントとも無変化 + False / commit 後のハンドラ失敗で True + pending 残存 → 再 flush 配送 / user 移動 (persona_id=None キュー) / db_session 口の除去。
