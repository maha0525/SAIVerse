# Playbook カタログ

`builtin_data/playbooks/public/` に同梱される Playbook の一覧（手書き・用途別）。概念は [concepts/playbook.md](../concepts/playbook.md)、作り方は [開発者ガイド: Playbook 作成](../developer-guide/creating-playbooks.md) を参照。

**フラグの意味**:
- `router_callable`（rc）= メタ判断・`run_playbook` / `exec` から呼び出せる
- `can_run_as_child`（child）= サブライン（`/run_playbook` / subplay `line="sub"`）として起動でき、`report_to_parent` を親に返す
- `user_selectable`（usel）= UI でユーザーがメタ Playbook として選べる

> 新規追加・改名時はここも更新する（[CLAUDE.md > Documentation Maintenance]）。正確な定義は各 JSON を参照。

## メタ判断（自律制御の中枢）

MetaLayer が Track/persona 状態から決定論的に選ぶ（→ [concepts/meta-judgment.md](../concepts/meta-judgment.md)）。dispatch される状況別5つは構造化出力ベース。

| Playbook | 表示名 | 用途 |
|---|---|---|
| `meta_judgment_running` | メタ判断 (running) | running Track があるとき。`continue/pause/complete/abort` を選ぶ |
| `meta_judgment_idle_pending` | メタ判断 (idle + pending) | アイドル + 保留 Track あり。`activate` / `create` を選ぶ |
| `meta_judgment_idle_empty` | メタ判断 (idle, no pending) | アイドル + 保留なし。新規 Track を必ず立てる |
| `meta_judgment_alert` | メタ判断 (alert) | alert 状態の Track があるとき |
| `meta_judgment_life_purpose` | メタ判断 (生きる目的の設定) | LIFE_PURPOSE 未設定時。他より先に起動し目的をドラフト |
| `meta_judgment` | メタ判断 | base（NL 独白 + `/spell`）。dispatch マップには含まれない別系統 |

## 判断点（自律行動 v2 の意思決定層）

`saiverse/judgment_points.py` の `run_judgment_point` が起動する（→ [intent/persona_cognition/judgment_points.md](../intent/persona_cognition/judgment_points.md)）。メタ判断と同じ様式: 構造化出力 + 動的 enum 注入 + `judgment_finalize` ツールでの検証・適用（メインキャッシュへの JSON 非混入）。

| Playbook | 表示名 | 用途 |
|---|---|---|
| `judgment_day_open` | 起床判断 (day_open) | 今日の時間割の編成 + 予算配分 + 欲求→関心の昇格 |
| `judgment_post_conversation` | 会話終了判断 (post_conversation) | 会話からの収穫（picked_tasks は origin_quote 必須の接地）+ 中断中セッションの扱い + 残り時間割の整え |
| `judgment_post_session` | セッション終了判断 (post_session) | タスクの裁定（done は実在成果物 ref 必須の接地検証つき）+ 残り時間割の整え |
| `judgment_on_event` | イベント到着判断 (on_event) | 反応の選択（engage_now / insert_slot / note_only / ignore）。alert は engage_now のみに縮退 |
| `judgment_day_close` | 就寝判断 (day_close) | 予定 vs 実績のふりかえり + 明日の自分へのメモ + 欲求のたな卸し + ユーザーへの報告種 |

## Track メインライン

各 Track 種別の会話・行動を回すメインライン Playbook（→ [concepts/track.md](../concepts/track.md)）。

| Playbook | 表示名 | usel | 用途 |
|---|---|:--:|---|
| `track_user_conversation` | 対ユーザー会話 Track | ✓ | 対ユーザー会話。重量級 LLM で応答生成 + `track_*`/`note_*` スペル |
| `track_social` | 交流 Track | | 他ペルソナとの会話（入口は未実装） |
| `track_external` | 外部通信 Track | | X / Discord / Elyth 等への通信 |

> **v1 自律系は退役済み**（2026-07-10、時間割への完全移行 — [features/autonomous-mode.md](../features/autonomous-mode.md)）: `track_autonomous` / `meta_autonomy_decision` は削除。`autonomy_creation` / `autonomy_web_research` は `builtin_data/playbooks/archive/` へ（復活時は `memory_*` 語彙で再設計）。`autonomy_memory_organization` / `fragment_organize` も archive（P4 庭仕事ワーカーへ転生予定）。

## 能力 Playbook（`run_playbook` / `exec` から呼ばれる）

すべて `rc=True child=True`。メインライン LLM が `/spell run_playbook name='...'` でサブライン起動する。

| Playbook | 表示名 | 用途 |
|---|---|---|
| `generate_image` | 画像生成 | Gemini / GPT Image / Grok で画像生成（canonical 実装例） |
| `generate_image_local` | ローカル画像生成 | ローカル ComfyUI（Anima モデル）で画像生成 |
| `web_research` | Webリサーチ | 検索 + verbatim 引用抽出。親へは厳選引用と出典のみ返す |
| `document_create` | ドキュメント作成 | テキスト内容のドキュメントアイテム作成 |
| `document_search` | ドキュメント内検索 | ドキュメント内をパターン/行番号で検索 |
| `building_move` | Building 間の移動 | City 内の Building 一覧 → 移動先選択 → 移動 |
| `create_building` | Building 作成 | 設定込みで新規 Building 作成（内装画像も任意生成） |
| `schedule_management` | スケジュール管理 | 自分のスケジュールの一覧/追加/削除 |
| `novel_writing` | 小説執筆 | Stelis スレッド内で全4章の短編を執筆 |

## サブ / 内部

ランタイムや他 Playbook から内部的に使われる。

| Playbook | 表示名 | 用途 |
|---|---|---|
| `sub_speak` | — | 最終応答を合成して発話（統合 speak Playbook） |
| `sub_think_meta` | — | 自律ステップの内的思考を合成 |
| `meta_exec_speak` | — | Playbook を実行して結果を発話（`call_playbook` ツールが使用） |
| `meta_simple_speak` | ツール不使用（喋るだけ） | speak のみ（`usel=True`） |
| `spell_args_decider` | Spell 引数決定 | pre_spells で引数なし指定の Spell の引数を認知から決める内部 Playbook |
