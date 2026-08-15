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

## 現在地 (2026-08-16 更新 — 棚卸し: 7月末以降の設計世代交代と突き合わせ、残作業を畳み直した)

```
一次監査     ████████ 完了 (全8サブシステム、2026-07-16)
柱の裁定     ████████ 完了 (8柱すべて方針確定、2026-07-16)
基盤工事     ████████ 完了 (実行台帳 Phase 0 + 統合工事 §6 ※§6-6bのみ分離)
実装 wave    ███████▶ W1〜W8 + W10 + W13 実装済み / W4 は検証を世代交代先へ移管 / W9 は v3 待ち凍結 / 残 = W11・W14 + A3 裁定 + W12
実機検証     ███░░░░░ 正常系は統合検証手順 (2026-08-07) に合流し Step 1〜2 の大半は消化済み / 故障注入系は別枠未実施
```

**棚卸しの結果 (2026-08-16)** — 約3週間の設計世代交代 (時間割の抜本改修 / あらすじのレベル制 / Track 撤廃 順序① / 編纂入口の一本化) と突き合わせた:

- **実機検証の本線は [統合検証手順 (2026-08-07)](../handoff/2026-08-07_timetable_live_verification_run.md) へ合流済み**。同手順が「実行台帳 W1/W2/W5 の正常系 + 横断ライフ一日」を守備範囲として明記し、W7 の起動時修復ログ (Step 1) や W6 の rebasing / stale save 静穏 (Step 3 横断観察) も拾う。詳細は「横断 実機検証」節。
- **W4 の実機検証は後継行へ移管して閉じる**。[あらすじのレベル制](../intent/arasuji_levels.md) が chronicle_consolidation / chronicle_eviction の圧縮規則を置き換える正典であることを自ら宣言しており、実機検証もそちらの行 (エリス合格済み・残 = aifi 再編纂とレベル1束ねの初発火観察) と [applier_veto_deadlock issue](../issues/chronicle_eviction_applier_veto_deadlock.md) が持つ。W4 固有に検証する対象はもう無い。
- **W9 (柱7 完全手動モード) は v3 待ち凍結**。自律行動の運転は [autonomous_behavior_v3](../intent/autonomous_behavior_v3.md) で再設計中 (運転の v0.4 分離裁定 2026-08-01) で、v2 配線に gate を新設しても土台ごと入れ替わる。A10 の finding は消えていない — 凍結であって解決ではない。
- **S9 (柱8) は患部消滅を裏取りして消し込み** (レビュー台帳 SEA 行 2026-08-16)。Metabolism の発火判定が字数三水位へ世代交代し、指摘対象の件数差分 gate が現行コードに存在しない。
- **A3 (host/City 時差) は残存を再裏取り**。autonomy_wiring.py に timezone 処理なしのまま (2026-08-16 grep)。host=City 同一 TZ の現行構成では潜伏。着手裁定待ちは変わらず。

**次にやる wave**: **W10 は 2026-08-16 実装完了** (Spell 監査残 3 件消し込み + auto_mode 常時 False の実バグ根治。明細は W10 行とレビュー台帳 Spell 行)。実働の残りは **A3 の着手裁定 (まはー)** と W11・W14。全 wave 決着後に **W12 (レビュー台帳の全行最終照合)** で監査を閉じる。実機検証は統合検証手順の走行の続き (Step 3 一日の走行〜) が本線。

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

### W4 ☑ 実行台帳 Phase 4 — Metabolism 残片 = 体験の構造 工程(2) と統合 — 実装済み (2026-07-27)・検証は世代交代先へ移管して閉じた (2026-08-16 棚卸し)

- **棚卸し (2026-08-16)**: 本 wave の実機検証は独立には行わない — 圧縮規則の正典が [あらすじのレベル制](../intent/arasuji_levels.md) へ世代交代し (同 intent 冒頭が chronicle_consolidation / chronicle_eviction の置き換えを宣言)、実機検証もそちら (2026-07-29 エリスで §11-4 合格、残 = aifi 再編纂とレベル1束ね初発火の観察) と [applier_veto_deadlock issue](../issues/chronicle_eviction_applier_veto_deadlock.md) の行が真実を持つ。以下の記録は経緯として保存

- **スコープ**: M2 (Chronicle 生成の残る原子性課題)。**S2/M1 は統合工事 §6-5 で先取り済み** — 差分を調査してから着手 (残量は小さい可能性が高い)
- **統合裁定 (2026-07-19)**: Chronicle 生成は [体験の構造](../intent/experience_structure.md) 工程(2) で episode 整列 (サイズ+列のあふれ束ね・バッチ降格・恒等圧縮) に世代交代する。**旧バッチ生成経路に hardening を入れない** (捨てる経路を固めるのは二度手間) — 新設経路を最初から原子的に作り、M2 はそこで消化する
- **確定設計 (Fable 裁定)**: [走行メモ](../handoff/2026-07-21_w4_metabolism_ledger_handoff.md)。M2 の残片を 5 欠陥 (M2-a 親子別 commit の二重生成 / M2-b 提示コンテキスト内圧縮 §4-1 違反 / M2-c digest 再圧縮 §4-4 違反 / M2-d source 重複無防備 / M2-e dismantle 複合更新) に具体化し、新設経路で消化
- **実装済み (2026-07-21)**: 整列計画 `alignment.py` (純関数、見積もりと生成の一点管理) + チャンク実行 `executor.py` (チャンク単一 tx + 重複再検査 + 由来メタ) + 列のあふれ束ね `bands.py` (親子単一 tx = M2-a 根治、壁、帰化バックフィル)。D1 退場時圧縮 (evict boundary = M2-b) / D2 退役の episode スナップ (open episode の内部で切らない) / D5 session_digest 材料除外 (M2-c) / D8 Track Chronicle 生成廃止 (§11-10) / D9 API・CLI・estimate・frontend 載せ替え + API 生成ジョブに M1 claim 結線 (旧: claim 素通りの別コネクション入口)。旧経路 (ArasujiGenerator / maybe_consolidate / gap-fill / dismantle 経路) は削除。**Codex レビュー 5 巡 20 件消し込み (受諾 20 / 却下 0、10→6→3→1→0 で対象単調縮小、明細は走行メモ)**: tx 内再検査の BEGIN IMMEDIATE 原子化 / 束ね子検査の同 / dry 予測と backfill の順序 (backfill を計画前へ) / 安全弁の試行数カウント / claim を claim_execution+try_mark_running へ (failed キー退避 = キャンセル・失敗後の同窓即時再試行) / backfill 全体の単一 tx 化 (lost update 閉塞) ほか。回帰=alignment 20 / executor 10 / bands 18 / metabolism 18 計 66 件 + gold_panning 35 全緑、本体スイート全緑、ruff clean。issue 消し込み: general_chronicle_metabolism_trigger (D1 で解決)・chronicle_generation_dual_pipeline (5/28 解決の移動漏れ) を archive へ。**残 = まはー実機検証** (episode 転写の恒等性・列のあふれ束ねの初回帰化・open episode スナップの観測)
- **実機検証で出た欠陥 (2026-07-24)**: エリスの Chronicle 一覧に単独発言のエントリが並ぶというまはーの観察から調査。**一次あらすじが標準被覆 U=1 万字に対し実測 9〜6,344 字に崩れている** — 自動経路の evict 境界 (退場の刻み幅) は U と無関係に数件ずつ動くのに、`_plan_run` は run 末尾で `_flush_pending()` を無条件に確定するため、「まだ相手が退場していない端数」が「束ねる相手がいない豆粒」(§4-3 恒等圧縮) と同一視される。§4-4 (同一レベルの再圧縮禁止) により後から束ね直せない。二次以上のあらすじ 側の `bands.py` `_select_bundle_run` には「目標未達の列は束ねず持ち越す」規律があるのに**一次あらすじだけ持ち越しの器が無い**という非対称 → [`chronicle_undersized_lv1_chunks`](../issues/chronicle_undersized_lv1_chunks.md) (解消は下の差し戻し行へ)。なお発端の 2 エントリ自体は W4 の欠陥ではなく、W5/M8 で根治済みの `ingested_by` 非永続化バグが 7/18 に作った重複 user 行を、W4 の集合ベース未処理判定が**正しく**拾った結果 → [`chronicle_orphan_duplicate_user_messages`](../issues/chronicle_orphan_duplicate_user_messages.md) (データ掃除のみ残)
- **差し戻し (2026-07-24→25): 退場設計の世代交代 = [chronicle_eviction.md](../intent/chronicle_eviction.md) 実装 — 実装待ち**。当初の借用案 (一次あらすじ端数を提示中の生ログで最小限巻き込んで U を満たす) は Codex レビューで **open episode 分断の P1** が出て撤回。まはーとの設計対話で退場の粒度そのものを変える上流の解に到達し、intent は **2026-07-25 レビュー通過・設計確定**。スコープ = ①退場を時系列一本線→ **episode 単位** (open は単独 digest・closed 同士は束ねる) ②水位をメッセージ数→ **文字数三水位** (既定 低4万/目標6万/高12万、全モデル一律・モデルファイルでユーザー設定可) ③ experience_structure §6 **pulse 関節細分**の実装 (open episode の部分圧縮) ④ digest の**時系列位置置き換えレンダリング** + 圧縮マーク注釈 ⑤借用実装 (alignment / session_lifecycle / テスト、未コミット) の撤回。**W4 検証項目の読み替え**: evict boundary (D1) と open episode 丸ごと回避スナップ (D2) はこの実装で世代交代するため旧実装のまま検証しない。恒等転写・列のあふれ束ね (bands)・チャンク単一 tx (M2-a/c/d/e) は現行のまま有効。**上書きされる裁定**: 柱2 §6-5 の「Metabolism 閾値はモデル依存」→ 全モデル一律・文字数三水位 ([beat_execution_context.md](../intent/beat_execution_context.md) §3.2 に注記済み)。既存小粒ノードの掃除 (19 identity + 17 batch、本番記憶書き換え=まはー承認必須) と重複 user 行掃除は実装と別枠
- **差し戻しの消化 (2026-07-25→27)**: **下段 (生ログ → 一次あらすじ) = 実装済み** ([chronicle_eviction.md](../intent/chronicle_eviction.md)、`e8c061d` + `3fa2711` + `53448ef`、Codex 3 巡 + サブエージェント 1 巡消し込み)。**上段 (一次 → 二次以上の束ねと提示) = 実装済み** ([chronicle_consolidation.md](../intent/chronicle_consolidation.md) v0.2、`f19553f`) — 束ねの発火を「未束ね字数 > 提示予算/4」に、選抜を質量ベース (比率 10 倍以内 + 卒業 = 合算 ≥ 5×最大) に世代交代し、束ね不能ノードは治療 (隣人への合流) で回収。提示粒度は累積質量ルールへ。Codex レビュー 5 巡で P1×9 消し込み、bands 33 件 + 全体 3,222 passed。**残 = まはー実機検証**
- **実装レビューで発掘した本設計以前からの欠陥 (消化状況は各項に付記)**: [`chronicle_eviction_applier_veto_deadlock`](../issues/chronicle_eviction_applier_veto_deadlock.md) (P1 — 適用側の拒否権で anchor が恒久的に詰まる) **2026-07-27 実装完了** (`fadd6de`。顔その1=編纂対象ゼロ fold を吸収限定退場へ / 顔その2=あらすじ手動削除の道連れ + 恒久欠落記録の撤去。残 = まはー実機検証 = 通常運転の Metabolism が退行していないこと) / ~~[`chronicle_run_boundary_lost_by_excluded_tag`](../issues/archive/chronicle_run_boundary_lost_by_excluded_tag.md) (P1 — 除外タグ 1 件で run 境界が消え §4-5 の偽の隣接が起きる)~~ **2026-07-27 解決** (境界を fold の先頭 id でなく所属 fold で持つ。この修正への攻撃レビューで [`fold_range_and_chronicle_entry_not_one_to_one`](../issues/fold_range_and_chronicle_entry_not_one_to_one.md) (P2 — 退場する範囲とあらすじの範囲の一対一が強制されていない) を新たに発掘・issue 化) / [`chronicle_split_episode_digest_double_description`](../issues/chronicle_split_episode_digest_double_description.md) (まはーへの再説明待ち)。W4 の完了条件には含めず、独立 issue として消化する
- **完了条件**: 記憶監査・SEA 監査の Metabolism 系 finding が全消し込み + 体験の構造 §4 の圧縮七原則が生成経路の回帰で固定される ✔ / **差し戻し後**: chronicle_eviction 実装 ✔ + chronicle_consolidation 実装 ✔ + 両 intent §9 / §10 の検証が実機通過 (残)

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

### W9 💤 柱7 — 完全手動モード — v3 待ち凍結 (2026-08-16 棚卸し)

- **スコープ**: 「行動を生む」仕事だけを止める gate の一貫化 (回復ジョブの二分は台帳側で確定済み — その適用徹底)
- **凍結理由 (2026-08-16)**: 自律行動の運転そのものが [autonomous_behavior_v3](../intent/autonomous_behavior_v3.md) で再設計中 (運転の v0.4 分離裁定 2026-08-01)。v2 配線への gate 新設は土台ごと入れ替わるため、v3 の運転設計が確定した時点でその一部として再定義する。**A10 (完全手動モード gate) の finding は未解決のまま** — 凍結であって解決ではない。v3 側の設計時に本 wave のスコープ (行動系/掃除系の二分) を持ち込むこと
- **完了条件**: 手動モードで自律系が完全停止し、掃除系が止まらないことの回帰固定

### W10 ☑ 柱8 — 独立小物 — 実装済み・実機検証待ち (2026-08-16)

- **スコープ**: Spell 監査残 (realtime spell の SPELL_ENABLED 迂回・auto_mode 固定 / `_` 予約 namespace / 入力 contract)
- **棚卸し (2026-08-16)**: 旧スコープの S9 (token trigger が件数 gate で拒否される) は**患部消滅で消し込み済み** — Metabolism 発火判定が [あらすじのレベル制](../intent/arasuji_levels.md) の字数三水位へ世代交代し、件数差分 gate が現行コードに存在しない (レビュー台帳 SEA 行に記録)
- **実装済み (2026-08-16)**: ①realtime spell を `_spell_enabled` gate の内側へ + **auto_mode の正直化** (`auto_mode=True` を渡す箇所がリポジトリ全体に不在という実バグを発見 — 自律 Pulse のスペルが常に「ユーザー起動」として確認ダイアログ/auto フィルタに伝わっていた。root で pulse_type から導出し `state["_auto_mode"]` 単調 OR で運搬) / ②`_` 予約 namespace を PlaybookSchema validator で fail-closed 拒否 (全ロードが通る関所) + merge 3点の実行時防御。既存 builtin 21+DB 27 は違反ゼロを事前確認 / ③入力契約 = 提供値の型正規化 + 変換不能・enum 外値の正直な失敗。**required 欠落のみ warn-only** (既存 52/94 パラメータが依存、完全強制は playbook データ棚卸しが前提 — W10 裁定)。**意図的保留**: `check_spell_permission(aspect=None)` fail-open は Track 撤廃で解体予定のゲートのため据え置き ([track_retirement](../intent/track_retirement.md) スコープへ)。回帰 = 新規 30 件。明細はレビュー台帳の Spell 行
- **Codex レビュー 1 巡 (2026-08-16)**: 判定 No-ship — **high 1 (schedule の ask_every_time 事前承認が auto 拒否に食われる確認済み回帰) + medium 4**、全件受諾。明細と裁定は [走行メモ](../handoff/2026-08-16_w10_spell_audit_remnants_handoff.md)。**消し込みは Opus セッション** (Fable 1 巡規律)。⚠️ F1 修正まで schedule から ask_every_time Playbook を起動する自動化は停止する
- **完了条件**: 柱8 リストの全消し込み (残 = Codex 指摘 5 件の消し込み [Opus] → まはー実機検証: SPELL_ENABLED=false のペルソナで realtime spell が走らないこと / 自律 Pulse のスペルで確認ダイアログが出ずログに reason="auto" が出ること / schedule の事前承認が生きていること / 通常会話・判断点の無変化)

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
- **棚卸し (2026-08-16)**: 知覚消費点の Beat 頭化 + フィード入室配送 (2026-08-08、[issue](../issues/feed_arrival_pulse_cannot_see_articles.md)) が知覚の**消費側**を先に前進させた。着手時はこの現状を前提に引き直す
- **完了条件**: event_message 直挿しの段階的廃止が始まり、通知洪水が観測面で解消

### W12 ☐ 仕上げ (常に最終)

- **スコープ**: gen_reference_docs 一括再実行 / 解決済み issue の archive 移動 / レビュー台帳の全行を最終照合して状態更新 / in_flight から監査系の行を退役 / memory 更新
- **完了条件**: レビュー台帳の全行が「回帰固定済み以上 or 保留裁定記録済み or 凍結」

### 横断 ▶ 実機検証 (まはー) — 本線は統合検証手順に合流 (2026-08-16 棚卸し)

- **本線 = [統合検証手順 (2026-08-07)](../handoff/2026-08-07_timetable_live_verification_run.md)**: 同手順が「実行台帳 W1/W2/W5 の正常系 + 横断ライフ一日」を守備範囲として明記 (Step 1, 3)。W7 の起動時修復ログは Step 1、W6 の rebasing / stale save 静穏と判断二重発火なしは Step 3 の横断観察が拾う。Step 1〜2 は 2026-08-08 に大半消化済み、**残り = Step 3 (一日の走行) 〜 Step 5**
- **受け身の観察で足りるもの** (専用手順なし、日常運転で乱れが出たら差し戻し): W8 S7 (履歴・提示順の同一秒乱れ) / W13 (継承エッジ — 通常運転でエッジ生成なしの無変化) / 統合工事 §6-2〜§6-5 (Beat ロック併走・キー化・内容型通知・退役ゲート誤発動なし)
- **故障注入系は別枠未実施**: W2 crash 後の settle-close / W3 dispatch 失敗時の oneshot 非消失 / W6 memory.db ロック時の正直なエラー / 席のロック競合 — 統合検証手順の「この一巡に載せないもの」に明記されたとおり、**正常系の一日が通ってから別枠で実施**
- **待機中**: 第二陣の実機導線確認
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

## 経緯: コードレビュー運用・残存finding修正 (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

**一次監査は全8サブシステム完了(2026-07-16)**。
migration/API/外部連携は第二陣まで消し込み済み、Spell監査もP1×3+P2×1修正済(P1×1部分修正)。
**残存=実質P1×30前後/P2×7を8本の柱に整理(2026-07-16)**: 柱1=偽成功・不可逆先行16件(→実行台帳、別行)/柱2=model別Session(**裁定済: フルキー化=実装を正典に追いつかせる→統合工事、別行**)/柱3=multi-city生死/柱4=native import意味論/柱5=位置占有/柱6=時刻/柱7=完全手動モード/柱8=独立小物。
**副産物: head操作通知の現行の抜けを発見・issue化**([head_mutation_notification_gap](../issues/head_mutation_notification_gap.md)、METAの生きる目的書き換えが内容不達)。
**柱3=裁定済(multi-city凍結、入口封鎖タスク化)/柱4=裁定済(復元/移植の分離)** — 8柱すべて方針確定(2026-07-16)。
**柱3封鎖=実装済(2026-07-16)**: inter-city/persona-proxy API 503+凍結メッセージ・VisitingAI/ThinkingRequest polling 不起動・dispatch_persona/return_visiting_persona 入口ガード・回帰テスト(tests/test_multi_city_freeze.py 6件)・docs 同期(sds/landscape §8/roadmap/inter-city/CLAUDE.md)。
**柱4分離=実装済(2026-07-17)**: native import の復元/移植分離 — 復元は source/target 不一致を書き込み前拒否(API 400/CLI 即エラー)・移植は明示フラグ+原子写像+transplanted_from provenance・replace 単一トランザクション化・embedding 非生成化(M4/M5/M6 消し込み、回帰 tests/test_native_import_separation.py 10件)。
**柱2=統合工事§6で完結(2026-07-17)**。
**以降の工程管理は [完了計画書](audit_remediation_plan.md) に一本化 (W1〜W14)** — セッション開始はまず計画書の「現在地」から。
**W1(台帳Phase 1判断点=A2/A7/A8/A9/A11 + digest統合)=実装済・実機検証待ち(2026-07-19)**。
**W2(台帳Phase 2時間割/予算=A1/A5/A6)=実装済・実機検証待ち(2026-07-20)**: Codexレビュー7巡(5+2+2+2+1+1+1件)を全て修正・回帰固定 — 第三陣=予約keyのslot id化+二重claimの勝者一意化、第四陣=発火照準のid一貫化+置換のid新世代化、第五陣=slots書き込みの世代CAS化、第六陣=meta_jsonもCAS対象に、第七陣=増分計算をCASの内側へ(mutate_plan_meta新設、並走積算の消失閉塞)。
本体スイート2749 passed。
**W3(台帳Phase 3 schedule=A12/A13)=実装済・実機検証待ち(2026-07-21)**: 発火claim(`schedule.dispatch`台帳実行=claim→席取り→型付きoutcome→精算単一commit、failed=backoff×3・unknown=自動再実行禁止) + 世代列`SYNC_GENERATION`+行一生`INSTANCE_TOKEN`+回復tick #7 reconciliation(60秒自己回復)+API `scheduler_synced`。
Codexレビュー10巡27件消し込み(受諾24/裁定却下3 — キー三軸化/tri-state応答/両境界の冪等マーカー+失敗伝播/periodicのprepared・failed回収/精算フェンス/applied sweep/世代のサーバー側インクリメント)。
**W4(台帳Phase 4 Metabolism残片=体験の構造 工程(2)に統合)=実装済・実機検証待ち(2026-07-21)**: Chronicle生成をepisode整列チャンク(alignment/executor/bands)へ世代交代し M2残片5欠陥(親子別commit二重生成/提示コンテキスト内圧縮/digest再圧縮/source重複/dismantle複合更新)を消化+Track Chronicle生成廃止+API生成ジョブへM1 claim結線。
Codexレビュー5巡20件消し込み(受諾20/却下0)。
圧縮七原則を回帰固定(新規66件)。
**W5(台帳Phase 5 配送系と移動)=実装済・実機検証待ち(2026-07-21)**: S5完了化(perception flushのNone静的失敗でpendingを消さない)+M8(Building転記cursorの後行確定+provenance冪等、副産物=auto_ingestのingested_by非永続化バグ根治)+ライフ境界通知のoutbox化(W3第六陣恒久解: マーカーと通知を単一commit)+B1(move_entity台帳化: 位置遷移+イベント+後処理outboxを単一commit、commit後にFalseを返さない)。
**Codexレビュー1巡4件消し込み(受諾4/却下0)**: P1×2=後処理outboxハンドラが誘発する再帰的flush_pending_for_personaが非再入ロックでデッドロック(実装直後に発見、再入検知で解消)、P2×2=post-move失敗のoutbox未伝播+provenance照会失敗の重複保存リスク。
回帰計24件。
**W6(head fail-closed化=S6)=実装済・実機検証待ち(2026-07-21)**: required Section(common_prompt/persona_self/core_memory)のcapture/render/persist失敗時は`HeadNotReadyError`でLLM実行前にPulse中断(人格に属さない発話が本人履歴に混ざる経路の封鎖)、自己修復=欠損限定recapture+persist再試行(durable版>=描画版の単調性)、store.save成否bool化+版条件付きUPDATE、core_memoryの内部握り撤去。
Codexレビュー5巡(受諾9/裁定却下1)。
回帰=test_head_fail_closed.py 27件。
**W7(柱5 位置・占有)=実装済・実機検証待ち(2026-07-21)**: 分離監査の非凍結finding全消し込み — occupancy部分一意index+起動時修復(参照整合行優先)+move_entityの書き込み時仲裁(条件付きUPDATE/guarded INSERT)+属性更新のservice集約(呼び出し側6箇所撤去)+chat境界(/send現在地専用409+発言永続化tx内の現在地検証+添付cleanup+撤去補記)+Region入口不変条件(parent変更同期・直接再所属拒否・所有一意index)+Building CITYID immutable化+event_key台帳採番+startup checker分類化。
Codexレビュー9巡18件消し込み(受諾18/却下0)。
回帰=新規47件+既存ファイルに9件追加。
**W8(柱6 時刻)のS7=実装済・実機検証待ち(2026-07-22)**: messagesの正典順序キーを`(created_at, rowid)`に確定し、anchor境界キーセット化+履歴/pagination/Chronicle/提示コンテキスト/表示系の全クエリ統一+native export/importの往復順序保存(seq運搬+明示rowid復元)。
Codexレビュー4巡(受諾3/スコープ外1=前セッションGemini作業への指摘、申し送り済)。
回帰=新規18件+native import系4件追加。
時刻系findingの棚卸し完了 — 残=A3(host/City時差、未実装・潜伏)の着手裁定。
**W4の実機検証で欠陥1件が出た(2026-07-24)**: エリスのChronicle一覧に単独発言のエントリが並んでいるというまはーの観察から調査 — (a)一次あらすじが標準被覆U=1万字に対し実測9〜6,344字に崩れており、退場の刻み幅がUより細かいとき「まだ相手が退場していない端数」を「束ねる相手がいない豆粒」と同一視して確定してしまう(二次以上のあらすじ側の`bands.py`には持ち越し規律があるのに一次あらすじだけ無い非対称)→[chronicle_undersized_lv1_chunks](../issues/chronicle_undersized_lv1_chunks.md)、(b)発端の2エントリ自体はW4の欠陥ではなく、W5/M8で根治済みの`ingested_by`非永続化バグが7/18に作った重複user行をW4の集合ベース未処理判定が正しく拾った結果(データ掃除のみ残)→[chronicle_orphan_duplicate_user_messages](../issues/chronicle_orphan_duplicate_user_messages.md)。
**(a)は再設計→設計確定(2026-07-25)**: 当初の借用案(一次あらすじ端数を提示中の生ログで最小限巻き込む)はCodexレビューで**open episode分断のP1**が出て撤回。
まはーとの対話で退場の粒度を**episode単位・文字数水位**に変える上流の解へ到達し、Intent [chronicle_eviction.md](../intent/chronicle_eviction.md) が**まはーレビュー通過→2026-07-25 実装完了・実機検証待ち**(open単独digest/closed束ね・三水位 低4万/目標6万/高12万・直近の保護範囲・pulse関節細分§6・digest置き換えレンダリング+圧縮マーク注釈)。
**実装の形**: 借用実装は撤回済み。
新規 `sea/eviction_plan.py`(退場計画=純関数)/`sea/session_window.py`(圧縮区間と digest 置き換え、`SessionWindow`=anchor+生ログ+提示)、`session_anchor.FOLDED_RANGES_JSON`(圧縮区間は(persona,model)の持ち物=退役がmodelごとだから)、編纂は`generate_chronicle(compile_groups=...)`で**退場する集合と編纂する集合を一致**させ下限を手続き保証、head の Chronicle 枠からは inline 表示中のあらすじを除外(二重化防止)。
旧メッセージ数水位は**単位ごと廃止**(モデルJSON 120件・API `/max-history-messages`・manager override・フロントUI。
landscape §9 に記録)。
回帰: `tests/test_eviction_plan.py`(13件)+`tests/test_session_window_folds.py`(12件)+`test_metabolism_two_layer.py`書き直し、本体スイート3193 passed。
**レビュー=Codexクレジット切れのためClaudeサブエージェントで代行・2巡14件消し込み**(受諾14/却下0。
P1=①圧縮区間の先頭が提示コンテキスト外で体験消失 ②digestを引けない圧縮区間の無限積み上がり、P2=③ロック外の提示コンテキストでRMW ④子episode open/closeの非原子 ⑤force-closeの歯止め ⑥置き換え本文の未計上 ⑦anchorが退場経路外で動くと提示コンテキストにもheadにも出ない体験ができる ⑧disabledで子episodeだけ増殖。
明細は[レビュー台帳](code_review_ledger.md)のP0記憶行)。
**監査工程上はW4の差し戻し行として一本化**(完了計画書に記載): W4検証項目のうちevict boundary/openスナップは世代交代・恒等転写/列のあふれ束ね/M2-a/c/d/eは有効のまま、柱2 §6-5「Metabolism閾値はモデル依存」裁定は三水位で上書き(beat_execution_context §3.2注記済)。
**🔴 ただし上段(二次以上のあらすじ)との接続が未設計と判明(2026-07-25 まはー指摘→調査で確定)**: 本実装が扱うのは「生ログ→一次あらすじ」まで。
一次あらすじを束ねる既存機構(`bands.py`)と噛み合っておらず、①束ねの連続判定は上の次数の壁しか見ないため**まだあらすじになっていない生ログの期間を跨いで前後が同じ二次あらすじに束ねられうる**(§4-5 偽の隣接=時系列の嘘。
提示コンテキストの途中を畳めるようにした本実装が束ね側の前提を壊した) ②圧縮区間を列の上限の勘定にどう置くか未定義(headに出していないのに列の上限を圧迫) ③圧縮区間の置き換えが Chronicle 側の束ねと連動していない(旧記述「上へ畳む道が無い」は §4-4 の限定を落とした誤読 → 2026-07-25 訂正。
道は最初からあり、壊れているのは提示側が一次あらすじの id を名指しで握っていること=②と同根)。
加えて**圧縮区間の記録が更新頻度の違う二箇所(提示コンテキスト=毎回/head抑止=節目で凍結)で共有**されており、キャッシュ切れ経路でその範囲が提示コンテキストにもheadにも出ない状態が生まれく(レビューP2-Aの対処が別のズレを作った形)。
**まはー判定=二次以上のあらすじの畳み方を丸ごと再考、調査と案出しから。
用語の作り直しは実施済み(圧縮区間→圧縮区間 / 級→一次・二次あらすじ / 提示コンテキスト→提示コンテキスト / 帯→用語廃止)**(短い常用漢字を用語にすると地の文で判別不能・説明の分かりにくさの一因)。
**2026-07-25 後半 = 用語改名 + 退場の判断基準を全面改訂**: ①用語を作り直し (穴→圧縮区間 / 級→一次・二次あらすじ / 窓→提示コンテキスト / 帯→用語廃止。
`level` は DB の metadata キーなので改名せず)。
②§4-4「同一レベルの再圧縮禁止」の本文から限定が落ちていたのを修復 — この誤読が「圧縮区間を上へ畳む道が無い」という**存在しない行き止まり**を作っていた。
③**未解決③(置き換えの溜まり)は議題から外した** — 算術で残骸は畳んだ量の12%、目標水位を埋めるには生ログ50万字ぶん必要=急ぎでない。
④**退場の判断基準を改訂 (まはー裁定)**: U は「優先度」の材料であって「畳んでいいか」の材料ではない。
計画を二段構えにし、一段目 (U 以上のみ) で目標に届かなければ**二段目で U 未満の open も一回だけ畳む**。
先頭に U 未満の端数が居座ると anchor が永久に進まないのが病巣で、後続を圧縮しても置き換えが積もるだけだった。
代償 (小粒の一次あらすじ / 進行中会話の要約 / 極小の子 episode) は受容 — anchor 停止の方が重篤。
**強制クローズ(旧§5-5)は撤去** (本来は episode 側のタイムアウト検知の仕事、landscape §9 へ)。
⑤実装中に**既存の欠陥を1件発見・修正**: 畳み済み unit を透明に素通りさせていたため、前後の closed がそれを飛び越えて束ねられた (§4-5 偽の隣接。
U 以上を畳み切った時にも成立していた)。
⑥**新 issue**: 分裂した episode の digest が同じ出来事を二回記述する ([chronicle_split_episode_digest_double_description](../issues/chronicle_split_episode_digest_double_description.md)、まはー「別セッションでまた訊く」)。
⑦自動想起 (会話 episode に戻ったとき要約済み部分を想起する仕組み) は**別途組む・未着手**。
**コードレビュー消し込み (Codex 3巡 + Claudeサブエージェント1巡)**: Codex=①anchor恒久停止の境界ケース(手前が未吸収なら端数を畳んでも進まず、置き換えが壁になって恒久デッドロック) ②壁修正が大きいopenの続きを畳めなくする回帰 ③修正が別の恒久デッドロックを新造(最後の手段をopen限定にしたため「先頭がU未満closed+直後がU未満open」で計画が永久に空。
→**種類を問わず未畳みの先頭を畳む**へ) ④二段構えでfoldsの時系列順が崩れ子episodeの記録順が逆転。
Codexは4巡目でクレジット上限(OpenAI障害と同時)→サブエージェントへ交代。
サブエージェント=93,312通り総当たりで**計画側の契約違反・偽の隣接・無限ループ0件**を確認したうえで、⑤最後の手段がanchor非停滞の回にも発火(手前を今回畳んでいても前提が真になる→**日常経路で小粒ノードを作り観測フラグも死ぬ**。
修正済) + **本変更以前から在る欠陥2件を発見→issue化のみ**: [applier_veto_deadlock](../issues/chronicle_eviction_applier_veto_deadlock.md)(適用側の拒否権2つを計画側が知らず永久ループ。
強制クローズでも救えなかった経路) / [run_boundary_lost_by_excluded_tag](../issues/archive/chronicle_run_boundary_lost_by_excluded_tag.md)(編纂のrun境界がfold先頭id依存で除外タグ1件で消え、離れたfoldが一つのあらすじに=§4-5偽の隣接。
**2026-07-27解決**=境界を先頭idでなく所属foldで持つ形へ交代し、先頭が編纂対象から落ちても境界が立つ。
純関数側と実DB側の両方に「先頭が居ない群」の回帰を追加。
Codex攻撃レビュー4巡で契約違反時の縮退も固め=所属不明(重複/未所属)は孤立させ束ねない・群の連続性の検算は提示コンテキストの完全な並びを持つ退場計画側(`compile_groups_from_folds`)へ配置。
**残った構造課題は新issue** [fold_range_and_chronicle_entry_not_one_to_one](../issues/fold_range_and_chronicle_entry_not_one_to_one.md)(P2=検算した分割が退場適用へ伝播しない/`_attach_chronicle_refs`が包含でなく重なりでentryを付ける。
現行`plan_eviction`は連続foldしか作らないので今日の経路では未発生))。
教訓=**歯止めの条件は目的から導く。
種類で書くと「達成しない発火」と「達成できるのに発火しない穴」の両方が開く**(同一変更で2回踏んだ)。
回帰=本体3251 passed・ruff clean。
**残=まはー実機検証** — 当時の残件は二つとも消化済み: ①W4 発掘の P1 = applier_veto_deadlock は **2026-07-27 実装完了** (`fadd6de`。
検証待ちは[専用行](../issues/chronicle_eviction_applier_veto_deadlock.md)が持つ) ②上段(二次以上のあらすじ)の偽の隣接と二箇所のズレも同日 chronicle_consolidation で実装完了 (`f19553f`) → **2026-07-28 に[あらすじのレベル制](../intent/arasuji_levels.md)へ世代交代**(そちらの専用行が真実を持つ)。
よって退場側 intent §9 (a)〜(d) の実機検証を止めていた条件も解けている。
走行メモ=[2026-07-25](../handoff/2026-07-25_chronicle_eviction_handoff.md)。
W1〜W3/W5〜W8の実機検証は退場経路と独立なので並行消化可。
aifi/air会話本線異常(eris42通vs aifi10/air14通)はチップtask_a3a22c08に分離
**2026-08-16 棚卸し**: 最終更新(2026-07-27)から約3週間の設計世代交代(時間割の抜本改修/あらすじのレベル制/Track撤廃 順序①/編纂入口の一本化)と突き合わせ、計画書を畳み直した — ①W4の実機検証を後継行(arasuji_levels+applier_veto_deadlock)へ移管して閉鎖 ②W9をv3待ち凍結(A10は未解決のまま持ち込み) ③S9は患部消滅を裏取りして消し込み(発火判定が字数三水位へ世代交代、件数差分gateが現行コードに不存在) ④A3残存を再裏取り(autonomy_wiringにtimezone処理なし) ⑤実機検証の正常系が統合検証手順(2026-08-07)へ合流済みであることを明文化し、故障注入系を別枠残として区別。旧「現在地」(2026-07-27版)は差し替え — 記載事実は全て各wave行と完了記録表が保持しているため移送不要。in_flight台帳の旧行文面(移送): 「一次監査完了、残存 finding は 8 本の柱に整理し工程管理は完了計画書 (W1〜W14) に一本化 — セッション開始は計画書の「現在地」から。W1〜W8 実装済み、W4 差し戻し分 (Chronicle 退場・レベル制) は専用行が真実を持つ。次 = W1〜W8 の実機検証 (退場経路と独立に並行消化可)・W9 (柱7 完全手動モード) 着手・A3 (host/City 時差) の着手裁定。」
**同日 W10 (柱8) 実装完了**: Spell 監査残 3 件 (realtime spell gate / `_` 予約 namespace / 入力契約) を消し込み。副産物で auto_mode 常時 False の実バグ (自律 Pulse のスペルが常にユーザー起動扱い) を発見・根治。required 欠落の warn-only と aspect=None fail-open の Track 撤廃送りは W10 行とレビュー台帳 Spell 行に裁定として記録。
