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

## 現在地 (2026-07-17 更新)

```
一次監査     ████████ 完了 (全8サブシステム、2026-07-16)
柱の裁定     ████████ 完了 (8柱すべて方針確定、2026-07-16)
基盤工事     ████████ 完了 (実行台帳 Phase 0 + 統合工事 §6 ※§6-6bのみ分離)
実装 wave    ██░░░░░░ W1〜W12 のうち 0 完了 (これから)
実機検証     █░░░░░░░ ライフ一日検証を実施中 (まはー、2026-07-17〜)
```

**済んだ大物** (詳細は §「完了済みの記録」):
- 柱2 (model 別 Session) = 統合工事 §6-1〜§6-7 で**完結** — S1/S2/S3/S4/S8 + M1 + 記憶監査第4片を根治
- 柱3 (multi-city) = 凍結・入口封鎖済み / 柱4 (native import) = 復元/移植分離済み
- 実行台帳 Phase 0 (器 + Beat ロック + 関所 + 配送ハンドラ) = 完了・休眠解除済み

**次にやる wave**: W1 (台帳 Phase 1 = 判断点 5種)。

---

## 工程表 (wave 一覧)

順序は依存関係順。各 wave は「調査 (Explore) → 設計 (メイン) → 実装 (委譲) → 検収 (メイン) → コミット」の型で回す。**状態**: ☐ 未着手 / ▶ 進行中 / ☑ 実装済み (実機検証待ち) / ✅ 完了。

### W1 ☐ 実行台帳 Phase 1 — 判断点 (柱1 の核)

- **スコープ**: 5種 finalize と on_event 入口を実行台帳に載せる。A2 (day_open/close 同日重複 → `(kind, persona:営業日)` UNIQUE) / A7 (メタ判断例外→空成功・イベント消失 → 成功=finalize 完了の永続証跡、on_event は prepared が durable queue) / A8 (finalize 保存失敗→成功扱い・二重適用 → 世界更新=applied + 判断行=outbox、副作用は execution_id で冪等) / A9 (post-session の completed 先行 → task 完了+artifact ref の単一トランザクション化) / A11 (spell 失敗を committed 成功で記録 → 失敗はシステム名義の失敗行)
- **同時に確定**: intent §11 の小物4点 (RESULT_JSON 標準列 / prepared 回収規則・期限の kind 別既定値ほか — Phase 1 実装時確定でまはー了承済み)
- **同工区で実施 (2026-07-18 裁定)**: post_session×digest 統合 — digest 専用コール廃止、状況文にセッション原本、digest は post_session の出力欄に (詳細は [judgment_points.md](../intent/persona_cognition/judgment_points.md) §6 冒頭の改定決定)。A9 と同じ finalize 経路を触るため同時に
- **参照**: [execution_ledger.md](../intent/execution_ledger.md) §7 / [自律行動監査](../handoff/2026-07-14_autonomy_judgment_schedule_audit.md)
- **完了条件**: A2/A7/A8/A9/A11 が回帰固定済み、レビュー台帳の自律行動行を消し込み更新

### W2 ☐ 実行台帳 Phase 2 — 時間割と予算

- **スコープ**: コマ実行の予約→精算→Episode close (A5, A6) と day_open 全置換 (A1)
- **完了条件**: 同上の要領で回帰固定 + 台帳消し込み

### W3 ☐ 実行台帳 Phase 3 — schedule

- **スコープ**: 発火 claim と reconciliation (A12, A13)
- **完了条件**: 同上

### W4 ☐ 実行台帳 Phase 4 — Metabolism 残片

- **スコープ**: M2 (Chronicle 生成の残る原子性課題)。**S2/M1 は統合工事 §6-5 で先取り済み** — 差分を調査してから着手 (残量は小さい可能性が高い)
- **完了条件**: 記憶監査・SEA 監査の Metabolism 系 finding が全消し込み

### W5 ☐ 実行台帳 Phase 5 — 配送系と移動

- **スコープ**: **S5 の完了化** (perception flush の append 戻り値 None 未検査 — §6-4 で outbox 化・例外時保持は済み、残るは None の静かな失敗経路) / M8 = Building→個人記憶の転記 cursor 先行確定 / B1 = 移動 outbox。**S3 は §6-4 で先取り済み**
- **完了条件**: SEA 監査 S5・記憶監査 Building転記の消し込み

### W6 ☐ head の fail-closed 化 (S6)

- **スコープ**: head capture/render/persist 失敗時に LLM を実行しない (required Section の readiness 検証、None Section を欠損と認識、store の commit 成否返却)。人格に属さない発話が本人履歴に混ざる経路を塞ぐ
- **参照**: [SEA 監査](../handoff/2026-07-15_sea_runtime_session_head_tail_audit.md) S6
- **完了条件**: required Section 失敗で LLM 不実行 + 復旧後再試行の回帰固定

### W7 ☐ 柱5 — 位置・占有

- **スコープ**: 単一 City 内の移動原子性 / occupancy 一意性 / chat 境界 / Region / City 変更 (Persona/City/Building 監査の非凍結残)
- **参照**: [分離監査](../handoff/2026-07-15_persona_city_building_separation_audit.md)
- **完了条件**: 同監査の非凍結 finding 全消し込み

### W8 ☐ 柱6 — 時刻

- **スコープ**: S7 (秒精度 timestamp による anchor 境界・履歴順の破れ → thread 内単調 sequence を正典順序キーに) + 監査横断の時刻系 finding の棚卸し
- **完了条件**: 同一秒衝突の回帰固定 (anchor 境界 / pagination)

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

### W12 ☐ 仕上げ

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
