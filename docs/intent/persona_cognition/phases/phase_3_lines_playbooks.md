# Phase 3 — ライン仕様 + Track 種別 Playbook

**親**: [../README.md](../README.md)
**ステータス**: 🟡 約 80% (2026-05-09 改訂で Track Chronicle 本体実装 / 待ち整理タスクを追加)
**旧称**: Phase C-2 (line / context_profile DEPRECATED) + Phase 1.2 (meta_judgment 経路) + Phase 1.4 (DEPRECATED 宣言)

---

## 目的

旧 `context_profile` / `model_type` を `line: "main"|"sub"` 指定に集約。Track 種別ごとの専用 Playbook を整備し、メインライン Pulse の判断ロジックを Playbook で表現できるようにする。

---

## タスク

### ライン仕様の導入

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| `SubPlayNodeDef.line` フィールド (`"main"|"sub"`) | ✅ | `sea/playbook_models.py:287-297` |
| ライン runtime (親 messages のコピー分岐 + report_to_parent append) | ✅ | `sea/runtime_nodes.py` |
| `LLMNodeDef.context_profile` DEPRECATED 化 | ✅ | `sea/playbook_models.py:48-55` |
| `LLMNodeDef.model_type` DEPRECATED 化 | ✅ | `sea/playbook_models.py:57-62` |
| `report_to_parent` 必須バリデーション (`can_run_as_child=true` 用) | 🟡 | runtime ルーティングは実装、厳密化は警告ログのみ |
| `exclude_pulse_id` 廃止 | 🔲 | 旧仕様コードは現存 |

### Track 種別 Playbook

| Playbook | 状態 | 場所 / 備考 |
|----------|------|------|
| `meta_judgment.json` | ✅ | `builtin_data/playbooks/public/` |
| `meta_judgment_dispatch.py` 経由パス | ✅ | (Phase 1.2 マージ済み) |
| `track_user_conversation.json` | ✅ | `builtin_data/playbooks/public/` |
| `track_autonomous.json` | ✅ | `builtin_data/playbooks/public/` |
| `track_social.json` | 🔲 | 未着手 (Track ライフサイクル補完が前提、Phase 5 タスク参照) |
| `track_external.json` | 🔲 | 未着手 |
| ~~`track_waiting.json`~~ | ❌ | **廃止**: 「待ち」は Track 状態でなく行動の性質。下の「待ち機構の整理」参照 |

### line vs タグの責務分離整理 (Phase 3 新規)

`line_role` / `line_id` / `scope` カラム (Phase 1 で実装済) と `metadata.tags` が context 構築で二重制御になっている問題を整理する。詳細は [../line_tag_responsibility.md](../line_tag_responsibility.md) (v0.1, 2026-05-01)。

| 項目 | 状態 | 備考 |
|------|------|------|
| intent doc 起草 | ✅ | `line_tag_responsibility.md` v0.1 |
| 段階 4-A: context 構築を line ベースに切替 (`required_tags` 廃止) | ✅ | `sea/runtime_context.py`, `saiverse_memory/adapter.py`, `persona/history_manager.py`, `sai_memory/memory/storage.py`, `sea/runtime.py:1559`, `persona/mixins/generation.py:170` (v0.21, 2026-05-01) |
| 段階 4-B: `sub_play` の `report_to_main` を line ベースに統一 + `report_to_parent` リネーム | ✅ | `sea/runtime_nodes.py`, `sea/playbook_models.py`, `tests/test_subplay_line.py` 更新 + `tests/test_payload_context_filter.py` 新規 28 件追加 (v0.22, 2026-05-01) |
| 段階 4-C: 既存 Playbook の `memorize.tags` 整理 + `context_profile` 削除 | ✅ | `scripts/migrate_playbooks_to_lines.py` 新規 (Y 案、`model_type=lightweight` 保留) で 33 件一括翻訳: `context_profile` 75 ノード削除 / `internal` → `sub_line`+`volatile` 66 件 / `conversation` → `main_line`+`committed` 5 件。`MemorizeNodeDef` に line_role/scope フィールド追加、`lg_memorize_node` で `_store_memory` に渡す経路追加 (v0.23, 2026-05-01) |
| 段階 4-D: 旧 DEPRECATED コードの削除 (`include_internal` / `pulse:{uuid}` タグ併行記録 / `model_type` / `LLMNodeDef.context_profile` Pydantic フィールド) | 🔲 | runtime + storage、`/run_playbook` Spell 実装後 |

**位置付け**: 入れ子サブライン Spell 実装の**前段**。タグレガシーが残ったまま新機構を入れると二重制御が深まるため、本整理を先に完遂する。

### 入れ子サブライン Spell 機構 (Phase 3 新規)

メインライン (or 親サブライン) の Playbook から `/run_playbook` Spell 経由で別 Playbook をサブラインとして起動できるようにする。Playbook グラフ内の `sub_play` ノードでなく、**Spell loop の中から動的に Playbook を呼び出す**経路を新設する。

**設計詳細**: [../nested_subline_spell.md](../nested_subline_spell.md) (v0.1, 2026-05-01 起草)

| 項目 | 状態 | 備考 |
|------|------|------|
| intent doc 起草 | ✅ | `nested_subline_spell.md` v0.1 (2026-05-01) |
| `/run_playbook` Spell 仕様確定 | ✅ | 引数は Playbook 名のみ。Playbook 引数は呼ばれた側で構造化出力で決める (旧 router 踏襲) |
| Spell loop → Playbook 起動の橋渡し runtime | ✅ | `builtin_data/tools/run_playbook.py` 新規。Spell 自動登録、内部で `sea_runtime._run_playbook(line="sub")` を呼ぶ (v0.24, 2026-05-01) |
| 入れ子深さ制限 (上限 4 階層) | ✅ | `pulse_ctx._line_stack` の長さで判定、stack length 5 (depth 4) まで許容、6+ は拒否 (v0.24) |
| `report_to_main` → `report_to_parent` リネーム | ✅ | 段階 4-B と一体実施 (v0.22, 2026-05-01) |
| `report_to_parent` のスタック昇り経路 | ✅ | sub_play 経路と統一: parent_state["report_to_parent"] を string で返却 (v0.24) |
| 親 LLM messages のサブライン流入 (snapshot 経路) | ✅ | `tools/context.py` に `_LLM_MESSAGES` ContextVar + `persona_context(llm_messages=...)` 引数追加。spell loop が呼び出し時に snapshot 渡し、`run_playbook` が `parent_state["_messages"]` に展開。入れ子も自動で正しく動く (context manager の入れ子 reset)。実機検証 OK (v0.25, 2026-05-01) |
| `report_template` フィールドによる機械的 report 生成 | ✅ | `PlaybookSchema.report_template: Optional[str]` 追加。子 Playbook 完了時に template を `{key}` / `{key.subkey}` で展開し `parent_state["report_to_parent"]` に書き込み。LLM コール不要で機械的サマリを返せる。`generate_image_playbook.json` で実例追加。実機検証 OK (v0.25, 2026-05-01) |
| Spell 結果の media を親 LLM ラウンドに attachment 転送 | ✅ | spell 戻り値を `Tuple[str, Optional[Dict]]` に拡張 (str 戻り値も互換)。spell loop が全 spell の `metadata.media` を集約し、次の LLM ラウンドの user message の `metadata.media` に lift。`run_playbook` が `parent_state["metadata"].media` を転送。複数 spell × 複数 media 合算対応。`generate_image_playbook.json` の `report_template` に Markdown リンク (`saiverse://item/<id>/content`) リマインドも追記 (v0.26, 2026-05-01) |
| line_id の親子関係 + cancellation 伝搬 | 🟡 | parent_state 経由で `_pulse_context` 共有。cancellation 伝搬は実機検証で確認予定 |
| Playbook 一覧のシステムプロンプト注入 | ✅ | `sea/runtime_context.py:118-152` で `## 利用可能な能力` セクションを bullet list 形式で組み立て、`router_callable=true` な Playbook を列挙。`ContextRequirements.available_playbooks` で活性化、`conversation` プロファイルで自動有効 |
| `router_callable` 運用整理 | ✅ | 削除予定だった `meta_user` / `meta_user_manual` / `basic_chat` が消えたので残るのは妥当な分布のみ (v0.28、2026-05-08) |
| `track_user_conversation` を 1-LLM + Spell 構成に書き換え | ✅ | `track_user_conversation.json` は `main_line_response` (LLM 1) + `process_body` (control_body ツール) 構成。Spell 実行ループは LLM ノード内で runtime が回す設計のため、Playbook 定義側で `/run_playbook` を明示する必要なし |
| **UI からの Playbook 起動 (pre_spells 機構)** | ✅ | コア機構 + 引数あり Spell 対応 + スケジュール経路適用が完了 (v0.28、2026-05-08)。`/spell name='X'` 引数省略形は `spell_args_decider` Playbook で動的引数生成。`PersonaSchedule.PLAYBOOK_PARAMS.pre_spells` から `submit_schedule(pre_spells=...)` に流れる経路完成 |
| **`meta_user` / `sub_router_user` / `meta_user_manual` / `basic_chat` の deprecated 化 → 削除** | ✅ | Playbook ファイル + DB レコード + コード残骸 (`update_router_selection` 関数 / `runtime_engine.py` 特別処理 / `runtime_llm.py:812` デバッグログ条件 / `inject_persona_event.py` / `schedule_management_playbook.json` 内 LLM プロンプト等) 全削除 + マイグレーションハンドラで既存スケジュール書き換え (v0.28、2026-05-08) |
| **スケジュール起動経路の `pre_spells` 化** | ✅ | `submit_schedule(pre_spells=...)` 引数追加 + `_execute_schedule` で `PLAYBOOK_PARAMS.pre_spells` 抽出 + `v0_3_0_dev2_legacy_schedule_selected_playbook` ハンドラで旧 `selected_playbook` を `pre_spells=["/spell name='X'"]` に変換 (v0.28、2026-05-08)。残: スケジュール作成 UI で `pre_spells` を指定する UX |
| end-to-end 動作検証 (Spell loop / `/run_playbook` 1 段 / 入れ子) | 🟡 | track_user_conversation 通常会話 OK (2026-05-08 まはー報告)。スケジュール経路の検証は次セッション以降 |
| **`response_schema_source` (`spell:<name>` 動的解決)** | ✅ | `LLMNodeDef.response_schema_source` フィールド + `_resolve_response_schema_source` ヘルパで `SPELL_TOOL_SCHEMAS[name].parameters` 解決 (v0.28、2026-05-08) |
| **`spell_args_decider` Playbook (引数決定の汎用部品)** | ✅ | pre_spells 経路で引数省略形が来たとき sub_line で起動。親ライン messages 継承 (v0.25 snapshot 経路) でペルソナ認知から自然に引数決定 (v0.28、2026-05-08) |

**動機**: 従来 `meta_user` で router → 通常発話と 2 回 LLM を呼んでいた構造を、スペルで Playbook を呼べる通常発話ノード一個に統一する。判断と発話の合体。

**Phase 4 着手前に必須**: MainLineScheduler 系の判断にも影響する可能性が高い。

### 旧 Playbook の翻訳

| 項目 | 状態 |
|------|------|
| 翻訳前段の Playbook 整理 (旧プロトタイプ削除 + Spell 化) | ✅ (v0.19, 2026-05-01) |
| 既存 Playbook の `context_profile` → `line` 翻訳 (`migrate_playbooks_to_lines.py`) | ✅ (v0.23, 2026-05-01)、`model_type=lightweight` は Y 案で保留 |
| `context_profile` / `model_type` / `exclude_pulse_id` の完全削除 | 🔲 段階 4-D で実施 (`/run_playbook` Spell 実装後) |

#### 整理結果 (2026-05-01)

翻訳作業に入る前に、対象 Playbook を圧縮するため以下を実施:

- **削除した Playbook**: 19 件
  - 旧自律稼働プロトタイプ: `meta_auto`, `meta_auto_full`, `sub_router_auto`, `sub_perceive`, `sub_reaction`, `sub_finalize_auto`, `sub_execute_phase`, `sub_detect_situation_change`, `sub_generate_want`, `wait`
  - テスト/残骸: `meta_websearch_demo`, `detail_recall_playbook`, `meta_agentic`, `agentic_chat_playbook`
  - Spell 代替済み: `memory_recall_playbook` (`memory_recall_unified` Spell), `web_search_step` (`source_web` Playbook)
  - 新規 Spell 化: `uri_view` (`resolve_uri` ツールに `spell=True`), `send_email_to_user_playbook` (`send_email_to_user` ツールに `spell=True`)
  - サンプル保存後削除: `web_search_sub` ([sub_line_playbook_sample.md](sub_line_playbook_sample.md) に内容を保存)

- **更新した Playbook**: `deep_research_playbook` の `exec_search` ノードを `web_search_step` → `source_web` に差し替え

- **コード側の整理**:
  - `sea/runtime.py`: `run_meta_auto` 関数削除、`_choose_playbook` の `meta_auto` fallback 削除
  - `sea/pulse_controller.py`: 旧 `auto-without-meta_playbook` 分岐削除、auto pulse は `meta_playbook` 必須化
  - `saiverse/conversation_manager.py`: `ConversationManager` クラスを no-op 化 (新認知モデルの `track_autonomous` + PulseScheduler 経路に統一)
  - `builtin_data/tools/detail_recall.py`: 削除

- **DB**: playbooks テーブル 67 → 48 件

整理に伴うコード経路の変更詳細は [revisions.md](../revisions.md) v0.19 を参照。

#### 残課題 (Phase 3 翻訳作業外で対応)

- `ConversationManager` クラスごと削除 (saiverse_manager.py / manager/runtime.py / manager/admin.py の参照整理を伴う)
- ~~DB 残骸の整理~~ → 起動時 prune を `playbook_sync.py` に追加して解決 (revisions v0.19 追補)

---

## 残タスクの詳細

### `track_social.json` Playbook

交流 Track 用。同 Building 内の他ペルソナ発話 (audience に自分が含まれる) で alert 化された時の処理を担う。

**設計の出発点**:

- メインライン (重量級) で起動
- 相手ペルソナの Person Note を自動開封
- 多者会話の場合、audience 解釈ロジックを Playbook 内で展開
- 応答完了後は `wait_response` 状態 (= 次の発話を待つ)

参考実装: `track_user_conversation.json` を雛形に、ユーザー固有処理を「相手ペルソナ固有処理」に置き換える形。

### `track_external.json` Playbook

外部 SAIVerse / Discord / X 等への通信 Track 用。`output_target=external:<channel>:<address>` で動作。

**設計の出発点**:

- 外部チャネルごとの送信ロジック (Discord webhook / X API / SAIVerse 間 dispatch 等) はツールに分離
- Playbook はメッセージ生成と送信タイミングの判断のみ担う
- 外部応答は「時間差で結果が返ってくるツール」の汎用基盤 (Phase 5) で受ける。Track の状態遷移としての `waiting` 概念は持たない

### Track Chronicle 本体実装 (Phase 3 新規、2026-05-09 追加 / v0.32 で再設計)

Track 内で Metabolism によりコンテキストから押し出された必要情報を、Track 目的に沿って圧縮保存し、再アクセス時にコンテキストへ呼び戻す機構。**v0.31 で「`pause_summary` 書き込み側実装」とされていた項目は、本機構として再設計された** (= `pause_summary` は完全廃止、Track Chronicle で置換)。

設計の核と実装計画は **[`../track_chronicle.md`](../track_chronicle.md) (Intent doc, v0.1, 2026-05-09 起草) に集約**。整理経緯は [../revisions.md](../revisions.md) v0.32 (2026-05-09) 参照。

**v0.32 で確定した骨子**:

- 書き込み: Metabolism 連動。押し出し対象を `origin_track_id` で Track ごとに分けて Chronicle DB (`arasuji_entries`) に entry 追加。バッチサイズ未満は `incomplete: true` フラグ付き、後で 20 件揃ったら正規 Lv1 に再生成。1000 字未満ならスキップ。新規関数 `_generate_track_chronicle` として独立経路で実装
- 読み込み (head): アクティブ Track の Chronicle 一式を `get_episode_context` (origin_track_id フィルタ版) で取得し、Memory Weave context として head 配置。Metabolism のたびに head が新アクティブ Track のものに入れ替わる
- 読み込み (history 末尾近く): Track 切り替え時、`_promote_meta_judgment_in_pulse` の延長でメタ判断独白の committed 昇格直後に切り替え先 Track の Chronicle を独立メッセージ (role='user' + `<system>` ラップ) として INSERT
- 時刻アンカー: Metabolism 時、最古残存メッセージ直前に揮発挿入 (`<system>以下、YYYY-MM-DD HH:MM:SS 以降のやり取りです</system>`)
- 撤去対象: `prepare_pulse_root_context` / `build_fixed_section` / `build_dynamic_section` / `pause_summary` 関連 (DB / API / Frontend) を一括撤去

**実装順序の目安** ([`../track_chronicle.md`](../track_chronicle.md) §9 参照):

1. `arasuji_entries` に `origin_track_id` カラム追加 (migration)
2. `ArasujiGenerator` の Track 用拡張 (入力フィルタ + 抽出プロンプト差し替え)
3. `_generate_track_chronicle` 新設 + Metabolism 連動
4. `get_memory_weave_context` の Track Chronicle セクション追加 (head 配置)
5. `_promote_meta_judgment_in_pulse` 延長 (切り替え時挿入)
6. 時刻アンカー揮発挿入
7. 1000 字未満生メッセージ取得経路
8. dead code 撤去 (上記)

**General Chronicle 側の課題** (本機構と独立に処理):

- 生成 trigger を Metabolism 押し出し対象判定に変更 → [`../../../issues/general_chronicle_metabolism_trigger.md`](../../../issues/general_chronicle_metabolism_trigger.md)
- 自律稼働中に Chronicle が生成されない問題 → [`../../../issues/general_chronicle_user_pulse_only.md`](../../../issues/general_chronicle_user_pulse_only.md)

### 待ち機構の整理 (Phase 3 新規、2026-05-09 追加)

「待ち」を Track の特殊状態として扱うのではなく、**結果が時間差で返ってくる行動の性質**として整理する。整理の経緯と結論は [../revisions.md](../revisions.md) v0.31 (2026-05-09) 参照。

**整理の核**:

- 「待ち」は Track 種別ではなく、行動 (ツール / Spell / Playbook ノード) の性質
- 行動者は予定調和的に「これは結果が時間差で返る」と認識して実行する
- Track の中断は「待ち」とは独立。メタ判断者がその時 Track を続けるか別 Track に移るかを決める
- 結果到達は Track 内のイベントメッセージとして処理される (Track 不在なら Alert)
- 時間差ツール基盤の本実装は Phase 5 ([phase_5_autonomy.md](phase_5_autonomy.md))

**Phase 3 で行う廃止作業**:

| 項目 | 状態 | 廃止理由 |
|------|------|---------|
| `track_waiting.json` Playbook | 🔲 削除 | Track 種別ではなく「待ち」を独立 Playbook 化していた誤設計 |
| `STATUS_WAITING` (`saiverse/track_manager.py`) | 🔲 削除 | 状態として独立する必要なし。pending と区別する根拠がなくなる |
| `track.waiting_for` カラム + 関連 API | 🔲 削除 | 「何を待っているか」はツール / Spell の引数 + 結果イベントで自己記述する |
| `track.waiting_timeout_at` カラム + EventScheduler 予約 | 🔲 削除 | timeout もツール側責務 (= 「結果不到達」イベントの一形態) |
| `TrackManager.wait()` / `resume_from_wait()` メソッド | 🔲 削除 | 状態廃止に伴う |
| Phase 4-e で実装した `_schedule_waiting_timeout` / `_handle_waiting_timeout` | 🔲 削除 | 時間差ツール基盤に移行 |
| `04_handlers.md` の `post_complete_behavior` 表から「waiting」削除 | 🔲 | 「Track 種別」として誤って記述されていた |

**移行注意**:

- 既存ペルソナの動作中 Track に `STATUS_WAITING` が残る場合、マイグレーション (`scripts/migrate_*`) で `pending` 等に変換
- Phase 4-e の revisions v0.30 で実装した待機 timeout 機構は、本廃止と相殺になる。revisions v0.31 で経緯を記録

**Phase 5 への接続**: 時間差ツールの汎用基盤 (起動 / 結果配送 / Track 不在時 Alert / timeout イベント) は Phase 5 で整備する。Phase 3 の段階では旧機構を廃止するところまで。

### `report_template` (機械的レポート生成、2026-05-01 実装済)

LLM コール不要で `report_to_parent` を機械的に生成する経路。`PlaybookSchema.report_template: Optional[str]` フィールドを Playbook トップレベルに置き、子 Playbook 完了時に runtime が `{key}` / `{key.subkey}` プレースホルダを最終 state で展開して `parent_state["report_to_parent"]` に書き込む。

例 (`generate_image_playbook.json`):
```json
{
  "report_template": "画像「{gen_params.title}」の生成が完了しました。\n\n{text}"
}
```

- 機械的に決まる成果物 (画像生成、ファイル作成、ツール戻り値の整形等) は LLM ノードを挟まず即返せる
- 動的サマリが要る場合は従来どおり LLM/memorize ノードで `state["report_to_parent"]` に書く経路も維持
- output_schema への `report_to_parent` 明記は不要 (template 経路は parent_state に直接書き込む)

### `report_to_parent` 厳密バリデーション

現状: runtime ルーティングは実装されているが、`output_schema` に `report_to_parent` が含まれていない子 Playbook も警告ログのみで通ってしまう。

**やること**:

1. `PlaybookSchema` に `can_run_as_child: bool` メタ属性を追加 (デフォルト false)
2. Playbook ロード時 (`save_playbook` ツール / `import_playbook.py`) でバリデーション
3. `can_run_as_child=true` かつ `report_to_parent` が `output_schema` にない → 例外 (警告ではなく)

```python
def validate_child_playbook(playbook: PlaybookSchema) -> None:
    if not playbook.can_run_as_child:
        return
    if "report_to_parent" not in (playbook.output_schema or []):
        raise ValueError(
            f"Playbook '{playbook.name}' lacks 'report_to_parent' in output_schema. "
            f"Child playbooks must report back to their parent line."
        )
```

### `migrate_playbooks_to_lines.py`

既存 Playbook の `context_profile` / `model_type` を `line: "main"|"sub"` に翻訳するスクリプト。

**翻訳ルール (案)**:

| 旧仕様 | 新仕様 |
|--------|-------|
| `context_profile: "default"` + `model_type: undefined` | `line: "main"` |
| `context_profile: "default"` + `model_type: "lightweight"` | `line: "main"` (継続) + 軽量モデル指定は別フィールドで継承 |
| `context_profile: "isolated"` 等 | `line: "sub"` (分岐) |
| `context_profile: "worker"` (完全独立) | Phase 6 で別途実装、現状は警告 |

機械的に翻訳できる部分は自動化、判断が必要な部分はユーザー確認を求める対話モード。

### `context_profile` / `model_type` / `exclude_pulse_id` の完全削除

すべての Playbook が新仕様に移行したことを確認後:

- `LLMNodeDef.context_profile` 削除
- `LLMNodeDef.model_type` 削除
- `CONTEXT_PROFILES` 定義削除
- `exclude_pulse_id` および関連の `PulseContext` 制御削除
- 関連ランタイムコード削除

---

## 段階移行計画

旧 Phase C-2a/b/c に対応:

| サブ Phase | 内容 | 状態 |
|-----------|------|------|
| 3-a (旧 C-2a) | 新仕様の追加 (旧仕様と共存) | ✅ 完了 |
| 3-b (旧 C-2b) | 既存 Playbook の改修 (`migrate_playbooks_to_lines.py`) | ✅ 完了 (v0.23、Y 案で `model_type=lightweight` 23 ノードのみ保留) |
| 3-c (旧 C-2c) | 旧仕様の削除 | 🔲 段階 4-D で実施 (`/run_playbook` Spell 実装後) |

---

## Playbook で表現できる範囲の確認

実装着手前の確認項目:

1. **モデル指定**: `line: "main"` (重量級指定) を確実に効かせられるか
2. **Track 情報をプロンプトに埋め込む**: 状態変数経由で Track 固定/動的情報を注入できるか
3. **スペル発火と応答生成の混在**: 1 ノードで「内的独白 + スペル + 発話」を表現できるか (Phase 2 で部分的に実証済み)
4. **Pulse 完了通知**: Playbook 完了時に呼び出し元 (PersonaCore or MetaLayer) に「次の挙動」を伝える機構が必要か

3, 4 が Playbook で表現できなければ (b) 路線 (メインライン Pulse 開始処理を Python で新規実装) に切り替える。

---

## 完了の判定基準

- [x] `SubPlayNodeDef.line` フィールドが受け入れられ、`line: "sub"` で子ラインが分岐実行される
- [x] 子ラインの `report_to_parent` が親メッセージに append される
- [ ] `can_run_as_child=true` Playbook が `report_to_parent` を欠いていたらロード時例外
- [ ] 必要な Track 種別 Playbook (user_conversation / social / autonomous / external) が揃う
- [ ] 既存 Playbook が `migrate_playbooks_to_lines.py` で全て翻訳済み
- [ ] `context_profile` / `model_type` / `exclude_pulse_id` 関連コードが削除された
- [ ] Track Chronicle 本体が動作: Metabolism 連動の `_generate_track_chronicle` で Track 別生成 / head 入れ替え / 切り替え時 history 末尾近く挿入 / 時刻アンカー (詳細 [`../track_chronicle.md`](../track_chronicle.md))
- [ ] dead code 撤去完了: `prepare_pulse_root_context` / `build_fixed_section` / `build_dynamic_section` / `pause_summary` 関連 (DB / API / Frontend)
- [ ] `track_waiting.json` / `STATUS_WAITING` / `waiting_for` / `waiting_timeout_at` 関連機構が完全に削除されている

---

## Phase 4 以降への前提条件

- Track 種別 Playbook が揃っていること → Phase 4 の MainLineScheduler が「どの Playbook を起動するか」を Handler から取れる
- ライン仕様が安定していること → Phase 4 の `on_periodic_tick` がメインライン Playbook を呼び出せる

---

## 関連ドキュメント

- [../02_mechanics.md](../02_mechanics.md) — Pulse 開始プロンプト構成 / ライン階層
- [../04_handlers.md](../04_handlers.md) — Handler / Playbook の関係
- [phase_4_pulse_scheduler.md](phase_4_pulse_scheduler.md) — Scheduler 実装
