# 機能の接点と検証範囲 (connections)

調査日: 2026-09-09 / 対象コミット: `7d7214be`
項目本体は [inventory.md](inventory.md)、全体の要約は [README.md](README.md)。

## これは何

台帳の項目どうしが、どこで情報や状態を受け渡しているかの一覧。**接点は「片方だけ検査しても
守れない場所」**なので、検査体制を設計するときは項目そのものより先にここを見る価値がある。

各行は「上流の項目 → 下流の項目 / 受け渡す情報・状態 / ソース上の根拠 / 既存検査 / 未検査の境界」
の形で書いてある。総当たりで並べたものではなく、**ソース上の呼び出しか、同じデータの読み書きが
実際にある組み合わせ**だけを、根拠つきで挙げている。

## なぜ接点を独立に見るのか — 事故 2 件がどちらも接点で起きた

- **事故 1** (大量のログを編纂したらレベル 2 のあらすじが 1 本しか生まれない) は、
  「承認した回数」(利用者に見せた数字) と「実際に生まれた本数」の接点。**両側にテストがあり、
  突き合わせる検査だけが無かった。**
- **事故 2** (大量の未整理履歴をスルースにも全量読ませた) は、読み戻し (`MEM-16`) が開いた量が
  そのままスルース (`MEM-17`) の入力になる接点。**読み戻し側のテストとスルース側のテストは
  別々に存在し、同じ走行で繋ぐテストだけが無かった** (`tests/test_window_refill.py` と
  `tests/test_sluice.py`。後者は窓の中身を偽物に差し替えているので、量は検査対象ですらない)。

どちらも「部品は緑、繋ぎ目が無検査」という同じ形をしている。だから接点の一覧は、事故の再発を
測る物差しとしても使える。

## 領域をまたぐ大きな流れ

各領域の接点表に加えて、領域をまたぐ流れを 6 本挙げておく。詳細は各領域の該当行にある。

1. **取り込み → 編纂 → 読み戻し → 会話** (B → B → B → A)
   他サービスからの取り込みが生ログを作り、編纂があらすじの階層を作り、読み戻しが窓を埋め、
   会話がそれを LLM へ送る。事故 2 件はどちらもこの線上にある。
2. **世界の出来事 → 建物の記録 → ペルソナの記憶** (C → C → B)
   移動・アイテム操作・Observer の通知が建物の記録になり、そのうち「誰が聞いたか」に名前がある行だけが
   ペルソナの記憶へ写る。**この選別を固定するテストが無い** (領域 C の検査の穴 3)。
3. **ペルソナ設定の変更 → コンテキストの構成 → 会話** (D → A → A)
   モデル・水位・スペルの設定が、次の送信の中身を変える。どの変更がいつ効くか (即時か次の Session か)
   は項目ごとに違う。
4. **自動処理の発火 → 記憶への書き込み** (E → B)
   Metabolism・冷えた窓の見張り・アラーム・キャッシュ保温が、画面を見ていない間に LLM を呼び、
   結果を記憶に残す。費用と永続化の両方が動く。
5. **更新・再起動 → 全領域** (F → all)
   起動時にバックアップ・スキーマ移行・データ補填・Playbook 同期・City 識別子の修復が走る。
   ここが失敗すると他の全部が影響を受けるのに、起動列を通した検査が一本も無い。
6. **アドオンの導入 → スペル・パネル → 会話** (F → A)
   アドオンが増やすスペルとバブル操作が、会話画面の選択肢とペルソナの持ち札を変える。
   アドオンを外すと次回起動で Playbook の孤児が削除される (領域 E の AUTO-33)。

---

## 領域 A. 会話とコンテキスト (CHAT-01〜32)


| 上流 | 下流 | 受け渡す情報・状態 | ソース根拠 | 既存検査 | 未検査の境界 |
|---|---|---|---|---|---|
| 会話 (`/chat/utter` → `send_message`) | 世界 (移動・在室) | `move_user` → `OccupancyManager.move_entity` で入室を確定してから発言 | `api/routes/chat.py:1397`、`manager/runtime.py:283-315` | `tests/test_chat_boundary_w7.py` (move_user は MagicMock) | 実際の CAS UPDATE と並行デバイスの競合 |
| 会話 (添付) | 世界 (Item) | `create_{picture,document,audio,video}_item_for_user` で建物に Item を作り、`is_open=True` で視界に入る | `api/routes/chat.py:518, 610, 684, 759` | `tests/test_attachment_paths.py` (未読) | Item の視界コンテキストへの反映 |
| 会話 (`run_meta_user` の頭) | 知覚バッファ | `DynamicStateManager.maybe_inject_event_messages` (検知) → `inject_copresence_recall` → `sai_memory.flush_perception_buffer` (消費) の順序が固定 | `sea/runtime.py:286-350` | `tests/test_perception_buffer.py` / `test_perception_call_contracts.py` / `test_copresence_recall.py` | 作業セッション (`sea/work_session.py`) は別の Pulse root で同じ頭処理を自前で持つ (片方だけ直す事故の実績がコメントにある) |
| 会話 (`run_meta_user` の頭) | 記憶 (窓・あらすじ) | 読み戻し → 最終防衛ライン → (知覚消費) → 非常畳み。床未達なら Pulse ごと見送る | `sea/runtime.py:180-403` | `tests/test_window_refill.py` / `test_window_floor.py` / `test_sluice_cold_isolation.py` | 各段の内部計算 (未読) |
| 会話 (応答後) | 記憶 (Chronicle) | `maybe_run_metabolism` → 編纂計画 → `chronicle_confirm` → 実行台帳 claim → LLM | `sea/runtime.py:405-427`、`sea/session_lifecycle.py:5686-5760` | `tests/test_arasuji_*` / `test_execution_ledger*` | ダイアログの往復と 3 つの直行条件 |
| 記憶 (冷えた起点の前進) | 会話 (スルース被覆) | パンマーカーを越える前進で `sluice_skipped_spans` へ記録 (fail-closed)。記録は `GET /api/people/{id}/sluice/...` で読めるが**フロントの画面が無い** (intent は第二段) | `sea/session_lifecycle.py:947-1000`、`api/routes/people/sluice.py`、`docs/intent/sluice_coverage_gaps.md` | `tests/test_sluice_cold_isolation.py::test_skipped_span_is_recorded_before_the_window_moves` | 記録された範囲がユーザーに届く経路 (未実装) |
| 会話 (発言の確定) | ペルソナ記憶 | `notify_speak_persisted` が唯一の発火口。DB 採番の message_id と保存後の本文が空でないことの両方を条件にする | `sea/runtime_emitters.py:20-62` | `tests/test_user_utterance_durability.py::test_the_persistence_signal_clears_the_interrupted_mark` | 4 つの保存経路 (下書き確定 / emit_say / emit_speak / tell ツール) すべてがこの口を通るかの検算 |
| 会話 (発言本文) | 他ペルソナ / 音声 | `<user_only>` は音声では中身ごと除去、他ペルソナ取り込みでは `[alt]` に置換、UI では**タグだけ**除去 | `saiverse/content_tags.py:25-64`、`frontend/src/lib/messageMarkdown.ts:24-26` | `tests/test_content_tags.py` (未読) | フロントとバックエンドの三通りの剥がし方が意図どおりか |
| 会話 (プレビュー / 送信量) | モデル定義 | 水位は三層 (モデル定義 > 全体設定 > 組み込み既定) で解決。`context-status` と実送信が同じ関数を通る | `saiverse/model_configs.py:400-428`、`api/routes/people/context_status.py:127-141` | `tests/test_metabolism_global_defaults.py` (41 本) | プレビューと実送信の一致 (非常畳みの有無、§3) |
| 会話 (アドオンバブル) | アドオン | `POST /api/addon/{addon}/{tool}` + SSE `/api/addon/events` でメタデータ変化を検知 | `frontend/src/components/AddonBubbleButtons.tsx:229-240`、`frontend/src/hooks/useAddonEvents.ts` | `tests/test_addon_*` (未読) | addon 側の実装 (expansion_data) |
| 音声入力 (stackchan アドオン) | 会話 | `manager.handle_user_input_stream` を**直接**叩いてユーザー発言として注入する。`/chat/utter` の位置照合・冪等キー・添付処理を通らない | `expansion_data/saiverse-stackchan-addon/audio_input_relay.py:210-245` | 未確認 | この入口の位置照合・重複抑止・エラー通知が本線と同等か |
| 会話 (停止) | 自律行動 | `cancel_active_generation` は建物内の**全ペルソナ**の走行中要求を取り消す (自律 Pulse を含む) | `saiverse/saiverse_manager.py:1319-1327` | 未確認 | 巻き添えの範囲と記録内容 |
| ゲーム (Region) | 会話 | `active_game` に応じて履歴の取得先が `/api/world/regions/{id}/game/log` に切り替わる。応答者に Ruler が先頭注入される | `frontend/src/app/page.tsx:718-731`、`manager/runtime.py:496-504` | `tests/test_game_session_log.py` (未読) | 表示切替と投稿先 (常に現在 Building) の関係 |

---


---

## 領域 B. 記憶 — Chronicle / Memopedia / スルース / 取り込み (MEM-01〜41)


| 上流 | 下流 | 受け渡す情報・状態 | ソース根拠 | 既存検査 | 未検査の境界 |
|---|---|---|---|---|---|
| インポート (MEM-33/34/35) | Chronicle 補修 (MEM-11) | 挿入されたメッセージが「未被覆」として補修の対象件数になる | `import_chatlog.py` → memory.db `messages` → `estimate.py:estimate_chronicle_generation_cost` が `_processed_ids` の差で数える | なし (取り込み → 補修を一本で通す試験を見つけられなかった) | **取り込み完了から Chronicle 化への導線が無い** (`docs/issues/import_flow_lacks_chronicle_cta.md`) |
| 整列計画 (`alignment.plan_alignment`) | 束ね (MEM-13) | `plan.chunks` の `coverage_chars` と時刻が `extra_leaves` として dry 予測に載る | `session_lifecycle.py:5606-5620`, `estimate.py:214-233` | `tests/test_arasuji_bands.py::TestPlanProperties` (純関数) | 大量チャンクでの dry 予測と実行の一致は 30 本規模の 1 本のみ (`TestFoldMaterialCap`) |
| 束ね (MEM-13) | 一次あらすじのプロンプト文脈 | 確定済みあらすじを新しい側から 20 件辿るとき、遠い過去がレベル2 以上で読める | `context.get_episode_context_for_timerange`、`arasuji_levels.md §3-2` | `tests/test_arasuji_interleaved_consolidation.py`(実物 3 点を接続) | **CLI 経路 (`build_arasuji_core.py`) は `after_chunk` を渡していない** → 大量編纂で後半チャンクが階層を見られない (§3 ③) |
| 読み戻し (MEM-16) | スルース (MEM-17/18) | 開いた窓がそのまま `_prepare_context` を通ってスルースの LLM 入力になる | `session_lifecycle.py:4738`(判定) / `sluice.py:2017`(`seen_ids = presented_ids`) | `tests/test_sluice_cold_isolation.py`(冷たい形での両者の接続) | 判定の主語 (未見の担当範囲) と送信量の主語 (窓全体) が違う (MEM-18 の疑義) |
| スルース (MEM-17) | コア記憶 (MEM-03) | `confirmed=0` の自動採取。ユーザーの confirm/edit で 1 になる | `sea/sluice.py:_apply_core_ops` → `sai_memory/core_memory.add_core_memory(confirmed=0)` | `tests/test_sluice.py` | 未確認バッジの置き場所が未確定 (`in_flight.md:66`) |
| スルース (MEM-17/19) | 手帳 (MEM-05) | メモの `origin` (live/readback/mechanism) と `event_date` (機械刻印) | `sea/sluice.py:_apply_memos` → `sai_memory/memory/pocketbook.add_memo` | `tests/test_sluice.py`, `test_sluice_capture.py` | 手帳の訂正の口が v0.3 に無い (`pocketbook.py:16`) |
| スルース (MEM-17) | タスク帳 / 約束 | `offered_tasks` の revision を CAS の照合値に使う | `sea/sluice.py:1094 _apply_promises` | `tests/test_sluice.py` | タスク帳側の更新経路 (中央 DB) は未追跡 |
| Chronicle 生成 (MEM-13/14) | Memopedia / Fragment (MEM-27) | チャンク確定ごとに `batch_callback` が発火し、抽出失敗は付箋へ | `session_lifecycle.py:5993`, `:6363` | `tests/test_metabolism_two_layer.py::ExtractionBacklogRecoveryPointTest` | 付箋にも残せなかった分の回収 (自動では拾い直されない) |
| Chronicle 帯 | 会話のコンテキスト | `get_episode_context` が予算 (既定 2 万字) 内で帯を組む。予算解決は ペルソナ列 > env > 既定 | `arasuji.py:254-303 _resolve_band_budget`, `sai_memory/arasuji/context.py` | `tests/test_chronicle_char_budget_resolution.py` | 帯の実寸が予算を超える経路 (診断で切り分ける設計、MEM-21) |
| メッセージ削除 (MEM-01) | Chronicle の source_ids | 削除は Chronicle を触らない。孤児参照は補修経路の `_sweep_dead_message_sources` が掃く | `absorption.py`, `session_lifecycle.py:5497` | `tests/test_arasuji_absorption.py`(機構 F) | 削除の時点で利用者に何も伝わらない |
| Chronicle 削除 (MEM-08) | 実行台帳 (MEM-41) | completed 行が再編纂を塞ぐ → `supersede_completed` で退避 | `execution_ledger §11.2`、handoff ① | 特定できていない | — |
| 記憶 (この領域) | 部屋の様子・知覚 | 水位の「上限」は知覚込みの合計で測る。知覚は「残す量」を消費しない | `arasuji_levels.md §9`「二数の主語」、`session_lifecycle.py:2815-2831` | `tests/test_metabolism_two_layer.py::InjectedPerceptionAccountingTest` | 知覚の供給側 (部屋の様子の太さ) は別領域 |
| 記憶 (この領域) | スペル | `memory_read/open/close/search/write/clip/delete` が地図帳を触る | `landscape.md §5`、`saiverse/memory_atlas.py` | `tests/test_pocketbook_spells.py` ほか | スペル領域の担当と重なるため未追跡 |
| 判断点 (自律) | Memopedia 編纂 (MEM-28) | day_close の approve が `curation_plans` へ積む | `judgment_finalize.py:858` | — | **`AUTONOMOUS_DRIVING_SHIPPED = False` で発火しない** |

---


---

## 領域 C. 世界 — City / Building / Item / 移動 / Observer / Phenomena (WORLD-01〜44)


| 上流 | 下流 | 受け渡す情報・状態 | ソース根拠 | 既存検査 | 未検査の境界 |
|---|---|---|---|---|---|
| 移動 (`move_entity`) | 建物ログ (`building_messages`) | 退室/入室の host 行 2 本。`heard_by` = 移動後の在室者、`event_key` = 移動ごとの採番 ID | `saiverse/occupancy_manager.py:498-583` → `database/building_messages.insert_building_message_in_session` | `tests/test_move_entity_ledger.py`, `tests/test_building_messages_db.py` | ユーザーの画面でこの 2 本がどう見えるか (フロントの描画) |
| 建物ログ | ペルソナ記憶 (SAIMemory) | `heard_by` に自分が入っている未読行を転記。occupancy 行と user 由来の行は**既読マークのみで転記しない** | `builtin_data/tools/get_building_messages.py:173-283, 306-432` | `tests/test_building_ingest_m8.py` | heard_by を空で書いた host 行 (アイテム note / Observer 通知) が誰にも届かないこと |
| 建物ログ | ペルソナ記憶の読み進み位置 | `pulse_cursors[building_id]`。記録が無い部屋は `manager.startup_seq_watermark` (起動時の末尾) から開始。水位に無い部屋 (起動後に作られた部屋) は 0 から | `builtin_data/tools/get_building_messages.py:339-372`, `manager/initialization.py:203-260` | `tests/test_building_ingest_m8.py` | **起動時の水位測定に失敗したとき (全部屋 0)、部屋の履歴を全部読み込む経路が残っている** (`manager/initialization.py:246-263` は startup_alerts を出すだけ) |
| 移動 (`move_entity`) | 知覚バッファ | `move.post_dynamic_state` outbox → 移動の事実の差分通知 + 同室者への入室通知 + 部屋の様子の束 (`perception.room_state`) | `saiverse/occupancy_manager.py:583-618`, `saiverse/dynamic_state.py:86-260` | `tests/test_move_entity_ledger.py::test_dynamic_state_handler_guards`, `tests/test_room_state_diff.py` | 遅延配送が古い部屋の様子を積む問題は `docs/issues/entry_delivery_retry_duplicates_room_perception.md` に起票済み (未解決) |
| 部屋のアイテム | 部屋の様子 (知覚) | `build_room_bundle(building_id)` がアイテム・設置物・在室者の外見・内装画像を束ねる | `saiverse/dynamic_state.py:195-200` → `builtin_data/tools/get_visual_context.py` | `tests/test_room_state_diff.py`, `tests/test_visual_context_feed_stand.py` | **アイテム数に上限が無く、部屋の様子が数万字になる** (`docs/issues/room_items_uncapped.md`、設計待ち) |
| Building 設定 | ペルソナの head / 部屋の様子 | `building.base_system_instruction` が `get_visual_context` と `head_pipeline/sections/building.py` へ渡る | `builtin_data/tools/get_visual_context.py:561`, `sea/head_pipeline/sections/building.py:34` | `tests/test_head_pipeline_building.py` | `building.system_instruction` (アイテム一覧を差し込む先) の読み手が見つからない (WORLD-07) |
| 移動 (`move_entity`) | 物理機体 (Vessel) | 入室イベントの `metadata.event.building_info` に `physical_vessel_id` と `system_prompt` を載せる | `saiverse/occupancy_manager.py:941-957` | 該当テストなし | `available_tools` は常に空リスト (A-3-c 未実装のまま) |
| 移動 (`move_entity`) | アドオン | `move.post_addon_hooks` outbox → `persona_exited_building` / `persona_entered_building` フック | `saiverse/occupancy_manager.py:598-608`, `:718-737` (legacy 経路) | `tests/test_move_entity_ledger.py` | アドオン側のフック実装 (リポジトリ外) |
| 移動 (`move_entity`) | ゲーム | `move.post_game_lifecycle` outbox → `GameLifecycleService.on_entity_moved` (パーティー再集結・自動再開) | `saiverse/occupancy_manager.py:609-614` | `tests/test_move_entity_ledger.py::test_game_lifecycle_handler_calls_on_entity_moved` | — |
| 移動 (`move_entity`) | Phenomena | `USER_MOVE` / `PERSONA_MOVE` トリガー。ただし `RuntimeService` を通る経路だけ | `manager/runtime.py:321-350` | 該当テストなし | game lifecycle など `move_entity` を直呼びする経路ではトリガーが出ない |
| アイテム操作 | ペルソナのイベントログ | `record_persona_event` (本人) + `broadcast_item_event` (同室の他者) | `manager/items.py:307-311, 348-355` | 該当テストなし | `record_persona_event` から head / 知覚への流れ (記憶領域の担当) |
| チャットの添付 | 建物のアイテム | `create_{picture,document,audio,video}_item_for_user` → `is_open=True` で Building 所有 | `api/routes/chat.py:483-880`, `manager/items.py:998-1293` | `tests/test_attachment_paths.py` | 添付が部屋に残り続けることの利用者への告知 |
| アイテム / メディア | LLM (課金) | 説明が空のとき `ensure_image_summary` / `ensure_document_summary` が LLM を呼ぶ | `api/routes/world.py:539-563`, `saiverse/media_summary.py:109-190` | `tests/test_attachment_paths.py::test_sync_summary_on_generates_synchronously` (patch で LLM を差し替え) | ワールドエディタからのアイテム作成でこの課金が起きることの告知 |
| Observer | 建物ログ | 閾値超過の host 行 (`event_type: "observer_alert"`) | `saiverse/observer_manager.py:707-728` | 該当テストなし | `heard_by` が空なのでペルソナ記憶へ届かない (WORLD-38) |
| Fixture | 部屋の様子 / 右サイドバー | `get_building_fixtures` → `/api/info/details` の `fixtures` 欄と `build_room_bundle` | `api/routes/info.py:200-217`, `tests/test_visual_context_feed_stand.py` | `tests/test_visual_context_feed_stand.py` (fake observer_manager) | 実物 ObserverManager 経由の描画 |
| RSS フィード | Fixture / Observer | フィード購読を Fixture が持ち、Observer 機構で取得する | `api/routes/feeds.py`, `saiverse/feed_manager.py` | `tests/test_feeds_api.py` (実物 ObserverManager) | 領域 C の担当外として詳細を読んでいない |
| `saiverse://` URI | ファイルシステム | `resolve_media_uri` がファイル名を素で結合し、`_resolve_document` が読み出す | `saiverse/media_utils.py:64-88`, `saiverse/uri_resolver.py:862-878` | `tests/test_attachment_paths.py` (正常系のみ) | **パス構文 (`..`) を含む URI の扱い (WORLD-32)** |
| Building 削除 | 建物ログ / Fixture / アイテム / binding | 削除しない (孤児行が残る)。FK は宣言のみで `PRAGMA foreign_keys=ON` が見つからない | `manager/admin.py:461-508`, `database/models.py:870, 907` | 該当テストなし | 孤児行の掃除経路の有無 |
| 世界の状態 | 検分 CLI | `scripts/inspect_world.py` が読み取り専用で世界を検分する | CLAUDE.md「Inspect a running world」 | `tests/test_inspect_world.py` | — |
| 世界の状態 | スナップショット / バックアップ | `scripts/snapshot.py`、`database/backup.py` | `tests/test_audit_second_batch_world.py` | 同左 (実 sqlite) | — |

### 事故二件との接続

- **事故 2 (大量の未整理履歴をコンテキストへ、スルースにも全量)** の「大量」の供給源はこの領域にある。建物ログ (`building_messages`) からペルソナの記憶へ流れる量を決めているのは、`heard_by` (誰に届くか) と `pulse_cursors` / `startup_seq_watermark` (どこから読むか) の 2 つだけで、**1 ラウンドで転記する件数の上限は無い** (`builtin_data/tools/get_building_messages.py:381-432` は候補を全件ループする)。読み進み位置の記録が失われた場合の落とし所は「起動時の末尾」だが、その水位の測定自体が失敗すると全部屋 0 に倒れる (`manager/initialization.py:246-263`)。この境界に「量が閾値を超えたら知らせる/止める」という検査は見当たらなかった。ここが棚卸しで検査対象になるべき箇所。
- **事故 1 (大量の未編纂ログの編纂)** の上流も同じ。部屋のアイテムが 58 個で部屋の様子が 35,000 字になる (`docs/issues/room_items_uncapped.md`) のように、**世界側には「量が増え続ける器」が複数あり、上限も警告も持っていない** (部屋のアイテム、建物ログ、添付ファイル)。量の検査は下流 (記憶・編纂) にしか置かれていない。

---


---

## 領域 D. ペルソナと利用者の設定・導入導線 (PERS-01〜29)


| 上流 | 下流 | 受け渡す情報・状態 | ソース根拠 | 既存検査 | 未検査の境界 |
|---|---|---|---|---|---|
| ペルソナ設定 (`PATCH /config`) | LLM クライアント (`llm_clients/`) | `DEFAULT_MODEL` / `LIGHTWEIGHT_MODEL` の変更で `get_llm_client` を呼び直す。失敗は `[WARNING:LLM]` としてメッセージに埋め込まれ、フロントが alert する | `manager/admin.py:1355-1412`, `SettingsModal.tsx:386-390` | `tests/test_admin_ai_edit_contract.py` (ただし `personas={}` でこのブロックを通さない) | クライアント再生成の失敗経路が実 API で何を返すか |
| ペルソナ設定 (chat UI 上書き) | `AdminService.update_ai` | グローバル上書き (`state.model`) がある間は、DB の `DEFAULT_MODEL` を書いてもインメモリのモデルは上書き値を保つ | `manager/admin.py:1356-1365` | 未検査 | 上書き解除時に DB 値へ戻る経路 |
| ペルソナ作成 | SAIMemory (`~/.saiverse/personas/<id>/`) | AIID がそのままフォルダ名になる。大文字小文字を畳んだ重複検査はここが根拠 | `manager/persona.py:482-521` | `tests/test_persona_creation_wiring.py` (ID 採番のみ。ディレクトリ作成は検査せず) | 実際の memory.db 生成タイミング |
| ペルソナ削除 | SAIMemory / `persona_schedule` / `persona_task` ほか | **何も渡さない** (削除経路が触るのは `ai` / `building_occupancy_log` / `task_book` のみ) | `manager/admin.py:1431-1482` | `tests/test_task_book.py:548-563` (task_book のみ) | 残る 19 本の FK 参照表と persona ディレクトリ |
| ペルソナ削除 | ScheduleManager | 残った `persona_schedule` 行が起動時に再登録され、発火時に `"persona not found"` で failed になる | `saiverse/schedule_manager.py:156-183, 1161-1164` | 未検査 | 失敗が実行台帳でどう精算されるか |
| アラーム UI | ScheduleManager → PulseDispatcher | `PersonaSchedule` 行 (種別・時刻・META_PLAYBOOK・PLAYBOOK_PARAMS)。発火は LLM 課金を伴う | `schedule.py:107-146`, `schedule_manager.py:1153-1189` | `tests/test_schedule_api_sync.py` ほか 4 本 | 非判断点発火が自律ゲートを通らないこと |
| `AUTONOMY_ENABLED` (設定) | `autonomy_wiring.is_autonomy_on` | v0.3 は止め具 (`AUTONOMOUS_DRIVING_SHIPPED=False`) が先に効くので設定値は届かない | `autonomy_wiring.py:199-224` | `tests/test_v03_autonomy_gate.py` | UI (ワールドエディタ) → 設定値 → 駆動 の通し |
| チュートリアル ステップ5 | `.env` / `os.environ` / `manager` | 6 つのモデルロール env var + 基準モデル。ペルソナ行の `DEFAULT_MODEL` は書き換えない | `api/routes/tutorial.py:552-562` | 未検査 | `write_env_updates` の実体、既存ペルソナへの波及 |
| チュートリアル ステップ4 | ペルソナ作成 (PERS-01) | `PersonaWizard` を embedded で開き、`createdPersonaId` / `createdRoomId` を受け取る | `TutorialWizard.tsx:412-421`, `StepPersonaChoice.tsx` | 未検査 | 途中コース (startAtStep=5,6) で persona が null のまま進む経路 |
| チュートリアル ステップ7 | ペルソナ設定 (`PATCH /config`) | `chronicle_enabled` のみを送る | `TutorialWizard.tsx:340-351` | 未検査 | 同上 |
| ユーザープロフィール | Building 表示名 | 表示名を変えると全 City の `user_room_<slug>` の名前を書き換える | `api/routes/user.py:229-243` | 未検査 | ユーザーが部屋名を自分で変えていた場合の上書き |
| LLM 呼び出し全般 | `llm_usage_log` → 使用状況ページ / RPD 表示 | model_id・トークン・コスト・通貨・カテゴリ | `saiverse/usage_tracker.py:53-124` | `tests/test_usage_tracker.py` (直接呼び出しのみ) | 実呼び出し経路からの記帳 (既知の欠落 2 issue) |
| SAIMemory | 点クリップ表示 / Pulse タイムライン | `messages.id` / `clips` / `pulse_id` | `api/routes/people/life.py:100-117`, `pulse_timeline.py` | `tests/test_life_view_api.py` (clips のみ) | pulse-timeline / pulse-logs の読み出し |
| 起動時の版アップグレード | `AI.LAST_KNOWN_VERSION` | 新規作成時は現行版を刻んで、直後にアップグレード通知が出ないようにする | `database/models.py:140-143` | 未確認 | `saiverse/upgrade_handlers.py` の実処理 (別領域) |

---


---

## 領域 E. 画面のない自動処理 (AUTO-01〜34)


| 上流 | 下流 | 受け渡す情報・状態 | ソース根拠 | 既存検査 | 未検査の境界 |
|---|---|---|---|---|---|
| ScheduleManager (アラーム発火) | PulseDispatcher → PulseController → SEARuntime | `<system>` プロンプト + `META_PLAYBOOK` + args + pre_spells | `saiverse/schedule_manager.py:1155-1200` | `tests/test_schedule_dispatch_outcome.py` (dispatcher は差し替え) | 実 Pulse から先 (発言・記憶書き込み・課金) |
| ScheduleManager (判断点スケジュール) | `autonomy_wiring.handle_scheduled_judgment` → 判断点 | 判断点 kind + `PLAYBOOK_PARAMS` (予算・life_mode_override) | `saiverse/schedule_manager.py:1130-1152` | `tests/test_autonomy_wiring.py::test_schedule_manager_routes_judgment_playbooks` | v0.3 ではゲートで止まる先 |
| Phenomena (`inject_persona_event`) | `autonomy_wiring.handle_external_event` → Pulse | イベント本文・`event_type`・dispatch envelope | `builtin_data/phenomena/inject_persona_event.py:172-186` | `tests/test_autonomy_wiring.py` 実イベント節 | 実際に撃つ Phenomenon ルールの存在 |
| EventScheduler | ScheduleManager / FeedManager / ObserverManager / SessionLifecycle / user_conversation / execution_ledger_wiring / AutonomyManager / SDS | 発火時刻と callback。**予約はインメモリ・再起動で消える** | `saiverse/event_scheduler.py:94-104` と各 `schedule*` 呼び出し | `tests/test_event_scheduler.py` | 「全予約が再起動で正しく再確立されるか」の横断検査 |
| SessionLifecycle (冷えたウィンドウ見張り) | Chronicle 編纂 + スルース (LLM) | ペルソナ・model・提示ウィンドウ | `sea/session_lifecycle.py:4317-4338` | 部分的 (`test_perception_call_contracts.py`) | 見張りが実際に走らせる編纂の全経路 |
| LLM 呼び出しの成功 (全経路) | `touch_anchor_after_llm_call` → `schedule_cache_ttl_pulse` → 保温 LLM | anchor・model・usage | `sea/session_lifecycle.py:1373-1440`、`:1524-1610` | `tests/test_cache_keepalive.py` (LLM クライアント差し替え) | v0.3 の実環境で保温が何回走るか |
| `autonomy_wiring` | 実行台帳 (`execution_ledger`) | 判断点の claim / 席取り / outcome / dispatch envelope | `saiverse/autonomy_wiring.py:372-392`、`:846-874` | `tests/test_autonomy_wiring.py` の台帳節 | 台帳の配送ハンドラが記憶へ書く先 |
| 実行台帳の回復 tick | ScheduleManager `_reconcile_schedules` | schedule の世代照合と再登録 | `saiverse/execution_ledger_wiring.py:293-297` | `tests/test_schedule_reconciliation.py` | — |
| ObserverManager (閾値超過) | Building メッセージ (`host` ロール) → ペルソナの記憶 | 通知文・`observer_alert` メタデータ | `saiverse/observer_manager.py:707-728` | なし | 建物ログからペルソナの記憶への写り方 |
| FeedManager | 知覚バッファ (kind="feed") | 記事の最新 N 件 | `saiverse/feed_manager.py:822-840` | `tests/test_feed_intake.py` | 知覚がコンテキストに載る所 (記憶領域) |
| `main.py` 起動シーケンス | SAIVerseManager / DB / バックアップ | DB パス・City 名・runtime marker | `main.py:296-556` | **なし (統合テストが無い)** | 起動の順序そのもの |
| `SAIVerseManager.start()` | 全背景ループ | 構築/起動分離の不変条件 | `saiverse/saiverse_manager.py:476-570` | ソース文字列一致 1 本のみ | どの順で何が立つかの実行検査 |

---


---

## 領域 F. 導入・運用・拡張・外部連携 (OPS-01〜35)


| 上流 | 下流 | 受け渡す情報・状態 | ソース根拠 | 既存検査 | 未検査の境界 |
|---|---|---|---|---|---|
| 更新エンジン (OPS-03) | 世界スナップショット (OPS-05) | pull の直前に `snapshot.py save` をサブプロセスで呼ぶ。名前・note・制限時間 3600 秒 | `scripts/update_engine.py:815-840` | `tests/test_snapshot_exclusions.py::test_pre_update_snapshot_*` (3 本) | 実サイズの世界での所要時間 |
| 更新エンジン (OPS-03) | 依存関係 (`requirements.lock`) | `pip install -r requirements.lock` + `pip check` | `scripts/update_engine.py:926-960` | `tests/test_requirements_lock_contract.py` (6 本、文字列検査) | 実際の pip 実行 |
| 更新エンジン (OPS-03) | アドオン (OPS-19) | 共有 venv。lock の更新がアドオンの依存を壊しても警告だけで進む | `scripts/update_engine.py:873-925` | なし | アドオン側が壊れたことを利用者がどこで知るか |
| 起動 (OPS-02) | 更新エンジン (OPS-03) | `--check-complete` の終了コード 0/10/11 | `start.bat:31-33`, `start.sh:41-53` | Python 側のみ (`tests/test_update_completion_marker.py`) | シェル分岐の実行 |
| 起動 (`main.py`) | DB 移行 (OPS-07) / バックアップ (OPS-06) / バージョンアップグレード | `pre_upgrade` バックアップ → migration → backfill 15 本 → `run_startup_upgrade` → 起動時バックアップ | `main.py:352-474` | 個別移行のテストのみ | 順序依存の不変条件 |
| セットアップ (OPS-01) | DB 初期化 (OPS-08) | `city` テーブルの読み取り可否だけで `seed.py --force` を発火 | `setup.bat:170-196`, `setup.sh:57-84` | なし | 破損 DB がこの条件に当たる実例 |
| プロバイダ設定 (OPS-14) | モデル設定 (OPS-15) | プロバイダ保存時にモデルも必ず再解決する (base_url / api_key_env をモデルが取り込むため) | `saiverse/provider_configs.py:131-143,186-187` | `tests/test_provider_configs.py::TestResolveProviderRef` (6 本) | API 層を通した再解決 |
| モデル設定 (OPS-15) | LLM クライアント生成 | `get_llm_client(model, provider, context_length, config)` が `protocol` で分岐 | `llm_clients/factory.py:160-395` | `tests/test_model_configs.py` (設定側のみ) | 実 API 呼び出し |
| 環境変数編集 (OPS-10) | 全ペルソナの LLM クライアント | キー系の変更で `persona._llm_client = None` を全ペルソナに書き、media summary クライアントも破棄 | `api/routes/admin.py:123-141` | なし | ペルソナが会話中に破棄されたときの挙動 |
| チュートリアル (`/api/tutorial/auto-configure-models`) | 環境変数 (OPS-10) + モデルロール (OPS-16) | 検出したプロバイダに応じて 6 つのロール env を `.env` へ書き、`manager.update_default_model()` を呼ぶ | `api/routes/tutorial.py:489-575` | なし | 既存ユーザーが再実行したときの上書き範囲 |
| 開発者モード (OPS-17) | ペルソナの自律行動 | `AI.AUTONOMY_ENABLED` を DB ごと一括 False + AutonomyManager 同期 | `api/routes/config.py:518-546` | なし | 全部。issue として未解決 |
| アドオン有効化 (OPS-20) | MCP (OPS-24) / Integration / server hook / composite action | 4 系統に個別通知。無効化のみ最大 5 秒待つ | `api/routes/addon.py:451-520` | `tests/test_addon_toggle_propagation.py`, `test_addon_config_mcp_reconnect.py` | 4 通知のいずれかが失敗したときの画面表示 |
| アドオンカタログ (OPS-19) | フロントエンドのビルド (OPS-23) | `ui/Panel.tsx` は `predev`/`prebuild` でコピーされる。`restart_required` はこれを見ていない | `api/routes/addon_catalog.py:143-144` vs `frontend/package.json:11-12` | なし | UI パネルだけのアドオンを入れたときの利用者体験 |
| アドオン (OPS-19/20) | ペルソナの発話・記憶 | `speak_hook` / tools / playbooks 経由。**本調査の担当外** | `saiverse/addon_hooks.py`, `expansion_data/*/speak_hook.py` | アドオン側 (opt-in、`conftest.py:29-38`) | アドオンが発話・記憶に書く内容の検査 (別調査が必要) |
| OAuth (OPS-25) | 持ち主認証 (OPS-30) | `/api/oauth/callback/` は owner 認証の例外パス | `api/owner_auth.py:58-61` | なし | LAN 公開時にこの例外が何を通すか |
| MCP 直接呼び出し (OPS-24) | 物理デバイス (Vessel / stackchan) | `POST /api/mcp/tool-call` は `visible:false` の管理系ツールも叩ける | `api/routes/mcp.py:163-230` | なし | 全部 |
| 管理 API (OPS-09/10/11/12) | 持ち主認証 (OPS-30) | ループバック起動では認証も Origin 検査も無い | `main.py:674-677`, `api/owner_auth.py` | なし | `docs/issues/api_state_changing_routes_have_no_origin_check.md` (未着手) |
| Unity ゲートウェイ (OPS-28) | ペルソナのツール (`control_body`) | `manager.unity_gateway` 経由で emote / behavior を送る | `builtin_data/tools/control_body.py:44-107` | なし | Unity クライアントが無いときの挙動 |
| SDS (OPS-29) | inter-city API サーバー (OPS-35) | `START_IN_ONLINE_MODE` が真なら登録し、他 City の `api_base_url` を `cities_config` に持つ | `manager/initialization.py:123-131`, `manager/sds.py:64-100` | `tests/test_multi_city_freeze.py` (封鎖側のみ) | SDS 登録が凍結対象外である理由 |
| 管理 API (`/api/admin/backfill-item-descriptions`) | LLM 課金 | `manager.backfill_item_descriptions()` が picture item の説明を一括生成する (`dry_run` あり) | `api/routes/admin.py:194-208` | なし | 実行時の課金規模。`dry_run=false` が既定 |
| リリース (OPS-31) | セットアップ (OPS-01) | ZIP → 展開 → `setup.bat` の `git init` + `git reset origin/main` | `.github/workflows/release.yml:16`, `setup.bat:256-263` | なし | ZIP 由来チェックアウトで更新が通るか (README の約束) |

---

