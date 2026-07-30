# Intent: ペルソナ認知モデル

**ステータス**: 整理中 (旧 `persona_cognitive_model.md` v0.14 + `persona_action_tracks.md` v0.11 を再構造化中)
**親 Intent**: なし (本セットが上位概念)
**最終更新**: 2026-05-09

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
├── track_chronicle.md     ← Track 内必要情報の維持機構 (中断・再開機構の本体、v0.32〜)
├── nested_subline_spell.md ← 入れ子サブライン Spell 機構
├── line_tag_responsibility.md ← line と memorize タグの責務分離
├── phases/
│   ├── phase_1_base.md            ← 基盤刷新 (handoff 解消 + データモデル拡張)
│   ├── phase_2_track_metalayer.md ← Track / MetaLayer / Handler 基盤
│   ├── phase_3_lines_playbooks.md ← ライン仕様 + Track 種別 Playbook
│   ├── phase_4_pulse_scheduler.md ← Pulse 階層 + Scheduler + メタ定期判断
│   ├── phase_5_autonomy.md        ← 自律稼働の本格化
│   └── phase_6_extensions.md      ← 拡張機構 (Stelis 統合・モニタリング等)
└── revisions.md           ← v0.1〜v0.32 の改訂履歴 (差分情報の集約所)
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
| Track Chronicle (中断・再開機構の本体) の設計 | `track_chronicle.md` |

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
[Phase 3] ライン仕様 + Track 種別 Playbook   🟡 約 95%
   line: main/sub フィールド + 各 Track 種別の Playbook 整備 + 入れ子サブライン Spell 機構
   残: 「待ち」Track 廃止作業 (handoff_waiting_track_removal) / report_to_parent 厳密化 (handoff_report_to_parent_validation)
   (track_social/external の運用化と 7制御点 (1)(2)(6) は Phase 5 へ移送)
   ↓
[Phase 4] Pulse 階層 + Scheduler + メタ定期判断  ✅ 完了 (v0.30 + v2 メタ判断, 2026-05-10)
   AutonomyManager + SubLineScheduler + EventScheduler に集約 / META_JUDGMENT_CONFIG / 失敗時 retry / メタ判断 v2 (構造化出力)
   ↓
[Phase 5] 自律稼働の本格化                🔲 未着手
   Handler tick / 内部 alert / Track パラメータ / Schedule 統合 / 時間差ツール基盤
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
| 1-7 | `include_internal` フィルタを line_role / scope ベースへ移行 | ✅ | line_role / scope ベースに完全移行 (段階 4-D で `ContextRequirements.include_internal` フィールド削除済 v0.35, 2026-05-09) | P0-7 |
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
| ~~`AI.ACTIVITY_STATE` カラム~~ → `AI.AUTONOMY_ENABLED` | 🔄 | `database/models.py` | **2026-07-14 解体**: 4 値は実装上「Active か否か」しか効いておらず、真偽値 1 本 (自律行動の ON/OFF) へ置換し列は削除 ([landscape §9](../../overview/landscape.md)) |
| ~~`AI.SLEEP_ON_CACHE_EXPIRE` カラム~~ | ❌ | — | **2026-07-14 削除**: 列は掘られたが**本体コードから一度も読まれず**、機能は実装されていなかった (Sleep 消滅で存在理由も消滅) |
| Pulse-root context 構築機構 (`pulse_root_context.py`) | ✅ | (Phase 1.1 マージ済み) | Phase 1.1 |
| Handler に `track_specific_guidance` 属性追加 | ✅ | `track_handlers/*` | Phase 1.1 |
| `AI.current_active_track_id` カラム | 🔲 | 運用上は不影響だが計画上は予定あり | C-1 残件 |

**詳細**: `phases/phase_2_track_metalayer.md`

---

### Phase 3 — ライン仕様 + Track 種別 Playbook (🟡 約 90%)

旧 `context_profile` / `model_type` を `line: "main"|"sub"` 指定に集約。Track 種別ごとの専用 Playbook を整備。

> 2026-05-09 (v0.31): 「待ち」Track の整理に伴い、`track_waiting.json` の整備は廃止 (Track 状態 / 種別から `waiting` を抜く作業に転換)。時間差ツール基盤は Phase 5 に移送。詳細は `revisions.md` v0.31 (2026-05-09)。
>
> 2026-05-09 (v0.32): v0.31 で「pause_summary 書き込み側実装」とした項目について、書き込み側設計の本質拡張に伴い **`pause_summary` を完全廃止** し **Track Chronicle** (Track 内必要情報の維持機構) に置き換えることが確定。Intent doc は [`../track_chronicle.md`](../track_chronicle.md)、整理経緯は `revisions.md` v0.32 (2026-05-09) 参照。
>
> 2026-05-09 (v0.35): **段階 4-D 完了**。`context_profile` / `model_type` / `CONTEXT_PROFILES` / `include_internal` / `exclude_pulse_id` / `pulse:{uuid}` タグ併行記録 を全層から削除し、最新仕様 (line ベース + state["_messages"] 単一経路) に統合。tool ノードが結果を `state["_messages"]` に append しないバグ (= meta_judgment の judge ノードが track_list 出力を読めない) を併せて修正。`model_type=lightweight` ノード単位混在の Playbook 10 件 (source_*, deep_research, memopedia_write, research_task, memory_research) は archive/ 退避 (再構築方針)。詳細は `revisions.md` v0.35 / `docs/issues/archive/phase3_4d_dead_code_removal.md`。

| 項目 | 状態 | 実装場所 / 備考 | 旧称 |
|------|------|----------------|------|
| `SubPlayNodeDef.line` フィールド (`"main"|"sub"`) | ✅ | `sea/playbook_models.py:287-297` | C-2 |
| ライン runtime (親 messages のコピー分岐 + report_to_parent append) | ✅ | `sea/runtime_nodes.py` | C-2 |
| `meta_judgment.json` Playbook | ✅ | `builtin_data/playbooks/public/` | Phase 1.2 |
| `meta_judgment_dispatch.py` 経由パス | ✅ | (Phase 1.2 マージ済み) | Phase 1.2 |
| `track_user_conversation.json` Playbook | ✅ | `builtin_data/playbooks/public/` | C-2 |
| `track_autonomous.json` Playbook | ✅ | `builtin_data/playbooks/public/` | C-2 |
| `LLMNodeDef.context_profile` DEPRECATED 化 → 削除 | ✅ | 段階 4-D で完全削除 (v0.35, 2026-05-09)。最新仕様は `state["_messages"]` 単一経路 | Phase 1.4 |
| `LLMNodeDef.model_type` DEPRECATED 化 → 削除 | ✅ | 段階 4-D で完全削除 (v0.35, 2026-05-09)。`SubPlayNodeDef.line='sub'` 一括指定が代替 | Phase 1.4 |
| ~~`track_social.json` Playbook~~ | → Phase 5 | Playbook 雛形は存在 (`builtin_data/playbooks/public/track_social.json`) だが、Track ライフサイクル補完と一体で進める方針 (Phase 5 §「Track ライフサイクル補完」) | C-2 残件 |
| ~~`track_external.json` Playbook~~ | → Phase 5 | Playbook 雛形は存在 (`builtin_data/playbooks/public/track_external.json`) だが、外部チャネル統合 (X / Discord / Elyth) と一体で進める方針 (Phase 5 範疇) | C-2 残件 |
| ~~`track_waiting.json` Playbook~~ | ⛔ 廃止 | 「待ち」を Track 状態として持たない方針に変更 (v0.31, 2026-05-09)。下記「待ち」Track 廃止作業に統合 | C-2 残件 |
| **「待ち」Track 廃止作業** (Track 状態 / 種別 / ツール / TrackManager コードから `waiting` を完全除去) | 🔲 別 handoff | 詳細・作業手順は [handoff_waiting_track_removal.md](handoff_waiting_track_removal.md) に集約。本 README ではトラッキングのみ | Phase 3 新規 (v0.31) |
| **Track Chronicle 本体実装** (中断・再開機構の本体、pause_summary を完全廃止して置き換え) | 🟢 一巡完了 | Intent doc: [`../track_chronicle.md`](../track_chronicle.md)。書き込み (`_generate_track_chronicle` 新設、Metabolism 連動、Track 別生成、incomplete Lv1 + 再生成、1000 字未満スキップ) / 読み込み (head 入れ替え + 切り替え時 history 末尾近く挿入) / 時刻アンカー / dead code 撤去を含む。詳細は `revisions.md` v0.32 / v0.33、Intent doc §9 / §11 |
| **ユーザー会話 Track 親スレッド保持機構** (生メッセージで対話温度を担保、Stelis 親子モデル流用) | 🟢 一巡完了 | `saiverse/user_conversation_preserver.py` + `sea/runtime_context.py` 連携。リンクユーザーのオーナー Track 特定 + 不足分補完。`SAIVERSE_USER_CONV_PRESERVE_COUNT` (デフォルト 20) で調整可。詳細は `revisions.md` v0.33、Intent doc §11 | Phase 3 新規 (v0.33) |
| `report_to_parent` 必須バリデーション (`can_run_as_child=true` 用) | 🟡 別 handoff | 詳細・作業手順は [handoff_report_to_parent_validation.md](handoff_report_to_parent_validation.md) に集約。現状は警告ログのみ、厳密化未着手 | C-2 残件 |
| `exclude_pulse_id` 廃止 | ✅ | 段階 4-D で 4 層 (adapter / history_manager / runtime_context / runtime) 全削除 (v0.35, 2026-05-09) | C-2 残件 |
| Phase 3 翻訳前段の Playbook 整理 (旧プロトタイプ削除 + Spell 化) | ✅ | DB 67 → 43 件、`run_meta_auto` 関数削除、`ConversationManager` no-op 化、`playbook_sync` に prune 追加 (v0.19, 2026-05-01) | Phase 3 整理 |
| **line vs タグの責務分離整理** (context 構築を line ベースに統一、タグはレガシー除去) | ✅ | intent doc (v0.1) + 4-A (v0.21) + 4-B (v0.22) + 4-C (`migrate_playbooks_to_lines.py` で 33 件一括翻訳、v0.23) + **4-D 完了** (旧 DEPRECATED コード全削除、v0.35, 2026-05-09) | Phase 3 新規 |
| 入れ子サブライン Spell (`/run_playbook` + 深さ 4 階層 + `report_to_parent`) | ✅ | コア機構 + システムプロンプト注入 + `track_user_conversation` 1-LLM 構成 + `meta_user` 系削除完了 (v0.24-v0.28) | Phase 3 新規 |
| Playbook 一覧のシステムプロンプト注入 (`available_playbooks` セクション) | ✅ | `sea/runtime_context.py:118-152` で `## 利用可能な能力` セクション、`router_callable=true` を bullet list 化 | Phase 3 新規 |
| `track_user_conversation` を 1-LLM + Spell 構成に書き換え | ✅ | `track_user_conversation.json` は `main_line_response` (LLM 1) + `process_body` (control_body ツール) 構成 | Phase 3 新規 |
| UI からの Playbook 起動 (pre_spells 機構) + 引数あり対応 | ✅ | コア機構 + 引数省略形 `/spell name='X'` 対応 + `spell_args_decider` Playbook 経由の動的引数生成 + スケジュール経路適用 (v0.28、2026-05-08)。残: スケジュール作成 UI で pre_spells 指定する UX |
| `meta_user` / `sub_router_user` / `meta_user_manual` / `basic_chat` 削除 | ✅ | Playbook ファイル + DB レコード + コード残骸 (`update_router_selection` / `runtime_engine.py` 特別処理 / `inject_persona_event.py` 等) 全削除 + マイグレーション (`v0_3_0_dev1_legacy_schedule_playbook_names`、VERSION 0.3.0.dev1) で既存スケジュール書き換え (v0.28、2026-05-08) | Phase 3 新規 |
| `meta_playbooks` UI フィルタ修正 (`name.like("meta_%")` 廃止) | ✅ | `api/routes/people/summon.py` で `user_selectable=true` のみ判定。`track_user_conversation` がスケジュール編集 UI に出るように (v0.28、2026-05-08) | Phase 3 新規 |
| `LLMNodeDef.response_schema_source` (`spell:<name>` 動的解決) | ✅ | template 展開 + `SPELL_TOOL_SCHEMAS[name].parameters` 解決 (v0.28、2026-05-08) | Phase 3 新規 |
| `spell_args_decider` Playbook (引数決定の汎用部品) | ✅ | pre_spells 経路で引数省略形 → sub_line で起動 → 親ライン messages 継承でペルソナ認知から自然に引数決定 (v0.28、2026-05-08) | Phase 3 新規 |
| 既存スケジュール `selected_playbook` のマイグレーション | ✅ | `v0_3_0_dev2_legacy_schedule_selected_playbook` ハンドラで `selected_playbook=X` を `pre_spells=["/spell name='X'"]` に変換 (VERSION 0.3.0.dev2、2026-05-08) | Phase 3 新規 |
| 親 LLM messages のサブライン流入 (snapshot 経路) | ✅ | `tools/context.py` に `_LLM_MESSAGES` ContextVar + `persona_context(llm_messages=...)` 引数追加。spell loop が呼び出し時に snapshot 渡し、`run_playbook` が `parent_state["_messages"]` に展開。入れ子も自動で正しく動く (context manager の入れ子 reset)。実機検証 OK (v0.25, 2026-05-01) | Phase 3 新規 |
| `report_template` フィールドによる機械的 report 生成 | ✅ | `PlaybookSchema.report_template` 追加。子 Playbook 完了時に template を `{key}` / `{key.subkey}` で展開し `parent_state["report_to_parent"]` に書き込み。LLM コール不要で機械的サマリを返せる (例: `generate_image_playbook.json`)。実機検証 OK (v0.25, 2026-05-01) | Phase 3 新規 |
| Spell 結果の media を親 LLM ラウンドに attachment 転送 | ✅ | spell 戻り値を `Tuple[str, Optional[Dict]]` に拡張 (既存 str 戻り値は互換)。spell loop が全 spell の `metadata.media` を集約し次の LLM ラウンドの user message に lift。`run_playbook` が `parent_state["metadata"].media` を転送。`generate_image_playbook.json` の report に Markdown リンクリマインド追記 (v0.26, 2026-05-01) | Phase 3 新規 |
| 既存 Playbook の `context_profile` → `line` 翻訳 + `memorize.tags` 整理 (`migrate_playbooks_to_lines.py`) | ✅ | 33 件翻訳完了 (`context_profile` 75 / `internal` → `sub_line` 66 / `conversation` → `main_line` 5)。`model_type=lightweight` ノード単位混在の Playbook 10 件は archive/ 退避 (v0.35, 2026-05-09) | C-2 残件 |
| end-to-end 動作検証 (Spell loop / `/run_playbook` 1 段 / 入れ子) | ✅ | 運用で問題出ていないため完了とみなす (2026-05-10) | Phase 3 新規 |
| `context_profile` / `model_type` / `exclude_pulse_id` / 旧タグ参照 の完全削除 | ✅ | 段階 4-D 完了 (v0.35, 2026-05-09)。詳細は `docs/issues/archive/phase3_4d_dead_code_removal.md` | C-2 残件 |
| **メタ判断 Pulse の tool 結果到達バグ修正** (`lg_tool_node` で `state["_messages"]` への append 漏れ) | ✅ | `sea/runtime_engine.py:lg_tool_node` の tool 実行成功 / 失敗の両ブロックに append 経路追加。これで `meta_judgment.json` の judge ノードが `track_list` 出力 (`last_message_relative` 等) を実際に読める (v0.35, 2026-05-09) | Phase 3 新規 |

**詳細**: `phases/phase_3_lines_playbooks.md`

---

### Phase 4 — Pulse 階層 + Scheduler + メタ定期判断 (✅ 完了 v0.30 + v2 メタ判断, 2026-05-10)

メインライン Pulse / サブライン Pulse の 2 階層分離 + 各 Scheduler 実装 + メタレイヤーの定期実行入口。

| 項目 | 状態 | 実装場所 / 備考 | 旧称 |
|------|------|----------------|------|
| Handler に `pulse_completion_notice` / `post_complete_behavior` 属性 | ✅ | `track_handlers/social_track_handler.py:48`, `autonomous_track_handler.py:43` | C-3 |
| Handler に `default_pulse_interval` / `default_max_consecutive_pulses` / `default_subline_pulse_interval` | ✅ | `autonomous_track_handler.py:44-46` | C-3 |
| `SubLineScheduler` クラス | ✅ | `saiverse/pulse_scheduler.py:76-127` | C-3b |
| `AutonomyManager` をメタレイヤー定期 tick タイマー化 → EventScheduler 駆動に変更 | ✅ | `saiverse/autonomy_manager.py` (v0.30 で push 駆動化) | C-3c |
| `MetaLayer.on_periodic_tick` (定期実行入口) | ✅ | `saiverse/meta_layer.py:192-` | C-3 |
| `MetaLayer` の per-persona 直列化 Lock (`on_track_alert` / `on_periodic_tick`) | ✅ | `saiverse/meta_layer.py:__init__`, `_get_lock` (v0.16, 2026-04-30) | handoff Part 1 |
| `meta_judgment_log` スキーマ v0.15 整合化 + 書き込み + 動的注入 | ✅ | (v0.16, 2026-04-30) | handoff Part 2 |
| SAIMemory `messages.pulse_id` カラム化 (Phase 2.5) | ✅ | (v0.17, 2026-05-01) | Phase 2.5 |
| 自律先制と外部 alert のレース解消 (Phase 2.6) | ✅ | (v0.18, 2026-05-01) | Phase 2.6 |
| **anchor touch を LLM 呼び出し成功後に移動** (Metabolism バグ修正) | ✅ | `sea/runtime.py:_touch_anchor_after_llm_call` (v0.30) | Phase 4-e |
| **`META_JUDGMENT_CONFIG` カラム新設** (ペルソナ別 Pulse パラメータ) | ✅ | `database/models.py`, `saiverse/meta_layer.py`, `frontend/SettingsModal.tsx` (v0.30) | Phase 4-e |
| **`EventScheduler` 新設 + コア側ポーリング全廃** | ✅ | `saiverse/event_scheduler.py`, ScheduleManager / AutonomyManager / InternalAlertPoller / DB polling / SDS heartbeat 全部統合 (v0.30) | Phase 4-e |
| **waiting Track timeout を EventScheduler に push** | ✅ | `saiverse/track_manager.py:_schedule_waiting_timeout` (v0.30) | Phase 4-e |
| **メタ判断 Pulse 失敗時の retry ループ** (max_retries / retry_backoff_seconds) | ✅ | `saiverse/meta_layer.py:_run_judgment_via_playbook` (v0.30) | Phase 4-e |
| **自動発話間隔の二重管理を解消** (META_JUDGMENT_CONFIG が真実、API 経由で永続化) | ✅ | `saiverse/autonomy_manager.py`, `api/routes/people/autonomy.py` (v0.30) | Phase 4-e |
| `keep_cache_alive` フラグ (低頻度ペルソナ向けに TTL 前倒し OFF) | ✅ | UI: SettingsModal の tri-state select + コスト警告 (v0.30) | Phase 4-e |
| ~~環境別デフォルト値の自動推定 (Pattern A/B/C)~~ | → 削除 | ペルソナ別 `META_JUDGMENT_CONFIG` の UI 編集で代替済。Pattern 自動推定機構は不要と判断 (Phase 4 完了マーク、2026-05-10) | C-3 |
| 7 制御点の実装場所明確化 | ✅ 部分 + → Phase 5 | (3)(4)(5)(7) は v0.30 で確定済。(1) Pulse 間隔 / (2) 連続実行回数上限 / (6) 完了後挙動 は Track metadata の API 整備が必要なので Phase 5 (Track パラメータ機構) に移送 | C-3 |
| **メタ判断 v2 (構造化出力ベース)**: 状況 A〜E 分類 → Playbook 4 分割 → 動的 response_schema (anyOf field-level discriminator) → finalize ツールで JSON → monologue + /spell 行に整形 + SAIMemory 書き込み | ✅ | 実装一巡 + 関連バグ 2 件修正済 + 実機検証済 (2026-05-10)。`saiverse/meta_layer.py` (`_classify_situation` / `_build_response_schema`), `builtin_data/tools/meta_judgment_finalize.py`, `builtin_data/playbooks/public/meta_judgment_{alert,running,idle_pending,idle_empty}.json`, `sea/runtime_llm.py` (`response_schema_source: "arg:<key>"` 機構). 詳細: [meta_judgment_structured.md](meta_judgment_structured.md) | Phase 4 新規 (v2, 2026-05-10) |
| **wait_response_timeout 即発火ループ修正**: `base_time` が過去のときも `now()` フォールバックする (= activate 時刻基準の N 分猶予) | ✅ 2026-05-10 | `saiverse/track_manager.py:_schedule_wait_response_timeout`. メタ判断 v2 で構造化出力が Track 操作を強制するようになって顕在化した既存設計の欠陥 | Phase 4 新規 (v2 関連, 2026-05-10) |
| **7層ストレージタブの削除UI**: 旧仕様で蓄積された汚染メタ判断ログの除去用に実装。正規の観察面が揃ったためタブ自体は 2026-07-16 に退役し、保守用 DELETE API のみ残した | ✅ 2026-05-10 → UI退役 2026-07-16 | `api/routes/people/storage_layers.py` (`meta-judgment` / `track-logs` の DELETE + bulk-delete) | Phase 4 関連 (v2 準備)。退役判断: [memory_modal_legacy_tabs_retirement.md](../memory_modal_legacy_tabs_retirement.md) |

**詳細**: `phases/phase_4_pulse_scheduler.md`、`revisions.md` v0.30、[meta_judgment_structured.md](meta_judgment_structured.md) (v2 詳細)

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
| **Track ライフサイクル補完** (ペルソナ削除 / Building 移動 / 複数アカウント合流時の挙動) | 🔲 | Phase 5 新規 (track_social 着手前提) |
| **Track 忘却の自動化** (dormant → forgotten 遷移 / `MAX_DORMANT_COUNT` 超過時の優先 forget) | 🔲 | Phase 5 新規 (不変条件 5 担保) |
| **時間差ツール基盤** (call_id 採番 / イベントメッセージ配送 / 不在 Track への alert 通知 / タイムアウトはツール側責務) | 🔲 | Phase 5 新規 (v0.31, Phase 3 から移送)。Kitchen / MCP Progress / dispatch_persona / X 投稿等の個別ツール実装はサブタスク化。詳細は `revisions.md` v0.31 |

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

- [handoff_waiting_track_removal.md](handoff_waiting_track_removal.md) — 🔲 未着手 (別セッション対応予定): 「待ち」Track 機構廃止作業 (revisions.md v0.31 方針)
- [handoff_report_to_parent_validation.md](handoff_report_to_parent_validation.md) — 🔲 未着手 (別セッション対応予定): `can_run_as_child=true` の Playbook で `report_to_parent` 必須バリデーション
- [handoff_2026-05-10.md](handoff_2026-05-10.md) — ✅ 対応完了 commit 5d567a7: メインライン応答の `origin_track_id` NULL 回帰バグ修正 (pending/alert 状態の Track でも Handler 経路で track_id を持ち回す経路を追加)
- [handoff_2026-05-09.md](handoff_2026-05-09.md) — 自律稼働中の長期 idle 脱出機構 (wait_response Track 自動 pause タイマー + Track 最終メッセージ時間の可視化)
- [handoff_2026-05-08.md](handoff_2026-05-08.md) — Phase 3 A 残件 (`meta_user` 系削除 + スケジュール `pre_spells` 適用)
- [handoff_2026-05-01.md](handoff_2026-05-01.md) — Phase 3 全体ロードマップ handoff
- [handoff_phase3_impl.md](handoff_phase3_impl.md) — 段階 4-A〜4-D + Spell コア実装時の handoff (4-D も 2026-05-09 完了)
- `docs/issues/archive/phase3_4d_dead_code_removal.md` — 段階 4-D 完了ログ
- [handoff_2026-04-30.md](handoff_2026-04-30.md) — Phase 2 / 2.5 / 2.6 完了時 handoff
- [pulse_dispatch.md](pulse_dispatch.md) — Pulse 起動経路ディスパッチ Intent (v0.3 実装一巡完了, 2026-05-10): 直接経路 / 熟慮経路 / メタ判断並列レーンの 3 構造、`on_track_activated` hook 導入、PulseController 改修、alert 発生経路網羅。段階 1〜5 実装完了 (ケース 4 実機検証済 / ケース 5・6 は自律稼働観察中)、段階 6 (alert 経路運用化 β/γ/δ/ε) は Phase 5 範疇
- [meta_judgment_structured.md](meta_judgment_structured.md) — メタ判断 v2 (構造化出力ベース) Intent (v0.3, 2026-05-10 実機検証 1 回目 + 関連バグ 2 件修正済、`02_mechanics.md` §「メタレイヤーの実行サイクル」の置き換え予定)
- [track_chronicle.md](track_chronicle.md) — Track 内必要情報の維持機構 (中断・再開機構の本体、Phase 3 の本体実装) Intent (v0.1, 2026-05-09 起草)
- [nested_subline_spell.md](nested_subline_spell.md) — Phase 3 の `/run_playbook` Spell 機構 Intent (v0.1, 2026-05-01 起草)
- [line_tag_responsibility.md](line_tag_responsibility.md) — line と memorize タグの責務分離 Intent (v0.1, 2026-05-01 起草)
- `unified_memory_architecture.md` v3 — v0.3.0 中心軸 (pulse_logs / Chronicle / Memopedia / 自律稼働バイオリズム)
- `dynamic_state_sync.md` — Metabolism 機構と A/B/C 状態モデル (本セットが活用する基盤)
- `kitchen.md` — Kitchen 通知が「Track への切り替え要求」に該当
- `mcp_protocol_coverage.md` — Elicitation / Cancellation がこのモデルに依存
- `stelis_thread.md` — Stelis スレッドの設計 (当面別物として共存、Phase 6 で統合)
- `handoff_track_context_management.md` — Phase 1 のもとになった handoff 観察記録
- (旧) `persona_cognitive_model.md` v0.14 — 整理完了まで残置
- (旧) `persona_action_tracks.md` v0.11 — 整理完了まで残置
