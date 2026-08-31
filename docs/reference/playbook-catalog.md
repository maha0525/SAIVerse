# Playbook カタログ

`builtin_data/playbooks/public/` に同梱される Playbook の一覧（手書き・用途別）。概念は [concepts/playbook.md](../concepts/playbook.md)、作り方は [開発者ガイド: Playbook 作成](../developer-guide/creating-playbooks.md) を参照。

**フラグの意味**:
- `router_callable`（rc）= `run_playbook` / `exec` から呼び出せる
- `can_run_as_child`（child）= サブライン（`/run_playbook` / subplay `line="sub"`）として起動でき、`report_to_parent` を親に返す
- `user_selectable`（usel）= UI でユーザーがメタ Playbook として選べる

> **この表の行は `builtin_data/playbooks/public/` に実在する JSON とだけ対応させる。** 退役した Playbook は行ごと消す（「退役」と書き添えて残すと、呼び出せるものの一覧として読まれてしまう）。新規追加・改名・削除のときはここも同じコミットで直す（[CLAUDE.md > Documentation Maintenance]）。正確な定義は各 JSON を参照。

## 判断点（自律行動の意思決定層）

kind → Playbook 名の対応は `saiverse/judgment_points.py` の `JUDGMENT_PLAYBOOK_MAP` がコードで固定し、`saiverse/autonomy_wiring.py` の `fire_judgment_point` が起動する（→ [intent/persona_cognition/judgment_points.md](../intent/persona_cognition/judgment_points.md)）。**どれを走らせるかを LLM が選ぶ経路は無い** — 発火点と Playbook は 1 対 1 の決定論。

様式は共通で、構造化出力（`response_schema`）+ 動的 enum 注入 + `judgment_finalize` ツールでの検証・適用（メインキャッシュへの JSON 非混入）。4 枚とも `rc=False` / `usel=False`（ユーザーにも `run_playbook` にも開いていない）。

| Playbook | 表示名 | 用途 |
|---|---|---|
| `judgment_day_open` | 起床判断 (day_open) | 今日の時間割の編成（コマの `ref` は実在タスク `task:N`、`facility` は実在 Building の動的 enum）+ 作業ラウンドの日次予算の提示 |
| `judgment_post_session` | セッション終了判断 (post_session) | 作業セッションの裁定。`done` は**このセッションが実際に作った成果物**の ref が必須（接地検証）+ セッションの実績要約（`digest` 欄）の生成 + 残り時間割の整え |
| `judgment_on_event` | イベント到着判断 (on_event) | 反応の選択（engage_now / insert_slot / note_only / ignore）。alert イベントではスキーマが engage_now のみに縮退する |
| `judgment_day_close` | 就寝判断 (day_close) | 予定 vs 実績のふりかえり + 明日の自分へのメモ + ユーザーへの報告種 + 記憶の編纂候補・命名候補のレビュー（候補ゼロなら欄ごと出さない） |

**退役した判断点・スキーマ欄**（新しいコードから参照しないこと）:

- **会話終了判断（`judgment_post_conversation`）** — 2026-08-16 裁定で退役し、JSON も削除済み（[autonomous_behavior_v3.md](../intent/autonomous_behavior_v3.md) §8 / §13.3）。会話に切れ目は定義できないため、約束・やりたいことの捕獲は Metabolism のスルースの一手へ一本化され、待ちを閉じる帳簿処理だけが `autonomy_wiring.handle_conversation_end` に残った。
- **v1 メタ判断一式（`meta_judgment` / `meta_judgment_running` / `_idle_pending` / `_idle_empty` / `_alert` / `_life_purpose`）** — 2026-08-14 に Playbook・`_SITUATION_PLAYBOOK_MAP`・`meta_judgment_finalize` ツールごと削除（[track_retirement.md](../intent/track_retirement.md) §7.4）。生きる目的の初期設定（`meta_judgment_life_purpose`）は受け皿なしで撤去され、後継はシステムタスクの第一号として v3 §9-5 で設計中。
- **欲求・Track まわりの欄**（`promotions` / `new_desires` / `desire_reviews` / `track_op` / `episode_purposes` の一部）— 欲求プールと Track の供給源が機構ごと消えたため、スキーマから落ちている（v3 §8）。判断 Playbook の JSON 本文に退役欄名・退役 namespace（`desire:` / `track:`）が現れないことは `tests/test_judgment_playbook_prompt_contract.py` が機械検査する。

## 会話メインライン

| Playbook | 表示名 | usel | 用途 |
|---|---|:--:|---|
| `track_user_conversation` | 対ユーザー会話 Track | ✓ | **ユーザー会話の既定メインライン。名前は Track 時代の遺物で、いま Track は一切経由しない**（会話の入口は `saiverse/user_conversation.py`）。ペルソナの `META_PLAYBOOK` 既定値であり、`saiverse/upgrade_handlers.py` が削除済み Playbook 名を巻き取る先でもある |
| `track_social` | 交流 Track | | **雛形のみ — 起動経路は存在しない。** 他ペルソナとの会話を回す Handler が 2026-08-21（束 6 第三便）に削除され、この JSON を名指しするコードはゼロ |
| `track_external` | 外部通信 Track | | **雛形のみ — 起動経路は存在しない。** X / Discord / Elyth 等への通信用に置かれたが、同上 |

> **v1 自律系は退役済み**（2026-07-10、時間割への完全移行 — [features/autonomous-mode.md](../features/autonomous-mode.md)）: `track_autonomous` / `meta_autonomy_decision` は削除。`autonomy_creation` / `autonomy_web_research` は `builtin_data/playbooks/archive/` へ（復活時は `memory_*` 語彙で再設計）。`autonomy_memory_organization` / `fragment_organize` も archive（P4 庭仕事ワーカーへ転生予定）。

## 能力 Playbook（`run_playbook` / `exec` から呼ばれる）

すべて `rc=True child=True`。メインライン LLM が `/spell run_playbook name='...'` でサブライン起動する。

| Playbook | 表示名 | 用途 |
|---|---|---|
| `generate_image` | 画像生成 | Gemini / GPT Image / Grok で画像生成（canonical 実装例） |
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
| `meta_simple_speak` | ツール不使用（喋るだけ） | speak のみ（`usel=True`） |
| `spell_args_decider` | Spell 引数決定 | pre_spells で引数なし指定の Spell の引数を認知から決める内部 Playbook |
