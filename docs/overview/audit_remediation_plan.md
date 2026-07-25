# 監査対応 完了計画書 (audit remediation plan)

> **これは何**: 2026-07-12〜16 の一次監査 (Codex GPT-5.6 Sol、全8サブシステム) で出た全 finding を「完全終了」まで運ぶための**唯一の工程表**。
>
> **使い方 (まはー / メティス共通)**:
> - **セッション開始はこのファイルから**。「現在地」→「次の wave」を見れば、前回どこまで進んだかの再構築が要らない。
> - **セッション終了時にメティスが更新する** (wave の状態チェックと「現在地」の日付)。handoff 文書はセッション固有の走行メモ (検収手順・走行中エージェント) に格下げし、工程の真実はここに一本化する。
> - finding 単位の真実は [レビュー台帳](code_review_ledger.md) と各監査文書が持つ。この計画書は **wave 単位** で持つ (二重管理しない)。
>
> **完全終了の定義**: 全 finding が「回帰固定済み」「意図的保留 (まはー裁定記録済み)」「凍結スコープ」のいずれかに分類され、実装 wave の成果が実機検証を通過し、レビュー台帳の全行が消し込み済みになること。

---

## 現在地 (2026-07-25 更新 — W4 差し戻し (chronicle_eviction) は下段のみ実装、上段=級2以上と接続していないことが判明し再設計へ)

```
一次監査     ████████ 完了 (全8サブシステム、2026-07-16)
柱の裁定     ████████ 完了 (8柱すべて方針確定、2026-07-16)
基盤工事     ████████ 完了 (実行台帳 Phase 0 + 統合工事 §6 ※§6-6bのみ分離)
実装 wave    ███████▶ W1〜W7 + W13 実装済み (実機検証待ち。W4 は退場設計の差し戻しあり) / W8 進行中 (S7 済・残A3) / W9〜W12・W14 未着手
実機検証     █░░░░░░░ ライフ一日検証を実施中 (まはー、2026-07-17〜)
```

**済んだ大物** (詳細は §「完了済みの記録」):
- 柱2 (model 別 Session) = 統合工事 §6-1〜§6-7 で**完結** — S1/S2/S3/S4/S8 + M1 + 記憶監査第4片を根治
- 柱3 (multi-city) = 凍結・入口封鎖済み / 柱4 (native import) = 復元/移植分離済み
- **実行台帳 = Phase 0〜5 全段実装済み** (器 + Beat ロック + 判断点 + 時間割/予算 + schedule + Metabolism + 配送系/移動)
- **SEA 監査 = 非保留 finding 全消し込み** (W6 で S6 消化。残 S7/S9 は柱6/柱8 スコープ)
- **柱5 (位置・占有) = W7 で分離監査の非凍結 finding 全消し込み** (2026-07-21)

**次にやる wave**: **W4 差し戻し = chronicle_eviction 実装** (intent 2026-07-25 レビュー通過。詳細は W4 の差し戻し行)。W4 以外の実機検証 (W1〜W3 / W5〜W8) は退場経路と独立なので chronicle_eviction を待たずに消化できる。その後 W8 残り (A3 の裁定・着手) or W9 (柱7 — 完全手動モード)。W8 の S7 は 2026-07-22 実装済み (実機検証待ち)。**W1〜W7 は実装済み・実機検証待ち** (W1: 2026-07-19、コミット 3f76619 / 7b2436c / e0ee4ff。W2: 2026-07-20。W3: 2026-07-21。W4: 2026-07-21 — Chronicle 生成の episode 整列化 + M2 消化 + Track Chronicle 生成廃止。W5: 2026-07-21 — S5 完了化 / M8 / B1 / 境界通知 outbox 化。W6: 2026-07-21 — head の fail-closed 化 = S6。W7: 2026-07-21 — 柱5 位置・占有の canonical 化)。

**一本化 (2026-07-19 まはー裁定)**: [体験の構造](../intent/experience_structure.md) の実装工程はこの計画書に統合された — 工程(1)=W1 同工区 / 工程(2)=W4 統合 (旧バッチ生成を固めず新設経路で M2 消化) / 工程(3)=W13 / 工程(4)=W14。**工程の真実は二重管理せずこの一枚が持つ**。

---

## 工程表 (wave 一覧)

順序は依存関係順。各 wave は「調査 (Explore) → 設計 (メイン) → 実装 (委譲) → 検収 (メイン) → コミット」の型で回す。**状態**: ☐ 未着手 / ▶ 進行中 / ☑ 実装済み (実機検証待ち) / ✅ 完了。

### W1 ☑ 実行台帳 Phase 1 — 判断点 (柱1 の核) — 実装済み・実機検証待ち (2026-07-19)

- **スコープ**: 5種 finalize と on_event 入口を実行台帳に載せる。A2 (day_open/close 同日重複 → `(kind, persona:営業日)` UNIQUE) / A7 (メタ判断例外→空成功・イベント消失 → 成功=finalize 完了の永続証跡、on_event は prepared が durable queue) / A8 (finalize 保存失敗→成功扱い・二重適用 → 世界更新=applied + 判断行=outbox、副作用は execution_id で冪等) / A9 (post-session の completed 先行 → task 完了+artifact ref の単一トランザクション化) / A11 (spell 失敗を committed 成功で記録 → 失敗はシステム名義の失敗行)
- **同時に確定**: intent §11 の小物4点 (RESULT_JSON 標準列 / prepared 回収規則・期限の kind 別既定値ほか — Phase 1 実装時確定でまはー了承済み)
- **同工区で実施 (2026-07-18 裁定)**: post_session×digest 統合 — digest 専用コール廃止、状況文にセッション原本 (コールローカル注入)、digest は post_session の出力欄に + **episode 読み口スペル新設** (詳細は [judgment_points.md](../intent/persona_cognition/judgment_points.md) §6 冒頭の改定決定)。A9 と同じ finalize 経路を触るため同時に。**= [体験の構造](../intent/experience_structure.md) 実装工程(1)** (2026-07-19 一本化裁定)
- **参照**: [execution_ledger.md](../intent/execution_ledger.md) §7 / [自律行動監査](../handoff/2026-07-14_autonomy_judgment_schedule_audit.md) / [走行メモ](../handoff/2026-07-19_w1_judgment_ledger_handoff.md) (設計 D1〜D10)
- **完了条件**: A2/A7/A8/A9/A11 が回帰固定済み、レビュー台帳の自律行動行を消し込み更新
- **実装済み (2026-07-19、コミット 3f76619 Chunk A / 7b2436c Chunk B / e0ee4ff Chunk C)**: A2/A7/A8/A9/A11 を回帰固定 (追加テスト計 41 件)、§11 小物4点確定、digest 統合 (a') + `episode_read` スペル実装、本体スイート 2666 passed 全緑。**残 = まはー実機検証** (判断点の重複抑止・偽成功 drop なし・digest 一本化の観測)。検証通過で ✅ + レビュー台帳の A2/A7/A8/A9/A11 を実機確認済みに

### W2 ☑ 実行台帳 Phase 2 — 時間割と予算 — 実装済み・実機検証待ち (2026-07-20)

- **スコープ**: コマ実行の予約→精算→Episode close (A5, A6) と day_open 全置換 (A1)
- **確定設計 (Fable 裁定)**: [走行メモ](../handoff/2026-07-20_w2_slot_ledger_handoff.md)。核心 = slots/予算/episode/台帳が全て world DB → A5/A6 精算は outbox なしの単一 tx。A5+A6 は同一患部 `_fire_slot` を `slot.fire` 台帳実行 (予約 tx→ハンドラ→精算 tx) で解く / A1 は原子的全置換 `replace_day_plan` (順序修正のみ、台帳不要)。予算二重台帳をライフ正典に一本化
- **実装済み (2026-07-20)**: Chunk A (台帳 `mark_running`/`recover_stale_running` に session=/exclude_kinds、episode open/close に session= + `invalidate_open_cache`) → B (`_fire_slot` 三区間化 + 予約/精算 tx + 予算一本化 + degrade 経路) → C (`settle_stale_slot` 回復 tick 結線 + `replace_day_plan` + finalize 差し替え)。A1/A5/A6 を回帰固定 (監査「必要な回帰」準拠、約35件追加)、本体スイート **2721 passed 全緑**、ruff clean。**残 = まはー実機検証** (予算精算の原子性・crash 後の settle-close・day_open 全置換の孤児なし)。検証通過で ✅ + レビュー台帳の A1/A5/A6 を実機確認済みに
- **完了条件**: A1/A5/A6 を回帰固定 (監査「必要な回帰」) + 台帳消し込み ✔

### W3 ☑ 実行台帳 Phase 3 — schedule — 実装済み・実機検証待ち (2026-07-21)

- **スコープ**: 発火 claim と reconciliation (A12, A13)
- **確定設計 (Fable 裁定)**: [走行メモ](../handoff/2026-07-20_w3_schedule_ledger_handoff.md)。A13 = 発火を `schedule.dispatch` 台帳実行で包む (claim → 席取り → 型付き outcome → 精算 world-DB 単一 commit、failed は backoff ×3、unknown は自動再実行禁止)。A12 = `SYNC_GENERATION` 世代列 + 行一生 `INSTANCE_TOKEN` 列 + 回復 tick #7 の reconciliation (60 秒で自己回復) + API `scheduler_synced` 明示
- **実装済み (2026-07-21)**: Chunk A (世代列 + 観測フィールド + 型付き dispatch + find_execution) → B (`_handle_fire` 台帳化) → C (reconciliation + 結線)。Codex レビュー 10 巡 27 件消し込み (受諾 24 / 裁定却下 3 — キー三軸化 {id}:{instance}:g{世代}:{occurrence} / tri-state 同期応答 / day_open・day_close 境界の冪等マーカー + 失敗伝播 / periodic の prepared・failed 回収 / 精算フェンス / applied sweep / 世代のサーバー側インクリメント。打ち切り裁定は走行メモ末尾)。A12/A13 を回帰固定 (schedule 系テスト約 70 件追加)、本体スイート全緑、ruff clean。**残 = まはー実機検証** (dispatch 失敗時の oneshot 非消失・register 失敗の 60 秒自己回復・世代照合の旧予約空振り)。検証通過で ✅ + レビュー台帳の A12/A13 を実機確認済みに
- **完了条件**: A12/A13 を回帰固定 (監査「必要な回帰」) + 台帳消し込み ✔

### W4 ▶ 実行台帳 Phase 4 — Metabolism 残片 = 体験の構造 工程(2) と統合 — 退場の下段は実装済み・上段(級2以上)は再設計中 (2026-07-25)

- **スコープ**: M2 (Chronicle 生成の残る原子性課題)。**S2/M1 は統合工事 §6-5 で先取り済み** — 差分を調査してから着手 (残量は小さい可能性が高い)
- **統合裁定 (2026-07-19)**: Chronicle 生成は [体験の構造](../intent/experience_structure.md) 工程(2) で episode 整列 (サイズ+帯あふれ束ね・バッチ降格・恒等圧縮) に世代交代する。**旧バッチ生成経路に hardening を入れない** (捨てる経路を固めるのは二度手間) — 新設経路を最初から原子的に作り、M2 はそこで消化する
- **確定設計 (Fable 裁定)**: [走行メモ](../handoff/2026-07-21_w4_metabolism_ledger_handoff.md)。M2 の残片を 5 欠陥 (M2-a 親子別 commit の二重生成 / M2-b 窓内圧縮 §4-1 違反 / M2-c digest 再圧縮 §4-4 違反 / M2-d source 重複無防備 / M2-e dismantle 複合更新) に具体化し、新設経路で消化
- **実装済み (2026-07-21)**: 整列計画 `alignment.py` (純関数、見積もりと生成の一点管理) + チャンク実行 `executor.py` (チャンク単一 tx + 重複再検査 + 由来メタ) + 帯あふれ束ね `bands.py` (親子単一 tx = M2-a 根治、壁、帰化バックフィル)。D1 退場時圧縮 (evict boundary = M2-b) / D2 退役の episode スナップ (open episode の内部で切らない) / D5 session_digest 材料除外 (M2-c) / D8 Track Chronicle 生成廃止 (§11-10) / D9 API・CLI・estimate・frontend 載せ替え + API 生成ジョブに M1 claim 結線 (旧: claim 素通りの別コネクション入口)。旧経路 (ArasujiGenerator / maybe_consolidate / gap-fill / dismantle 経路) は削除。**Codex レビュー 5 巡 20 件消し込み (受諾 20 / 却下 0、10→6→3→1→0 で対象単調縮小、明細は走行メモ)**: tx 内再検査の BEGIN IMMEDIATE 原子化 / 束ね子検査の同 / dry 予測と backfill の順序 (backfill を計画前へ) / 安全弁の試行数カウント / claim を claim_execution+try_mark_running へ (failed キー退避 = キャンセル・失敗後の同窓即時再試行) / backfill 全体の単一 tx 化 (lost update 閉塞) ほか。回帰=alignment 20 / executor 10 / bands 18 / metabolism 18 計 66 件 + gold_panning 35 全緑、本体スイート全緑、ruff clean。issue 消し込み: general_chronicle_metabolism_trigger (D1 で解決)・chronicle_generation_dual_pipeline (5/28 解決の移動漏れ) を archive へ。**残 = まはー実機検証** (episode 転写の恒等性・帯束ねの初回帰化・open episode スナップの観測)
- **実機検証で出た欠陥 (2026-07-24)**: エリスの Chronicle 一覧に単独発言のエントリが並ぶというまはーの観察から調査。**級 1 ノードが標準被覆 U=1 万字に対し実測 9〜6,344 字に崩れている** — 自動経路の evict 境界 (退場の刻み幅) は U と無関係に数件ずつ動くのに、`_plan_run` は run 末尾で `_flush_pending()` を無条件に確定するため、「まだ相手が退場していない端数」が「束ねる相手がいない豆粒」(§4-3 恒等圧縮) と同一視される。§4-4 (同一レベルの再圧縮禁止) により後から束ね直せない。級 2+ 側の `bands.py` `_select_bundle_run` には「目標未達の列は束ねず持ち越す」規律があるのに**級 1 だけ持ち越しの器が無い**という非対称 → [`chronicle_undersized_lv1_chunks`](../issues/chronicle_undersized_lv1_chunks.md) (解消は下の差し戻し行へ)。なお発端の 2 エントリ自体は W4 の欠陥ではなく、W5/M8 で根治済みの `ingested_by` 非永続化バグが 7/18 に作った重複 user 行を、W4 の集合ベース未処理判定が**正しく**拾った結果 → [`chronicle_orphan_duplicate_user_messages`](../issues/chronicle_orphan_duplicate_user_messages.md) (データ掃除のみ残)
- **差し戻し (2026-07-24→25): 退場設計の世代交代 = [chronicle_eviction.md](../intent/chronicle_eviction.md) 実装 — 実装待ち**。当初の借用案 (級1端数を提示中の生ログで最小限巻き込んで U を満たす) は Codex レビューで **open episode 分断の P1** が出て撤回。まはーとの設計対話で退場の粒度そのものを変える上流の解に到達し、intent は **2026-07-25 レビュー通過・設計確定**。スコープ = ①退場を時系列一本線→ **episode 単位** (open は単独 digest・closed 同士は束ねる) ②水位をメッセージ数→ **文字数三水位** (既定 低4万/目標6万/高12万、全モデル一律・モデルファイルでユーザー設定可) ③ experience_structure §6 **pulse 関節細分**の実装 (open episode の部分圧縮) ④ digest の**時系列位置置き換えレンダリング** + 圧縮マーク注釈 ⑤借用実装 (alignment / session_lifecycle / テスト、未コミット) の撤回。**W4 検証項目の読み替え**: evict boundary (D1) と open episode 丸ごと回避スナップ (D2) はこの実装で世代交代するため旧実装のまま検証しない。恒等転写・帯あふれ束ね (bands)・チャンク単一 tx (M2-a/c/d/e) は現行のまま有効。**上書きされる裁定**: 柱2 §6-5 の「Metabolism 閾値はモデル依存」→ 全モデル一律・文字数三水位 ([beat_execution_context.md](../intent/beat_execution_context.md) §3.2 に注記済み)。既存小粒ノードの掃除 (19 identity + 17 batch、本番記憶書き換え=まはー承認必須) と重複 user 行掃除は実装と別枠
- **完了条件**: 記憶監査・SEA 監査の Metabolism 系 finding が全消し込み + 体験の構造 §4 の圧縮七原則が生成経路の回帰で固定される ✔ / **差し戻し後**: chronicle_eviction 実装 + 同 intent §9 の検証 (a)〜(d) が実機通過

### W5 ☑ 実行台帳 Phase 5 — 配送系と移動 — 実装済み・実機検証待ち (2026-07-21)

- **スコープ**: **S5 の完了化** (perception flush の append 戻り値 None 未検査 — §6-4 で outbox 化・例外時保持は済み、残るは None の静かな失敗経路) / M8 = Building→個人記憶の転記 cursor 先行確定 / B1 = 移動 outbox / **W3 委譲: ライフ境界通知の outbox 化** (マーカーと world-DB 単一 commit)。**S3 は §6-4 で先取り済み**
- **確定設計 (Fable 裁定)**: [走行メモ](../handoff/2026-07-21_w5_delivery_ledger_handoff.md)。S5 = flush の成功条件を「message id 取得」に (`_append_message` が例外を握って None を返す静かな失敗経路を検査)。M8 = cursor を「連続消費の最大 seq」に後行確定 + 失敗で即停止 + `building_msg_ref` provenance で「append 成功→marker 失敗」の limbo (停止規律により常に高々 1 件 = 次ラウンド最初の転記候補) を冪等修復。副産物の実バグ = auto_ingest 経路の `_mark_ingested` が contextvar 経由で manager を引けず **DB の ingested_by が一度も永続化されていなかった** (cursor 先行と相殺して隠れていた) → manager 明示渡しで根治。境界通知 = kind `life.boundary_{start,end}` の台帳実行 — claim → 冪等段 → 「マーカー + applied + 通知 outbox」を `mutate_plan_meta` の `in_session_extra` で単一 commit (W3 第六陣の「マーク失敗の無条件 True + 即時リトライ」暫定を撤去)。B1 = `move.entity` 台帳実行 — 位置遷移 + leave/enter イベント (`insert_building_message_in_session` 内核抽出で移動 tx に同居) + applied + 後処理 outbox (dynamic state / addon hooks / game lifecycle の 3 target) を単一 commit、**commit 後は False を返さない**。死んだ `db_session` 口を削除
- **実装済み (2026-07-21)**: 上記 4 点 + `add_to_persona_only` の成否契約 (`memory_first` = 保存失敗時は in-memory にも積まない)。回帰 = S5 2 件 / M8 7 件 (append 失敗・mark 失敗・DB lock・再起動で欠落も重複もなし) / 境界 3 件 + W3 期テスト 2 件を新契約に書換 / B1 8 件 (単一 commit・全巻き戻し・後処理再配送・None キュー・縮退)。**Codex レビュー 1 巡 4 件消し込み (受諾 4 / 却下 0)**: P1×2 (`move.post_dynamic_state`/`move.post_game_lifecycle` ハンドラが誘発する再帰的 `flush_pending_for_persona` が非再入 `_delivery_lock` でデッドロック — 同一原因、`ExecutionLedger` に再入検知を追加し一括解消) / P2×2 (`on_entity_moved`/`on_building_entered` の内部失敗が outbox に伝播しない → 両関数 bool 化 + ハンドラ側で失敗を再試行対象に / Building 転記 provenance 照会の例外を「未登録」に倒すと重複保存しうる → 照会失敗はラウンド停止に変更)。回帰追加 4 件、走行メモ = [`2026-07-21_w5_delivery_ledger_handoff.md`](../handoff/2026-07-21_w5_delivery_ledger_handoff.md)。**スコープ境界**: 監査修正方針の「persona/user 属性更新の移動 service への集約」は明文契約 (`persona.current_building_id` は呼び出し側責務) と 5 呼び出し箇所に跨るため **W7 柱5 の canonical 化と同時に実施** (走行メモに記録 — 「commit 後 False」の根絶で分裂の実害は W5 で閉じる)。**残 = まはー実機検証** (移動イベントの同時確定・境界通知の一度きり配送・Building 転記の再試行)
- **完了条件**: SEA 監査 S5・記憶監査 Building転記の消し込み ✔

### W6 ☑ head の fail-closed 化 (S6) — 実装済み・実機検証待ち (2026-07-21)

- **スコープ**: head capture/render/persist 失敗時に LLM を実行しない (required Section の readiness 検証、None Section を欠損と認識、store の commit 成否返却)。人格に属さない発話が本人履歴に混ざる経路を塞ぐ
- **参照**: [SEA 監査](../handoff/2026-07-15_sea_runtime_session_head_tail_audit.md) S6 / [走行メモ](../handoff/2026-07-21_w6_head_fail_closed_handoff.md)
- **実装済み (2026-07-21)**: required Section (`required=True`: common_prompt / persona_self / core_memory) の capture 失敗 (既存値なし=欠損、`capture_failures` 記帳) / render 失敗 / persist 未確認で `HeadNotReadyError` → prepare_context が LLM 実行前に Pulse 中断 (会話=正直なエラー、判断点/コマ=台帳 failed 行)。自己修復 = `ensure_snapshot` の**欠損限定** `recapture_missing` + `ensure_persisted` の再保存 (「durable 版 >= 描画版」の単調性)。store.save 成否 bool 化 + 版条件付き UPDATE。core_memory の内部握り撤去。Codex レビュー 5 巡 (受諾 9 / 裁定却下 1 — 明細は走行メモ)。回帰 = test_head_fail_closed.py 27 件、本体スイート全緑。**残 = まはー実機検証** (通常運転の無変化 / memory.db ロック時に人格なし応答でなくエラーになること / rebasing・stale save ログが平常時に出続けないこと)
- **完了条件**: required Section 失敗で LLM 不実行 + 復旧後再試行の回帰固定 ✔

### W7 ☑ 柱5 — 位置・占有 — 実装済み・実機検証待ち (2026-07-21)

- **スコープ**: 単一 City 内の移動原子性 / occupancy 一意性 / chat 境界 / Region / City 変更 (Persona/City/Building 監査の非凍結残)
- **参照**: [分離監査](../handoff/2026-07-15_persona_city_building_separation_audit.md) / [走行メモ](../handoff/2026-07-21_w7_location_occupancy_handoff.md)
- **実装済み (2026-07-21)**: 分離監査の非凍結 finding 全消し込み — **P1-2** (active occupancy の部分一意 index `uq_occupancy_active_ai` + 起動時重複修復 + move_entity の書き込み時仲裁 = close の条件付き UPDATE / 新行 guarded INSERT / user 位置の条件付き UPDATE) / **P1-1残・W5委譲** (persona 属性 + cursor 儀式 + user state の更新を `move_entity._sync_canonical_location` へ集約、呼び出し側 6 箇所の重複更新撤去、公開は配送前) / **P1-3** (`/chat/send` 現在地専用化 409 + runtime 多層防御 + **発言永続化 tx 内の現在地検証** `insert_building_message_with_location_guard` + utter 意味論の正直化 + 拒否時の添付 Item cleanup と撤去補記) / **P1-6** (Region parent 変更の入口同一 tx 同期 + 入口の直接再所属拒否 + 入口所有一意 index) / **P1-7** (Building CITYID の immutable 化 + UI セレクタ disabled) / **P2-1** (event_key を台帳 execution_id 採番に) / **P2-2** (startup checker 分類化 = 重複修復 [参照整合優先]・無効行 close・派遣中/capacity 警告・pre-start 修復の監査引き継ぎ)。**Codex レビュー 9 巡 18 件消し込み (受諾 18 / 却下 0、明細は走行メモ)**。回帰 = test_location_occupancy_w7.py + test_chat_boundary_w7.py 計 47 件 (新設) + test_region_admin.py に 9 件追加 + スタブ 6 箇所の新契約化。ruff / tsc clean。**残 = まはー実機検証** (通常移動・utter の無変化 / 二重 presence の不在 / 起動時修復ログ)
- **完了条件**: 同監査の非凍結 finding 全消し込み ✔

### W8 ▶ 柱6 — 時刻 — S7 実装済み・実機検証待ち / 残 = A3 (2026-07-22)

- **スコープ**: S7 (秒精度 timestamp による anchor 境界・履歴順の破れ → thread 内単調 sequence を正典順序キーに) + 監査横断の時刻系 finding の棚卸し
- **S7 実装済み (2026-07-22)**: 正典順序キー = `(created_at, rowid)` (created_at が意味時刻でインポート履歴を尊重、rowid は同一秒内の挿入順 tie-breaker、NULL created_at は先頭側)。anchor 境界 (`get_messages_from_id`) をキーセット化、履歴・pagination・Chronicle・周辺窓・範囲クリップ・表示系 API の全 messages クエリを同一 total order に統一。native export/import は `seq` (元 rowid) を運び、往復でスレッド横断の同秒交互順序まで保存 (空きがあれば明示 rowid で完全復元・衝突時は追記フォールバック)。Codex レビュー 4 巡 (受諾 3 / スコープ外 1)。回帰 = test_time_order_canonical_w8.py 18 件 (新設) + test_native_import_separation.py に 4 件追加、本体スイート全緑。走行メモ = [`2026-07-22_w8_time_order_handoff.md`](../handoff/2026-07-22_w8_time_order_handoff.md)
- **棚卸し結果 (2026-07-22)**: 時刻系 finding のうち S7=本 wave 消し込み / occupancy event_key 同秒衝突=W7 済 / backup 名ミリ秒衝突=第二陣済 (backup.py の uuid suffix 裏取り) / 深夜帯コマ・day_close 半開区間=消し込み済み。**残 = A3 (host/City 時差、自律行動監査 P1)** — 未修正を裏取り (autonomy_wiring に timezone 処理なし)。host=City 同一 TZ の現行構成では潜伏。共通 clock helper + persona-local 比較 + EventScheduler 直前変換の設計から必要 — **A3 の着手時期 (W8 継続 or 分離 wave) はまはー裁定待ち**
- **完了条件**: 同一秒衝突の回帰固定 (anchor 境界 / pagination) ✔ + A3 の裁定

### W9 ☐ 柱7 — 完全手動モード

- **スコープ**: 「行動を生む」仕事だけを止める gate の一貫化 (回復ジョブの二分は台帳側で確定済み — その適用徹底)
- **完了条件**: 手動モードで自律系が完全停止し、掃除系が止まらないことの回帰固定

### W10 ☐ 柱8 — 独立小物

- **スコープ**: S9 (token trigger が件数 gate で拒否される) / Spell 監査残 (realtime spell の SPELL_ENABLED 迂回・auto_mode 固定 / `_` 予約 namespace / 入力 contract)。着手時に柱8 の全量を棚卸しして確定
- **完了条件**: 柱8 リストの全消し込み

### W11 ☐ §6-6b — Beat ロックの実行トークン化

- **スコープ**: beat_gate の threading.local 深度 → persona別トークンスタック。**着手前に全子 acquire 経路の伝播マップ必須** (伝播漏れ=デッドロック新設)。現状の劣化は軽微 (レガシー分岐で META 待ち最大1 Pulse) なので優先度は柱の後ろ
- **参照**: [handoff §6](../handoff/2026-07-17_audit_wave_session2_handoff.md) に調査材料一式
- **完了条件**: running-loop レガシー分岐でも boundary が有効 + デッドロック回帰なし

### W13 ☑ 体験の構造 工程(3) — 継承エッジの器 (監査外) — 実装済み・実機検証待ち (2026-07-22)

- **スコープ**: 継承 DAG のテーブル + 範囲オープン時の機械的記帳 ([experience_structure.md](../intent/experience_structure.md) §3.3)。分岐・再生成 / メティス取り込みの前提。他 wave と独立 — 空きに挟んでよい
- **実装済み (2026-07-22)**: `episode_inheritance` テーブル (database/models.py — 子→親 0..n、`LAYER` = fact(事実層)/digest(咀嚼層)、`ANCHOR_REF` = 分岐点の pulse 関節メッセージ、`ORIGIN`、UNIQUE(子,親,層) で冪等) + 操作モジュール `saiverse/experience_inheritance.py` (record_edges / get_parents / get_children / get_ancestors)。`open_episode(predecessors=[...])` が範囲オープンの同一 tx (session= 予約 tx にも相乗り) で機械的に記帳 (§11-4) — 選択なし = エッジ 0 本 = 直列の縮退で既存挙動は無変化。migration = `ensure_episode_inheritance_table` 軽量シンク (main.py 起動時、既存 DB はテーブル追加のみで既存行に触れず)。回帰 = `tests/test_experience_inheritance.py` 21 件 (両層記帳・anchor 付き分岐・並列統合の二親・祖先 BFS・冪等/自己ループ禁止/層検証・予約 tx 原子性・既存データ無害) 全緑、既存 episode スイート 63 件無変化、ruff clean。**残 = まはー実機検証** (通常運転の無変化 = エッジ生成なし) + 消費者配線 (継承チェーン閉じ生成・分岐再生成 UI・メティス取り込み) は後続 wave
- **完了条件**: 事実層/咀嚼層エッジの記帳が回帰固定、既存データはエッジなしで無害 ✔

### W14 ☐ 体験の構造 工程(4) — 知覚レンダリング (監査外)

- **スコープ**: [perception_buffer.md](../intent/perception_buffer.md) 後続 Phase として。experience_structure §7 の原則 (翻訳のみ・直挿し段階的廃止) に従う。詳細設計はこの wave 着手時
- **完了条件**: event_message 直挿しの段階的廃止が始まり、通知洪水が観測面で解消

### W12 ☐ 仕上げ (常に最終)

- **スコープ**: gen_reference_docs 一括再実行 / 解決済み issue の archive 移動 / レビュー台帳の全行を最終照合して状態更新 / in_flight から監査系の行を退役 / memory 更新
- **完了条件**: レビュー台帳の全行が「回帰固定済み以上 or 保留裁定記録済み or 凍結」

### 横断 ▶ 実機検証 (まはー)

- **実施中 (2026-07-17〜)**: ライフ一日検証 — §6-2 Beat ロック (会話+自律併走) / §6-3 キー化 / §6-4 内容型通知 (記憶操作→tail に render 断片) / §6-5 退役ゲート (見送り WARN の誤発動がないか)
- **待機中**: 第二陣の実機導線確認 / 各 wave 完了後の随時検証
- 実機で問題が出たら該当 wave に差し戻し行を立てる

---

## 完了済みの記録 (2026-07-17 時点)

| 項目 | 内容 | 完了日 |
|---|---|---|
| 一次監査 全8サブシステム | 記憶境界 / migration / 自律行動 / SEA runtime / Spell権限 / City分離 / API / 外部連携 | 2026-07-16 |
| 8柱の裁定 | 残存 P1×30前後/P2×7 を柱に整理、全柱方針確定 | 2026-07-16 |
| 第二陣 hardening | migration/API/外部連携の共通境界 (回帰2419+34件)。残=外部の署名鍵 publish (外部待ち) | 2026-07-16 |
| 柱3: multi-city 凍結封鎖 | API 503 + polling 不起動 + 入口ガード | 2026-07-16 |
| 柱4: native import 分離 | 復元/移植の分離 + 原子化 (M4/M5/M6) | 2026-07-17 |
| 実行台帳 Phase 0 | 器 + 状態機械 + FIFO 配送器 + 関所 + 回復骨格 + manager 結線 + 実ハンドラ2種 + Beat 関所 | 2026-07-17 |
| **柱2: 統合工事 §6 (§6-6b 除く)** | §6-1 ExecutionContext / §6-2 Beat ロック+関所+main/META解体 / §6-3a anchor 行分離+実model記帳 (**S1/S8**) / §6-3b head snapshot キー化 (+MODEL_KEY 全行'default'の実バグ発見) / §6-4 内容型通知 (**S3/S5入口** + issue head_mutation_notification_gap) / §6-5 Metabolism 二層分離 (**S2/M1** + 記憶監査第4片 = TTL失効後の旧anchor touch) / §6-6a thread push/pop (**S4**) / §6-7 正典改訂。コミット 77e81e6 / 4c64bbe / 30ebaf7 / 48f3421 / a4a2bba / 9a02ecc / e645792。スイート 2474→2591 passed | 2026-07-17 |

---

## 運用ルール

1. **メティスは監査系の作業をしたセッションの終わりに、この計画書の wave 状態と「現在地」を必ず更新する** (レビュー台帳の finding 更新と同じコミットで)
2. wave の状態遷移: 着手で ▶、検収コミットで ☑、実機検証通過で ✅
3. wave 内で新 finding や設計課題が出たら、この計画書に行を足すのではなく issue / intent に置き、該当 wave のスコープ行から参照する (この文書は薄く保つ)
4. handoff 文書 (docs/handoff/日付_*.md) はセッション固有の走行メモ (走行中エージェントの検収手順など)。セッションを跨ぐ工程情報は書かず、ここへ書く
