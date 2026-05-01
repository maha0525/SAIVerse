# Intent: ペルソナ認知モデル

**ステータス**: 整理中 (旧 `persona_cognitive_model.md` v0.14 + `persona_action_tracks.md` v0.11 を再構造化中)
**親 Intent**: なし (本セットが上位概念)
**最終更新**: 2026-05-01

---

## これは何か

ペルソナが**複数の進行中「行動の線」(Track) を単一主体として動かす**認知モデルと、その実装機構を扱う Intent ドキュメント群。SAIVerse の自律稼働・応答待ち・並列タスク・割り込み処理・ペルソナ再会のすべてが、このモデルの上に立脚する。

旧 2 ドキュメント (`persona_cognitive_model.md` 1155 行 + `persona_action_tracks.md` 2106 行) は v0.1〜v0.14 の改訂差分が積み重なって読みづらくなったため、**確定仕様 / Phase 計画 / 改訂履歴**を分離する形で再構造化した。

旧ドキュメントは整理完了まで残置 (Phase 計画完遂時にリダイレクト stub 化予定)。

---

## ドキュメント構造

```
docs/intent/persona_cognition/
├── README.md              ← このファイル (全体俯瞰 + 進捗表)
├── 01_concepts.md         ← 用語定義・不変条件・認知モデルの中身
├── 02_mechanics.md        ← メタ判断 A/B フロー、Pulse 階層、再開コンテキスト
├── 03_data_model.md       ← テーブルスキーマ、マイグレーション
├── 04_handlers.md         ← Track 種別ごとの Handler / Playbook 設計方針
├── phases/
│   ├── phase_1_base.md            ← 基盤刷新 (handoff 解消 + データモデル拡張)
│   ├── phase_2_track_metalayer.md ← Track / MetaLayer / Handler 基盤
│   ├── phase_3_lines_playbooks.md ← ライン仕様 + Track 種別 Playbook
│   ├── phase_4_pulse_scheduler.md ← Pulse 階層 + Scheduler + メタ定期判断
│   ├── phase_5_autonomy.md        ← 自律稼働の本格化
│   └── phase_6_extensions.md      ← 拡張機構 (Stelis 統合・モニタリング等)
└── revisions.md           ← v0.1〜v0.14 の改訂履歴 (差分情報の集約所)
```

### どれを読めば何が分かるか

| 目的 | 読むべきファイル |
|------|----------------|
| 「Track / Line / Note って何?」の用語確認 | `01_concepts.md` |
| 「メタ判断はどう動くか」の仕組み | `02_mechanics.md` |
| DB スキーマ / カラム追加 / マイグレーション | `03_data_model.md` |
| 新しい Track 種別を追加する時の Handler 書き方 | `04_handlers.md` |
| 「今どこまで実装済みで、次に何をやるか」 | このファイル下の **進捗表** + `phases/*.md` |
| 「v0.X でなぜこの仕様に変わったか」 | `revisions.md` |

---

## Phase の切り方 (重要)

認知モデルの実装は **Phase 1 → 2 → 3 → 4 → 5 → 6** の線的順序で積み上げる。各 Phase は前の Phase の成果物を前提にする。下の Phase が完全に終わるのを待たずに、上の Phase に着手することは可能 (例: Phase 3 が 60% で Phase 4 に着手していい)。

```
[Phase 1] 基盤刷新                      ✅ 完了
   handoff 解消 + データモデル拡張 (テーブル + カラム + 7層ストレージ基礎)
   ↓
[Phase 2] Track / MetaLayer / Handler 基盤   ✅ ほぼ完了
   action_tracks / notes / track_handlers / track_* ツール群
   ↓
[Phase 3] ライン仕様 + Track 種別 Playbook   🟡 約 60%
   line: main/sub フィールド + 各 Track 種別の Playbook 整備
   ↓
[Phase 4] Pulse 階層 + Scheduler + メタ定期判断  🟡 約 40%
   MainLineScheduler / SubLineScheduler / on_periodic_tick
   ↓
[Phase 5] 自律稼働の本格化                🔲 未着手
   Handler tick / 内部 alert / Track パラメータ / Schedule 統合
   ↓
[Phase 6] 拡張機構                       🔲 構想
   Stelis 統合 / モニタリングライン / 創発 Track / Note 同期
```

### 旧 Phase 番号からの移行マップ

旧ドキュメントには **Phase 0 / Phase C-1〜C-3 / Phase 1.1〜1.4 / v0.4** が混在していた。本ディレクトリでは以下のように集約する:

| 旧称 | 新 Phase | 備考 |
|------|---------|------|
| Phase 0 (handoff 解消) | Phase 1 | P0-1〜P0-7 |
| Phase 1.3 (scope='discardable'/'committed') | Phase 1 | messages テーブル拡張の一部 |
| Phase C-1 (MetaLayer / Track 基盤) | Phase 2 | |
| Phase 1.1 (Pulse-root context + Handler.track_specific_guidance) | Phase 2 | Handler 雛形整備の一部 |
| Phase C-2 (line / context_profile DEPRECATED) | Phase 3 | |
| Phase 1.2 (meta_judgment.json 経由パス) | Phase 3 | meta_judgment Playbook の整備 |
| Phase 1.4 (context_profile / model_type DEPRECATED) | Phase 3 | |
| Phase C-3 (Pulse スケジューラ / 定期実行) | Phase 4 | |
| Phase B-X (social_track_handler 雛形) | Phase 2 | Phase 2 の一部として既に取り込み済み |
| 旧 Intent B v0.7「Handler tick / 内部 alert」 | Phase 5 | |
| 旧 Intent B v0.7「Track パラメータ機構」 | Phase 5 | |
| 旧 Intent B v0.7「ScheduleManager 段階移行」 | Phase 5〜6 | Phase 5 で並走、v0.4.0 で完全移行 (= Phase 6) |
| 旧 v0.4 以降「Stelis 統合」 | Phase 6 | |
| 旧「モニタリングライン (v0.3.0 Phase 4)」 | Phase 6 | unified_memory_architecture の Phase 4 とは別 |
| 旧「創発 Track の生成」 | Phase 6 | |

---

## Phase 進捗表

凡例: ✅ 完了 / 🟡 進行中 / 🔲 未着手 / ⛔ ブロック中

### Phase 1 — 基盤刷新 (✅ 完了)

handoff 3 経路問題の解消 + 7 層ストレージモデルを支えるデータモデル拡張。本 Phase が以降すべての前提。

| ID | タスク | 状態 | 実装場所 | 旧称 |
|----|--------|------|---------|------|
| 1-1 | `PulseContext._line_stack` / `LineFrame` / push/pop/current_line | ✅ | `sea/pulse_context.py:56-224` | P0-1 |
| 1-2 | `messages` テーブル拡張 (line_role / line_id / scope / paired_action_text) | ✅ | `sai_memory/memory/storage.py:101-129` | P0-2、Phase 1.3 |
| 1-3 | `meta_judgment_log` / `track_local_logs` テーブル新設 | ✅ | `database/models.py:512-580` | P0-3 |
| 1-4 | Spell loop の `tags=["conversation"]` 固定廃止 | ✅ | `sea/runtime_llm.py:434-465` | P0-4 |
| 1-5 | `speak: false` 時に `_emit_say` skip | ✅ | `sea/runtime_llm.py:977-983` | P0-5 |
| 1-6 | action 文ペア保存 (`paired_action_text` 利用) | ✅ | `sea/runtime_llm.py:1733-1766` | P0-6 |
| 1-7 | `include_internal` フィルタを line_role / scope ベースへ移行 | ✅ | (Phase 1.4 で DEPRECATED 化済み、削除は Phase 3 完了後) | P0-7 |
| 1-8 | scope 昇格 SQL UPDATE 機構 (`discardable` → `committed`) | ✅ | (Phase 1.3 マージ済み) | Phase 1.3 |

**詳細**: `phases/phase_1_base.md`

---

### Phase 2 — Track / MetaLayer / Handler 基盤 (✅ ほぼ完了)

action_tracks / notes テーブル + alert ベースのメタレイヤー + Handler パターン基盤。Phase 3〜5 の足場。

| 項目 | 状態 | 実装場所 / 備考 | 旧称 |
|------|------|----------------|------|
| `MetaLayer` クラス (alert observer + Playbook ディスパッチ) | ✅ | `saiverse/meta_layer.py` | C-1 |
| `track_handlers/user_conversation_handler.py` | ✅ | `saiverse/track_handlers/user_conversation_handler.py` | C-1 |
| `track_handlers/social_track_handler.py` | ✅ | `saiverse/track_handlers/social_track_handler.py` | B-X |
| `track_handlers/autonomous_track_handler.py` | ✅ | `saiverse/track_handlers/autonomous_track_handler.py` | C-1 |
| `action_tracks` テーブル | ✅ | `database/models.py:395` | C-1 |
| `notes` / `note_pages` / `note_messages` / `track_open_notes` テーブル | ✅ | `database/models.py:436-506` | C-1 |
| `track_*` ツール群 (create/activate/pause/wait/resume/complete/abort/forget/recall/list) | ✅ | `builtin_data/tools/track_*.py` | C-1 |
| `AI.ACTIVITY_STATE` カラム | ✅ | `database/models.py:56` | C-1 |
| `AI.SLEEP_ON_CACHE_EXPIRE` カラム | ✅ | `database/models.py:59` | C-1 |
| Pulse-root context 構築機構 (`pulse_root_context.py`) | ✅ | (Phase 1.1 マージ済み) | Phase 1.1 |
| Handler に `track_specific_guidance` 属性追加 | ✅ | `track_handlers/*` | Phase 1.1 |
| `AI.current_active_track_id` カラム | 🔲 | 運用上は不影響だが計画上は予定あり | C-1 残件 |

**詳細**: `phases/phase_2_track_metalayer.md`

---

### Phase 3 — ライン仕様 + Track 種別 Playbook (🟡 約 60%)

旧 `context_profile` / `model_type` を `line: "main"|"sub"` 指定に集約。Track 種別ごとの専用 Playbook を整備。

| 項目 | 状態 | 実装場所 / 備考 | 旧称 |
|------|------|----------------|------|
| `SubPlayNodeDef.line` フィールド (`"main"|"sub"`) | ✅ | `sea/playbook_models.py:287-297` | C-2 |
| ライン runtime (親 messages のコピー分岐 + report_to_parent append) | ✅ | `sea/runtime_nodes.py` | C-2 |
| `meta_judgment.json` Playbook | ✅ | `builtin_data/playbooks/public/` | Phase 1.2 |
| `meta_judgment_dispatch.py` 経由パス | ✅ | (Phase 1.2 マージ済み) | Phase 1.2 |
| `track_user_conversation.json` Playbook | ✅ | `builtin_data/playbooks/public/` | C-2 |
| `track_autonomous.json` Playbook | ✅ | `builtin_data/playbooks/public/` | C-2 |
| `LLMNodeDef.context_profile` DEPRECATED 化 | ✅ | `sea/playbook_models.py:48-55` | Phase 1.4 |
| `LLMNodeDef.model_type` DEPRECATED 化 | ✅ | `sea/playbook_models.py:57-62` | Phase 1.4 |
| `track_social.json` Playbook | 🔲 | 未着手 | C-2 残件 |
| `track_external.json` Playbook | 🔲 | 未着手 | C-2 残件 |
| `track_waiting.json` Playbook | 🔲 | 未着手 | C-2 残件 |
| `report_to_parent` 必須バリデーション (`can_run_as_child=true` 用) | 🟡 | runtime ルーティングは実装、厳密化は警告ログのみ | C-2 残件 |
| `exclude_pulse_id` 廃止 | 🔲 | 旧仕様コードは現存 | C-2 残件 |
| Phase 3 翻訳前段の Playbook 整理 (旧プロトタイプ削除 + Spell 化) | ✅ | DB 67 → 48 件、`run_meta_auto` 関数削除、`ConversationManager` no-op 化 (v0.19, 2026-05-01) | Phase 3 整理 |
| 既存 Playbook の `context_profile` → `line` 翻訳 (`migrate_playbooks_to_lines.py`) | 🔲 | 未着手 (整理後の対象 Playbook 数を再カウント要) | C-2 残件 |
| `context_profile` / `model_type` / `exclude_pulse_id` の完全削除 | 🔲 | 全 Playbook 翻訳後 | C-2 残件 |

**詳細**: `phases/phase_3_lines_playbooks.md`

---

### Phase 4 — Pulse 階層 + Scheduler + メタ定期判断 (🟡 約 40%)

メインライン Pulse / サブライン Pulse の 2 階層分離 + 各 Scheduler 実装 + メタレイヤーの定期実行入口。

| 項目 | 状態 | 実装場所 / 備考 | 旧称 |
|------|------|----------------|------|
| Handler に `pulse_completion_notice` / `post_complete_behavior` 属性 | ✅ | `track_handlers/social_track_handler.py:48`, `autonomous_track_handler.py:43` | C-3 |
| Handler に `default_pulse_interval` / `default_max_consecutive_pulses` / `default_subline_pulse_interval` | ✅ | `autonomous_track_handler.py:44-46` | C-3 |
| `SubLineScheduler` クラス | ✅ | `saiverse/pulse_scheduler.py:76-127` | C-3b |
| `MainLineScheduler` クラス | 🔲 | コメント `Phase C-3c で別途実装予定` (`pulse_scheduler.py:18`) | C-3c |
| `MetaLayer.on_periodic_tick` (定期実行入口) | 🔲 | 未着手 | C-3 |
| `MetaLayer` の per-persona 直列化 Lock (`on_track_alert` / `on_periodic_tick`) | ✅ | `saiverse/meta_layer.py:__init__`, `_get_lock` (v0.16, 2026-04-30) | handoff Part 1 |
| `meta_judgment_log` スキーマ v0.15 整合化 + 書き込み + 動的注入 | ✅ | `database/models.py`, `saiverse/meta_layer.py`, `sea/runtime_graph.py`, `sea/runtime_llm.py`, `sea/pulse_context.py` (v0.16, 2026-04-30) | handoff Part 2 |
| SAIMemory `messages.pulse_id` カラム化 (Phase 2.5) | ✅ | `sai_memory/memory/storage.py`, `saiverse_memory/adapter.py`, `sea/runtime.py` (v0.17, 2026-05-01) | Phase 2.5 |
| 自律先制と外部 alert のレース解消 (Phase 2.6) | ✅ | `saiverse/track_manager.py`, `saiverse/meta_layer.py`, `meta_judgment.json` (v0.18, 2026-05-01) | Phase 2.6 |
| `AutonomyManager` の `MainLineScheduler` への移管 | 🔲 | 旧 `autonomy_manager.py` は現存 (レガシー残置) | C-3c |
| 環境別デフォルト値の自動推定 (Pattern A/B/C) | 🔲 | 未着手 | C-3 |
| 7 制御点の実装場所明確化 (action_tracks.metadata + 環境変数 + Handler 属性 + モデル設定) | 🔲 | 部分的に Handler 属性のみ実装、残りは未着手 | C-3 |

**詳細**: `phases/phase_4_pulse_scheduler.md`

---

### Phase 5 — 自律稼働の本格化 (🔲 未着手)

Handler tick による内部 alert + Track パラメータ機構 + ScheduleManager の Track 化。「ペルソナが自分の意思で動く」を技術的に支える層。

| 項目 | 状態 | 旧称 |
|------|------|------|
| Handler `tick()` メソッド機構 (`SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS`) | 🔲 | Intent B v0.7 |
| 内部 alert ポーラ機構 (Handler tick 内で `set_alert` 発火) | 🔲 | Intent B v0.7 |
| Track パラメータ機構 (`metadata.parameters` 連続値、メタ判断時に注入) | 🔲 | Intent B v0.7 |
| `track_parameter_set` ツール (ペルソナ自身による明示更新) | 🔲 | Intent B v0.7 |
| `SomaticHandler` 雛形 (空腹度等の身体的欲求 Track) | 🔲 | Intent B v0.7 |
| `ScheduledHandler` 雛形 (スケジュール起因 Track) | 🔲 | Intent B v0.7 |
| `PerceptualHandler` 雛形 (SNS 経過時間等の知覚起因 Track) | 🔲 | Intent B v0.7 |
| 既存 ScheduleManager の Track metadata.schedules 形式への並走対応 | 🔲 | Intent B v0.7 |
| ペルソナ再会機能の汎用化 (Person Note 自動開封 + alert 化に統合) | 🔲 | C-1 後半相当 |

**詳細**: `phases/phase_5_autonomy.md`

---

### Phase 6 — 拡張機構 (🔲 構想)

Stelis 統合 / モニタリングライン / Note 同期 / 創発 Track。本格的な v0.4.0 以降の中核。

| 項目 | 状態 | 旧称 |
|------|------|------|
| Stelis スレッドの新基盤統合 | 🔲 | v0.4 |
| ScheduleManager の完全廃止 (ScheduledHandler 移行完了) | 🔲 | v0.4 |
| モニタリングライン本格実装 (カメラ / X タイムライン等) | 🔲 | v0.3.0 Phase 4 |
| 完全独立 worker 系コンテキスト (新基盤上で再実装) | 🔲 | C-2 スコープ外 |
| Project Note → Vocation Note ノウハウ転記 | 🔲 | 構想 |
| Note のペルソナ間共有・同期 | 🔲 | 構想 |
| 重量級モデルの「判断時詳細 + 記憶時簡略」プロンプト | 🔲 | 効率化案 |
| 創発 Track の生成 | 🔲 | 高難度長期課題 |
| Track 越境参照機構 (`track_local_logs.visible_to_other_tracks` 運用) | 🔲 | Intent B v0.11 |

**詳細**: `phases/phase_6_extensions.md`

---

## 進捗表の更新ルール

- 実装着手時に該当行を 🔲 → 🟡 に変更
- マージ時に 🟡 → ✅、`実装場所` カラムにファイルパス記入
- 仕様変更で項目追加・削除は `revisions.md` に経緯を残してから本表を変更
- 進捗が止まっている場合は `⛔ <ブロック理由>` を「状態」カラムに記入
- 旧称マッピングは「移行マップ」セクションを更新

---

## 守るべき不変条件 (要約、詳細は `01_concepts.md`)

1. **同時実行しない** — アクティブ Track は常に 1 本
2. **単一主体の記憶** — Track が違っても記憶は単一空間
3. **メタレイヤーが切り替えを独占** — Playbook 内では切り替えない
4. **Track 永続化** — プロセス再起動を跨いで失われない
5. **古い Track の忘却** — 完全削除はしない
6. **メタレイヤーは恒常的に存在** — ランタイムレベル常駐
7. **キャッシュヒット継続を最優先** — Track 切り替えごとのキャッシュ破棄は許容しない
8. **軽量 / 重量級モデルの使い分け** — 2 本のキャッシュを並列維持
9. **他者との会話は重量級モデル** — 軽量での外向き発話は禁止
10. **Metabolism 機構を活用** — 新規キャッシュ管理層を作らない
11. **メタ判断はペルソナの自分の思考** — 別人格扱いしない
12. **親-子ラインの寿命関係** — 子は親の中で完結

---

## 関連ドキュメント

- [handoff_2026-05-01.md](handoff_2026-05-01.md) — **次セッション用** (Phase 3 既存 Playbook 翻訳)
- [handoff_2026-04-30.md](handoff_2026-04-30.md) — 前回 handoff (Phase 2 / 2.5 / 2.6 完了済み)
- `unified_memory_architecture.md` v3 — v0.3.0 中心軸 (pulse_logs / Chronicle / Memopedia / 自律稼働バイオリズム)
- `dynamic_state_sync.md` — Metabolism 機構と A/B/C 状態モデル (本セットが活用する基盤)
- `kitchen.md` — Kitchen 通知が「Track への切り替え要求」に該当
- `mcp_protocol_coverage.md` — Elicitation / Cancellation がこのモデルに依存
- `stelis_thread.md` — Stelis スレッドの設計 (当面別物として共存、Phase 6 で統合)
- `handoff_track_context_management.md` — Phase 1 のもとになった handoff 観察記録
- (旧) `persona_cognitive_model.md` v0.14 — 整理完了まで残置
- (旧) `persona_action_tracks.md` v0.11 — 整理完了まで残置
