# 棚卸しの網羅性を検算する記録 (coverage)

調査日: 2026-09-09 / 対象コミット: `7d7214be`
項目本体は [inventory.md](inventory.md)、接点は [connections.md](connections.md)、要約は [README.md](README.md)。

## これは何

**「全部見た」という宣言を、後から他人が検算するための資料。** 各領域の担当が実際に開いた入口と、
それを台帳のどの項目に対応させたか、対応させなかったものは何をなぜ除外したかを残してある。
件数だけのカバレッジ評価ではないし、ソースの全文ダンプでもない。

構成は二部。**第一部**は領域 A〜F が調べた入口の一覧 (製品の側から見た地図)。
**第二部**は既存テストの棚卸し (テストの側から見た地図)。両方を突き合わせると、
「入口はあるがテストが無い」場所が出る。

## 統合担当が独立に数えた入口の総量

領域を割り振る前に、私 (統合担当) が機械的に数えた総量を先に置く。**各領域の担当が拾った数と
この数を突き合わせることが、網羅性の一次検算になる。**

| 入口の種類 | 数 | 数え方 |
|---|---|---|
| API モジュール | 50 | `api/` 配下の非 `__init__` Python (ルート定義を持つもの) |
| API ルート (デコレータ) | 約 350 | `@router.(get\|post\|put\|delete\|patch\|websocket)` の総数 |
| 画面コンポーネント | 35 | `frontend/src/components/*.tsx` (サブディレクトリ除く) |
| 画面ページ | 5 | `frontend/src/app/**/page.tsx` + `layout.tsx` |
| CLI / スクリプト | 71 | `scripts/` 直下のエントリ |
| 起動・導入スクリプト | 11 | ルート直下の `.bat` / `.sh` |
| ペルソナが使えるツール | 77 | `builtin_data/tools/` |
| 公開 Playbook | 19 | `builtin_data/playbooks/public/` |
| 同梱モデル定義 | 142 | `builtin_data/models/` |
| 同梱プロバイダ定義 | 12 | `builtin_data/providers/` |
| フェノメノン定義 | 3 | `builtin_data/phenomena/` |
| アドオン (導入済み) | 7 | `expansion_data/` (gitignore 対象。`ls` で確認) |
| GitHub Actions | 2 | `.github/workflows/` |
| テストファイル | 312 | `tests/` 288 + `tests/sea/` 6 + `tests/llm_clients/` 4 + `discord_gateway/tests/` 14 |

参考として、文書側の総量も置く (期待の根拠を探す先)。

| 文書 | 数 |
|---|---|
| 利用者向けガイド (`docs/user-guide/`) | 10 |
| 概念リファレンス (`docs/concepts/`) | 21 |
| 自動生成を含むリファレンス (`docs/reference/`) | 8 |
| 機能解説 (`docs/features/`) | 7 |
| 設計意図 (`docs/intent/`) | 90 |
| 未解決の課題 (`docs/issues/`) | 175 (完了済み `archive/` は 69) |
| 引き継ぎ (`docs/handoff/`) | 68 |

## 領域の割り振り

6 領域 + テスト側の 1 領域で、重複を許して分担した。境界にあるもの (例: 記憶の画面は B と A の両方から
見える) は、両方が自分の観点で拾っている。

| 領域 | 担当範囲 | 項目数 |
|---|---|---|
| A | 会話とコンテキスト (送信〜返答、水位、確認ダイアログ) | 32 |
| B | 記憶 (Chronicle / Memopedia / コア記憶 / 手帳 / スルース / 取り込み) | 41 |
| C | 世界 (City / Building / Item / 移動 / Observer / Fixture / Phenomena / メディア) | 44 |
| D | ペルソナ作成と設定 / 利用者設定 / 生活 / アラーム / 導入導線 / 観測 UI | 29 |
| E | 画面のない自動処理 (自律・判断点・起動終了・バックアップ・見張り) | 34 |
| F | 導入・更新・運用・設定 (プロバイダ/モデル) / アドオン・MCP / 外部連携 / CI | 35 |
| G | 既存テストと検証手段 (第二部) | 13 の観点 |

## この調査が対象外にしたもの

- **`.worktrees/` 配下** — 別セッションの作業ツリー。根拠に引いていない。
- **アドオンの内部実装** — 本体側の接続・権限・導入・解除までを範囲とし、各アドオンの中身は
  別調査が必要な境界として領域 F に明記した。
- **実行を伴う確認すべて** — pytest、`main.py`、セットアップ・更新スクリプト、API サーバー起動。
  一度も走らせていない。
- **本番ペルソナと本番データ** — `~/.saiverse/` 配下は読み書きしていない。`.env` の値も読んでいない。
- **画面の実操作** — UI はソースから調べた。

---

## 領域 A. 会話とコンテキスト (CHAT-01〜32) — 調べた入口


| 調べた入口 (ファイル/ディレクトリ/画面) | 何を確認したか | 対応する項目 ID | 未対応の場合の理由 |
|---|---|---|---|
| `api/routes/chat.py` (全 1526 行) | 全ルート (history / send / utter / stop / continue / retry / withdraw / message-outcome / preview / permission-response / spell-confirmation-response / persona avatar) と添付処理 | CHAT-01〜11, 17, 18 | — |
| `api/routes/people/context_status.py` | 送信量の内訳・水位・畳み可否の read-only 計測 | CHAT-12 | — |
| `api/routes/people/cache_status.py` | キャッシュの残り時間とペルソナ単位のキャッシュ設定 (GET + POST cache-config) | CHAT-13 | — |
| `api/routes/people/working_memory.py` | recalled_ids の GET/POST/DELETE | CHAT-27 | — |
| `api/routes/people/realtime_spell.py` | リアルタイムスペル binding の CRUD + カタログ | CHAT-28 | — |
| `api/routes/uri.py` | `saiverse://` の解決 endpoint | CHAT-24 | — |
| `api/routes/people/summon.py` の `/spells` | ツール指定モードのスペル一覧の供給元 | CHAT-16 | — |
| `api/routes/config.py` の `/model` `/playbook` `/max-image-embeds` `/cache` `/models` | ChatOptions・ToolModeSelector が叩く設定口 | CHAT-14, 15, 16 | モデル定義編集そのもの (`/models/{key}` CRUD) は設定領域として項目化せず (別領域担当) |
| `api/routes/people/sluice.py` | 冷たいときに飛ばした範囲の一覧と後追い採取ジョブの API | 接点表に記載 | 記憶領域 (B/C) の担当と重なるため項目化せず。会話境界としてのみ記録 |
| `api/main.py` | ルーターの登録 (どの prefix がぶら下がるか) | — | 入口の網羅確認にだけ使用 |
| `manager/runtime.py` (全 1487 行) | `handle_user_input_stream` / `_stream_persona_pulse` / continue / retry / withdraw / preview_context / `_build_responding_personas` / NDJSON イベント判定 | CHAT-01, 04〜08, 11, 31, 32 | — |
| `saiverse/saiverse_manager.py` の `run_sea_user` / `cancel_active_generation` / `set_model` / `stop_all_active_generations` | 会話の実行経路と停止・モデル上書きの範囲 | CHAT-01, 04, 14 | — |
| `sea/runtime.py` `run_meta_user` / `_run_meta_user_locked` / `check_playbook_permission` / `_request_playbook_permission` | Pulse の頭の順序 (窓の手当て→知覚→取り込み→非常畳み→Playbook→応答後 Metabolism)、Playbook 権限 | CHAT-01, 17, 20, 21 | — |
| `sea/runtime_context.py` `prepare_context` / `preview_context` | 提示コンテキストの組成と preview_only の分岐 | CHAT-11, 12, 20, 22 | — |
| `sea/runtime_emitters.py` | `speak_persisted` の唯一の発火口、`<user_only>` / `<in_heart>` の剥がし | CHAT-01, 05 | — |
| `sea/runtime_llm.py` / `runtime_nodes.py` / `runtime_engine.py` | NDJSON イベント型の発火箇所 (grep で網羅) | CHAT-01 | 個々のノード実装は Playbook 領域として項目化せず |
| `sea/session_lifecycle.py` (6679 行、該当節のみ) | 水位判定・Metabolism 実行・chronicle_confirm・冷えた起点の前進とスルース飛ばし記録 | CHAT-12, 19, 20, 21 | 全文は読んでいない (grep で該当節を特定して読了) |
| `sea/sluice.py` (該当節のみ) | 後退方式の撤去・fail-closed の例外群 | 接点表 | 記憶領域の担当 |
| `saiverse/model_configs.py` L200-320 | 二族四つの水位の組み込み既定と三層解決 | CHAT-12 | — |
| `sea/mode_spell_permissions.py` | aspect 別スペル権限 (現在は表が空) | CHAT-16 | 表が空で実効なし。§4 に記載 |
| `tools/confirmation.py` | スペル確認の要求・タイムアウト・自動承認条件 | CHAT-18 | — |
| `saiverse/content_tags.py` | `<user_only>` / `<in_heart>` の三種の剥がし方 | CHAT-01 | — |
| `frontend/src/app/page.tsx` (3839 行) | 送信・ストリーム消費・復旧導線・履歴取得・ポーリング・停止・取り消し・プレビュー・描画 | CHAT-01〜11, 22, 31 | ゲーム (Region) 表示切替は世界領域。会話表示に影響する箇所だけ触れた |
| `frontend/src/components/ChatOptions.tsx` | モデル・キャッシュ・送信量・画像上限の UI | CHAT-12〜15 | — |
| `frontend/src/components/common/ContextVolumeBar.tsx` | 送信量の横棒と内訳表示の実装 | CHAT-12 | — |
| `frontend/src/components/ContextPreviewModal.tsx` | トークン内訳・費用推定・知覚バッジ | CHAT-11 | — |
| `frontend/src/components/SpellConfirmDialog.tsx` | 120 秒の自動キャンセル・編集・文字数上限 | CHAT-18 | — |
| `frontend/src/components/PlaybookPermissionDialog.tsx` | 60 秒の自動拒否・恒久設定の書き込み | CHAT-17 | — |
| `frontend/src/components/ChronicleConfirmDialog.tsx` | 60 秒の自動スキップ・LLM 回数の提示 | CHAT-19 | — |
| `frontend/src/components/ToolModeSelector.tsx` + `frontend/src/lib/preSpells.ts` | ツール指定モードと pre_spells の組み立て | CHAT-16 | — |
| `frontend/src/components/RightSidebar.tsx` | 在室者・アイテム・10 秒ポーリング・建物変化でのモーダル強制クローズ | CHAT-29 | 個々のモーダル (Memory/Schedule/Inventory) は別領域 |
| `frontend/src/components/ActionsPanel.tsx` | アドオンの複合アクション定義 CRUD + テスト実行 | CHAT-30 | 会話本線ではなくアドオン設定画面。存在だけ記録 |
| `frontend/src/components/AddonBubbleButtons.tsx` | 発言バブル上の音声再生・アドオン tool 起動・300 秒タイムアウト | CHAT-23 | — |
| `frontend/src/components/SaiverseLink.tsx` / `ContentViewerModal.tsx` | `saiverse://` リンクのクリックと内容表示 | CHAT-24 | — |
| `frontend/src/components/SystemAlertBanner.tsx` | システム警告の一覧・隔離ログの退避 | CHAT-25 | — |
| `frontend/src/components/ActiveClientIndicator.tsx` | アクティブタブの表示のみ (クリック不可) | CHAT-26 | — |
| `frontend/src/hooks/*` (useActivityTracker / useActiveClientTab / useAddonEvents / useClientActions) | 心拍・タブ排他・アドオン SSE | CHAT-23, 26 | useClientActions はアドオンのクライアント動作登録。会話本線に固有の分岐は見つからず |
| `frontend/src/lib/messageMarkdown.ts` / `formatCost.ts` / `clientActionRegistry.ts` | 発言本文の描画前整形 (`<user_only>` タグ除去・旧スペル結果の境界修復) | CHAT-01 | formatCost/clientActionRegistry は補助。項目化せず |
| `frontend/package.json` / `frontend` 配下のテスト探索 | フロントエンドのテスト基盤の有無 | §5 | テストランナー (jest/vitest 等) の依存も設定も無い |
| `docs/user-guide/chat-options.md` | 利用者向け説明と実装の突き合わせ | CHAT-12, 14, 15, 16 | §3 に乖離を記載 |
| `docs/overview/landscape.md` §3〜§4, §6 | Session / head / 机 / Metabolism / Anchor の概念記述 | CHAT-12, 20, 21 | §3 に乖離を記載 |
| `docs/intent/session.md` / `cached_head_architecture.md` / `perception_buffer.md` / `sluice_coverage_gaps.md` / `quick_spell.md` / `conversation_runner.md` | ステータス行と不変条件 | 各項目の「期待の根拠」 | 全文精読はしていない (冒頭のステータスと該当節) |
| `docs/handoff/2026-09-09_sluice_cold_skip_and_v0311_handoff.md` | 事故二件の記録 (読むだけ) | §5, 接点表 | — |
| `docs/reference/api-endpoints.md` | 自動生成の chat 節と実装の突き合わせ | — | 一致していた (ドリフトなし)。項目化不要 |
| `tests/test_chat_boundary_w7.py` / `test_user_utterance_durability.py` / `test_context_status.py` / `test_streaming_placeholder_salvage.py` / `test_window_refill.py` / `test_window_floor.py` / `test_eviction_plan.py` / `test_sluice_cold_isolation.py` / `test_metabolism_global_defaults.py` / `test_watermark_headroom_validation.py` ほか | 何を実コードで通し、何を差し替えているか | 各項目の「既存テスト」 | 実行はしていない |
| `expansion_data/saiverse-stackchan-addon/audio_input_relay.py` | `handle_user_input_stream` を叩くもう一つの入口 (音声入力の注入) | 接点表 | expansion_data (gitignore 対象の user pack)。会話の入口として存在だけ記録 |

---


---

## 領域 B. 記憶 — Chronicle / Memopedia / スルース / 取り込み (MEM-01〜41) — 調べた入口


| 調べた入口 (ファイル/ディレクトリ/画面) | 何を確認したか | 対応する項目 ID | 未対応の場合の理由 |
|---|---|---|---|
| `frontend/src/components/MemoryModal.tsx` | タブ構成 (browser/core_memory/pocketbook/arasuji/memopedia/experience/pulse_timeline/import/debug) と `HIDDEN_TABS` | MEM-01〜04, 05, 07〜13, 22〜26, 29, 33〜36 | — |
| `api/routes/people/__init__.py` | 記憶系ルーターの登録一覧 (memory/recall/import_chatlog/native_export_import/reembed/memopedia/arasuji/memory_notes/working_memory/storage_layers/context_status/core_memory/experience_ledger/pocketbook/sluice) | 全般 | — |
| `api/routes/people/memory.py` | スレッド一覧・メッセージ CRUD・スレッド有効化・Stelis 救出 | MEM-01, MEM-02 | — |
| `api/routes/people/core_memory.py` | 会話検索・窓プレビュー・scene 作成・一覧・ごみ箱・confirm/edit/delete/restore | MEM-03, MEM-04 | — |
| `api/routes/people/pocketbook.py` | 手帳 (activities/memos) とタスク帳の読み口 (読み取り専用) | MEM-05, MEM-06 | — |
| `api/routes/people/arasuji.py` (1599 行) | cost-estimate / stats / diagnosis / 一覧・単体・source メッセージ・fragments / 編集・削除・全削除 / regenerate / generate (compaction・repair) / cancel / latest / status | MEM-07〜MEM-14, MEM-21 | — |
| `api/routes/people/sluice.py` | skipped-spans / candidate-memos の読み口、capture ジョブ (dry・実行・cancel・status) | MEM-18, MEM-19 | — |
| `api/routes/people/memopedia.py` (1086 行) | tree/page/history/rollback/put/delete/export/import/全削除/新規/trunks/important/desk/move/unorganized/generate/build-from-logs | MEM-22〜MEM-25 | — |
| `api/routes/people/recall.py` | recall / recall-debug / unified-recall | MEM-29 | `recall-debug` は frontend から呼ばれていない (項目内に記載) |
| `api/routes/people/reembed.py` | 再埋め込みジョブと status | MEM-32 | — |
| `api/routes/people/import_chatlog.py` | ChatGPT 公式エクスポートの preview/import/status、拡張機能エクスポートの import/status | MEM-33, MEM-34 | — |
| `api/routes/people/native_export_import.py` | スレッドのネイティブ書き出し / 取り込み / preview / status | MEM-35 | — |
| `api/routes/people/experience_ledger.py` | 経験の台帳の索引・ページ (読み取り専用) | MEM-36 | — |
| `api/routes/people/memory_notes.py` | memory-notes 一覧・resolve | MEM-31 | — |
| `api/routes/people/working_memory.py` | 想起した ID の一覧・追加・削除・全消去 | MEM-30 | — |
| `api/routes/people/storage_layers.py` | 7 層ストレージのデバッグ読み口と一括削除 | MEM-37 | frontend に呼び出しなし (到達不能として記載) |
| `api/routes/people/context_status.py` | 水位と提示量の内訳 (会話 / 機構行 / 知覚) | MEM-38 | — |
| `api/routes/system.py` (quarantine 系) + `frontend/src/components/QuarantineModal.tsx` | 建物ログ (log.json) の隔離一覧・復元・削除 | MEM-39 | 対象は建物履歴で、ペルソナ memory.db ではない (項目内に明記) |
| `frontend/src/components/ChronicleConfirmDialog.tsx` | 自動編纂の同意ダイアログ (件数・LLM 回数・モデル・60 秒で自動スキップ) | MEM-14 | — |
| `frontend/src/components/memory/ArasujiViewer.tsx` (1392 行) | 補修バナー・確認モーダル・生成ボタン・ジョブ再接続・エラー文言辞書 | MEM-10〜MEM-13 | — |
| `frontend/src/components/memory/{CoreMemoryScene,PocketbookViewer,MemopediaViewer,MemopediaConversion,MemoryBrowser,MemoryImport,MemoryRecall,ExperienceLedgerViewer}.tsx` | 各タブの操作面 | 対応項目に記載 | — |
| `frontend/src/components/memory/{MemoryNotesViewer,WorkingMemoryViewer,PulseLogsViewer}.tsx` | どこからも import されていない | MEM-30, MEM-31 | 画面に出る道が無い (項目内に記載) |
| `frontend/src/components/PersonaMenu.tsx` | 「溜まった会話をあらすじにまとめる」の入口 (mode=compaction と同じ背景ジョブ) | MEM-10 | — |
| `sea/session_lifecycle.py` (6679 行) | `generate_chronicle` / `_run_metabolism_locked` / 非常畳み / 読み戻し / anchor 解決 / スルース飛ばし / 内訳の記録 | MEM-10〜MEM-18, MEM-20 | — |
| `sea/sluice.py` (3749 行) | `run_sluice` / 適用 (コア記憶・メモ・約束) / `run_sluice_capture` (機構モード・本人モード) / `plan_sluice_capture` / `get_max_span_chars` | MEM-17〜MEM-19 | — |
| `sea/coverage_repair.py` | 止め線の解決 (`resolve_compile_ceiling`)・冷えた窓への印・tail rewind | MEM-11, MEM-12 | — |
| `sai_memory/arasuji/{bands,alignment,absorption,executor,estimate,storage,context,generator}.py` | 束ね計画と実行・整列計画・吸収・チャンク実行・見積もり | MEM-11〜MEM-13 | — |
| `sai_memory/{core_memory,memory/pocketbook,memopedia/*,unified_recall,curation_ops,backup,experience_ledger}.py` | 各器の読み書き | MEM-03〜06, 22〜28, 29, 36, 40 | — |
| `saiverse_memory/adapter.py`, `saiverse_memory/native_export.py` | テーブル初期化 (`sluice_skipped_spans` を含む)・ネイティブ書き出し | MEM-35, MEM-41 | — |
| `scripts/arasuji/build_arasuji_core.py` | 全量再編纂 CLI (見積もり + 吸収 + execute_plan + 束ねの呼び直し) | MEM-13 | — |
| `docs/intent/{arasuji_levels,chronicle_coverage_gaps,sluice_coverage_gaps,memopedia_body_to_fragment,gold_panning,memory_modal_legacy_tabs_retirement}.md` | 期待の根拠 (規則・予算・裁定の原文) | 各項目の「期待の根拠」 | — |
| `docs/user-guide/{memory-view,memory-migration,memopedia}.md` | 利用者向け説明とのズレ | §3 矛盾 | — |
| `docs/overview/{landscape,in_flight}.md` | §5 土地と Memory Atlas、進行中案件 | §3 矛盾、§4 | — |
| `docs/handoff/2026-09-0{8,9}_*.md` | 事故二件の記録と現況 | §5 | — |
| `tests/test_{sluice,sluice_capture,sluice_cold_isolation,arasuji_bands,arasuji_absorption,arasuji_interleaved_consolidation,coverage_repair,metabolism_two_layer,arasuji_generation_status_mapping,arasuji_diagnosis_api,pocketbook_api,core_memory_scene_api,...}.py` | 何を実コードで通し何を差し替えているか | 各項目の「既存テスト」 | — |
| `sai_memory/{room_state,perception_buffer}.py` | 記憶 DB 内に同居するが、部屋の様子・知覚の領分 | — | 領域 B の担当外 (別領域の担当と重なるため触れない) |
| `sai_memory/{clips,desk,theme_pages,purpose_tags}.py` | 存在と役割だけ確認 (クリップ・机・テーマページ・目的タグ) | — | 独立した利用者操作の入口を frontend に見つけられなかったため項目化せず。スペル (memory_clip/memory_open) 経由の面は領域「スペル」と重なる |

---


---

## 領域 C. 世界 — City / Building / Item / 移動 / Observer / Phenomena (WORLD-01〜44) — 調べた入口


| 調べた入口 (ファイル/ディレクトリ/画面) | 何を確認したか | 対応する項目 ID | 未対応の場合の理由 |
|---|---|---|---|
| `api/routes/world.py` (48 ルート) | 全ルートの署名と委譲先。City/Building/Region/Game/AI/Blueprint/Tool/Item/Playbook/RealtimeSpell の 10 群 | WORLD-01〜10, 18〜23, 41, 42 | Playbook 群 (7 ルート) は領域 C の担当外 (SEA 側) と判断し項目化せず。ここに同居している事実だけ §3 に残す |
| `api/routes/info.py` | `/details` `/city-map` `/item/*` `/models` の実装。fixture 欄・occupants 分離・legacy パス復旧 | WORLD-11, 12, 24, 25, 26 | `/models` はモデル設定領域なので項目化せず |
| `api/routes/observer.py` | push 認証 / Fixture CRUD / Observer CRUD / メトリクス参照 | WORLD-34〜37 | — |
| `api/routes/phenomena.py` | ルール CRUD + `available` / `triggers` | WORLD-39 | — |
| `api/routes/media.py` | upload 5 種 + serve 4 種、上限値、ffmpeg 依存 | WORLD-29, 30 | — |
| `api/routes/people/inventory.py` | 持ち物一覧 (読み取り専用) | WORLD-27 | — |
| `api/routes/people/summon.py` | summonable / summon / dismiss / spells / meta_playbooks | WORLD-15 | `spells` `meta_playbooks` は Spell/Playbook 領域なので項目化せず |
| `api/routes/user.py` | `/move` の CAS と 409 | WORLD-14 | 他 (`/status` `/me` 等) は領域外 |
| `api/routes/uri.py` + `saiverse/uri_resolver.py` | `saiverse://` の解決経路、global scheme 6 種 | WORLD-33 | message/memopedia/chronicle は記憶領域の担当 |
| `api/routes/system.py` (quarantine 3 ルート) | 隔離一覧・復元・リセット | WORLD-43 | — |
| `api/routes/chat.py` (utter / 添付) | 発言契機入室、添付 → Item 化 | WORLD-13, 31 | チャット本体は別領域 |
| `api/file_safety.py` + `saiverse/file_policy.py` | 許可ルート、ファイル名検査、アップロード上限 | WORLD-32 | — |
| `saiverse/occupancy_manager.py` (957 行) | move_entity の全経路、CAS、拒否条件 5 種、host イベント生成 | WORLD-14〜17 | — |
| `manager/admin.py` (City/Building/Region/Item/AI CRUD) | 削除時の検査と取り残し | WORLD-01〜08, 18, 22, 23 | — |
| `manager/runtime.py` (summon / end_conversation / _move_persona) | ペルソナ移動の入口 | WORLD-15 | — |
| `manager/items.py` (ItemService, 2000 行超) | pickup/place/use、bag、slot、記録の宛先 | WORLD-23, 28 | — |
| `manager/history.py` | `add_building_event` の heard_by 既定、log.json バックアップ関数 | WORLD-16, 43 | — |
| `manager/initialization.py` | 建物履歴の起動時初期化、起動時水位、旧 log.json 取り込み、`_quarantine_building` | WORLD-43, 接点表 | — |
| `builtin_data/tools/get_building_messages.py` | 建物ログ → ペルソナ記憶の転記規律 (host 行の扱い) | WORLD-16 | — |
| `saiverse/dynamic_state.py` | 入室時の知覚注入 3 段 | WORLD-16 | — |
| `saiverse/observer_manager.py` (728 行) | Fixture/Observer 作成・pull 実行・push 記録・通知 | WORLD-34〜38 | — |
| `phenomena/` (`__init__` / `core` / `manager` / `triggers`) | レジストリ、トリガー種別 11、発火経路 | WORLD-39, 40 | — |
| `builtin_data/phenomena/` | `example_log` / `inject_persona_event` の 2 本のみ | WORLD-40 | — |
| `saiverse/media_utils.py` | 保存先ディレクトリ、`resolve_media_uri` | WORLD-29, 33 | — |
| `saiverse/media_summary.py` | アイテム説明の自動生成が LLM を呼ぶこと | WORLD-22, 31 | — |
| `frontend/src/components/CityMap.tsx` (1063 行) | 表示・パン/ズーム・編集モード・背景・表示名編集・Region 潜り | WORLD-03, 04, 09, 11 | — |
| `frontend/src/components/BuildingSettingsModal.tsx` | 編集項目、ツール紐付け、事前実行スペル | WORLD-07, 10 | — |
| `frontend/src/components/ItemModal.tsx` | 名前/説明/本文の編集、bag 表示 | WORLD-23, 24, 25 | — |
| `frontend/src/components/InventoryModal.tsx` (99 行) | 読み取り専用の一覧 | WORLD-27 | — |
| `frontend/src/components/RightSidebar.tsx` (FixtureModal は同ファイル内 555 行〜) | 在室者・アイテム・設置物の表示、toggle-open、設置物モーダル | WORLD-12, 26, 37 | grep で `FixtureModal.tsx` は存在せず RightSidebar 内の関数と確認 |
| `frontend/src/components/PeopleModal.tsx` | 召喚・解散の入口 | WORLD-15 | — |
| `frontend/src/components/Sidebar.tsx` | 場所一覧・Region 折り畳み・Building 新規作成・閲覧切替 | WORLD-06, 13, 19 | — |
| `frontend/src/app/phenomena/page.tsx` (411 行) | ルール管理画面。開発者モードでのみ導線が出る | WORLD-39 | — |
| `frontend/src/components/settings/WorldEditor.tsx` (899 行) | サブタブ 7 種と各 CRUD | WORLD-01〜08, 22, 23, 41, 42 | — |
| `frontend/src/components/QuarantineModal.tsx` | 隔離の復元/リセット UI | WORLD-43 | — |
| `docs/user-guide/{world-editor,world-view,city-map,items-and-files}.md` | 利用者向けの期待 | 各項目の「期待の根拠」 | — |
| `docs/intent/{city_identity,region,observer,room_state_packages}.md` | 不変条件とステータス | 各項目の「期待の根拠」 | — |
| `docs/reference/saiverse-uri.md` | URI 表 | WORLD-33 | — |
| `docs/reference/api-endpoints.md` | world 節 48 行 = 実装と一致していた | §3 | ドリフト無しなので項目化せず |
| `docs/overview/landscape.md` §2/§4/§8/§9 | 世界の構成・Phenomena・凍結 | §3, §4 | — |
| `docs/overview/in_flight.md` | 進行中案件に世界系がほぼ無いこと | §3 | — |
| `docs/issues/` (world 関連 12 件) | 既知の負債 | §3, §5 | — |
| `tests/` の world 関連 20 ファイル | 何を実コードで通し、何を fake にしているか | 各項目の「既存テスト」 | — |
| `builtin_data/tools/` (move_persona / create_building / list_city_buildings / item_* / observer_read / resolve_uri / invoke_phenomenon) | ペルソナ側の世界操作の入口と spell 可視性 | WORLD-15, 28, 33, 37, 40 | `game_*` 4 本は WORLD-21 に含めた |
| `builtin_data/playbooks/public/{building_move,create_building}_playbook.json` | `router_callable` でペルソナから届くこと | WORLD-15, 06 | — |
| `database/models.py` (Building/Item/ItemLocation/BuildingMessage/Fixture/ObserverConfig/Region) | 列と FK、cascade の有無 | WORLD-08, 34 | — |

---


---

## 領域 D. ペルソナと利用者の設定・導入導線 (PERS-01〜29) — 調べた入口


| 調べた入口 (ファイル/ディレクトリ/画面) | 何を確認したか | 対応する項目 ID | 未対応の場合の理由 |
|---|---|---|---|
| `api/routes/people/__init__.py` | ペルソナ一覧 `GET /api/people/` の実装とサブルーターの構成。作成・削除はここに無い | PERS-03 | — |
| `api/routes/people/config.py` | ペルソナ設定の GET/PATCH。旧 organize-memory 撤去のコメント | PERS-05, PERS-26 | — |
| `api/routes/people/life.py` | 残っているのは `/clips` のみ。profile-tree / day-plan は退役と明記 | PERS-11, PERS-10 | — |
| `api/routes/people/schedule.py` | スケジュール CRUD、世代 bump、`scheduler_synced` | PERS-08 | — |
| `api/routes/people/pulse_logs.py` | `GET /{id}/pulse-logs`, `/{id}/pulse-logs/{pulse_id}` | PERS-23 | — |
| `api/routes/people/pulse_timeline.py` | messages を pulse_id で束ねる可視化 API (先頭 60 行のみ精読) | PERS-24 | 詳細な gap 計算ロジックは未読 (項目の判定に不要) |
| `api/routes/people/debug.py` | Embedding 一括生成 / Memopedia 本文→Fragment 変換 (下見・実行・取消) | PERS-21, PERS-22 | — |
| `api/routes/people/summon.py` | summonable / summon / dismiss / spells / meta_playbooks | PERS-04 | — |
| `api/routes/people/utils.py` | persona_id の検証、`get_adapter`、`ensure_persona_exists` | PERS-23 の根拠 | 単体の機能ではないため項目化せず |
| `api/routes/people/models.py` | `AIConfigResponse` / `UpdateAIConfigRequest` / `ScheduleItem` | PERS-05, PERS-08 | — |
| `api/routes/people/realtime_spell.py` | エンドポイント一覧のみ確認 (4 本) | PERS-27 | 実行時の注入経路は領域外 (会話コンテキスト) |
| `api/routes/user.py` | status / move / buildings / me(PATCH) / heartbeat / visibility / list | PERS-12, PERS-13 | — |
| `api/routes/tutorial.py` | status / complete / reset / api-keys / available-models / auto-configure-models / model-roles | PERS-14, PERS-15 | — |
| `api/routes/info.py` | `/details` の occupants (autonomy_enabled のみ返す)、`/models` | PERS-28 | item / city-map は領域外 |
| `api/routes/usage.py` | summary / daily / by-persona / models / personas / categories / by-category / rpd | PERS-17, PERS-18 | — |
| `api/routes/feeds.py` | エンドポイント一覧のみ (presets / fixtures / subscriptions / fetch / items) | — | RSS フィード施設は世界モデル側の領域。設定画面に「フィード」タブがある事実のみ PERS-25 に記載 |
| `api/routes/system.py` | `/announcements` (外部 Gist 取得)、`/version`、`/alerts` | PERS-20 | quarantine 系は領域外 |
| `api/routes/config.py` (announcements-monitor のみ) | トグルの読み書き先が `manager.state.announcements_enabled` | PERS-20 | 他の config は領域外 |
| `api/routes/world.py` (AI 系のみ) | `POST/PUT/DELETE /api/world/ais`、`AICreate`/`AIUpdate` | PERS-01, PERS-02, PERS-07 | Building/Region/Blueprint は領域外 |
| `manager/admin.py` (AI 管理節) | `get_ai_details` / `create_ai` / `update_ai` / `delete_ai` の実体 | PERS-01, PERS-05, PERS-07 | — |
| `manager/persona.py` | `_create_persona` の実体、`delete_ai` の複製定義 | PERS-01, PERS-07 | — |
| `manager/runtime.py` | `get_summonable_personas` (ruler 除外あり) | PERS-04 | — |
| `manager/initialization.py` | 既定モデルの解決 (`SAIVERSE_DEFAULT_MODEL` → builtin) | PERS-01 | — |
| `persona/core.py` | `lightweight_llm_client` の遅延生成、`autonomy_enabled` / `current_building_id` の実名 | PERS-01, PERS-06 | 全体は未読 (必要属性のみ確認) |
| `persona/tasks/storage.py` | 値型のみ。per-persona tasks.db は廃止済みと明記 | PERS-29 | — |
| `saiverse/autonomy_wiring.py` | `AUTONOMOUS_DRIVING_SHIPPED = False`、`is_autonomy_on` | PERS-06 | 判断点の中身は領域外 |
| `saiverse/schedule_manager.py` | start / register_schedule / `_execute_schedule` の判断点分岐 | PERS-09 | 実行台帳・reconciliation の詳細は未読 |
| `saiverse/usage_tracker.py` | `record_usage` / `record_cache_storage` / `_flush_to_db` | PERS-19 | — |
| `saiverse/task_book.py` | `purge_persona_entries` の docstring | PERS-07 | — |
| `database/models.py` (AI / PersonaSchedule / LLMUsageLog / UserSettings / PersonaTask) | 列と既定値、`ForeignKey("ai.AIID")` の本数 | PERS-01, PERS-06, PERS-07, PERS-08, PERS-19 | — |
| `frontend/.../PersonaWizard.tsx` | 3 ステップ。モデル設定なし。ID 自動採番 | PERS-01 | — |
| `frontend/.../PersonaMenu.tsx` | Return to Room / Memory / Inventory / Alarm / あらすじまとめ / Settings | PERS-04, PERS-05 | — |
| `frontend/.../PeopleModal.tsx` | 呼び出し / 帰ってもらう の 2 タブ | PERS-04 | — |
| `frontend/.../SettingsModal.tsx` | ペルソナ設定の全項目。自律トグルは非表示・state のみ往復 | PERS-05, PERS-06 | — |
| `frontend/.../ScheduleModal.tsx` | アラーム管理。meta_playbook は送らない | PERS-08 | — |
| `frontend/.../UserProfileModal.tsx` | 表示名 / メール / アバター | PERS-12 | — |
| `frontend/.../GlobalSettingsModal.tsx` | タブ 8 種 (env/world/feeds/models/modelMgmt/playbooks/about/utilities) | PERS-25 | 各タブの中身は他領域 |
| `frontend/.../settings/WorldEditor.tsx` (AIs タブ) | ペルソナの作成 / 編集 / 削除 / 移動。自律トグルあり | PERS-02, PERS-06, PERS-07 | 他のサブタブは領域外 |
| `frontend/.../settings/` の他 4 パネル | ファイル名のみ確認 (Feed/Model/Provider/CodexLogin) | — | モデル・プロバイダ管理は別領域 |
| `frontend/.../tutorial/TutorialWizard.tsx` + steps 全 9 本 | 8 ステップの遷移、保存先 API、スキップ挙動 | PERS-14, PERS-15 | — |
| `frontend/.../tutorial/TutorialSelectModal.tsx` | 4 コース (full/persona/api_keys/models) | PERS-16 | — |
| `frontend/.../DebugPanel.tsx` | ボタンは Embedding 一括生成の 1 本のみ | PERS-21 | — |
| `frontend/.../Sidebar.tsx` | システム欄 (お知らせ / API使用状況 / チュートリアル / アドオン)、フッタ (設定 / プロフィール) | PERS-16, PERS-17, PERS-20, PERS-25 | — |
| `frontend/src/app/usage/page.tsx` | summary / daily / personas / categories / by-category を叩く | PERS-17 | — |
| `frontend/src/app/announcements/page.tsx` | `/api/system/announcements` を表示、既読ハッシュを localStorage へ | PERS-20 | — |
| `frontend/src/app/page.tsx` (チュートリアル・RPD 部分のみ) | 初回起動時のチュートリアル自動表示、RPD 取得 | PERS-14, PERS-18 | 他は他領域 |
| `frontend/.../memory/PulseLogsViewer.tsx` | どこからも import されていない | PERS-23 | — |
| `frontend/.../memory/MemopediaConversion.tsx` | debug の変換 API を叩く唯一の画面 | PERS-22 | 中身は記憶領域と重なるため呼び出し確認のみ |
| `docs/user-guide/persona-settings.md` | ACTIVITY_STATE を現役として説明 | 矛盾 §3 | — |
| `docs/user-guide/global-settings.md` | タブ一覧が実装と食い違う | 矛盾 §3 | — |
| `docs/user-guide/world-editor.md` | AIs タブに「アクティビティ状態」 | 矛盾 §3 | — |
| `docs/features/autonomous-mode.md` | 自律の駆動を現役として説明 | 矛盾 §3 | — |
| `docs/intent/life.md` | 凍結 (2026-08-23) | PERS-10 | — |
| `docs/intent/autonomous_behavior_v3.md` §11.1 | 止め具の設計根拠 | PERS-06 | — |
| `docs/intent/persona_activity_view.md` | ライフビュー intent (間隔設定 UI は退役) | PERS-10 | — |
| `docs/overview/roadmap_status.md` §0/§1/§6 | 導入導線の現況、止め具の記載 | PERS-14 | — |
| `docs/concepts/persona.md` | AUTONOMY_ENABLED の説明 (正しい) | 矛盾 §3 の対比 | — |
| `docs/issues/entity_creation_has_no_transactional_boundary.md` | 作成経路の一貫性欠如 (未着手) | PERS-01 | — |
| `docs/issues/llm_usage_accounting_gaps.md` / `usage_tracking_gaps.md` | 記帳漏れの既知課題 | PERS-19 | — |
| `tests/conftest.py` | autouse で `AUTONOMOUS_DRIVING_SHIPPED=True` に差し替え | PERS-06 | — |
| `tests/test_v03_autonomy_gate.py` | 止め具そのものの回帰 | PERS-06 | — |
| `tests/test_persona_creation_wiring.py` | `_create_persona` の配線 | PERS-01 | — |
| `tests/test_admin_ai_edit_contract.py` | `update_ai` / `get_ai_details` の往復契約 | PERS-05 | — |
| `tests/test_schedule_api_sync.py` ほか schedule 系 5 本 | 世代 bump / 同期 / 発火分類 / 台帳 | PERS-08, PERS-09 | 個々の assert は表題と docstring まで |
| `tests/test_life_*.py` (4 本) | ライフ確定・点クリップ・暮らし欄の撤去回帰 | PERS-10, PERS-11, PERS-28 | — |
| `tests/test_usage_tracker.py` | 未設定時の drop 挙動ほか | PERS-19 | — |
| `tests/test_task_book.py::(削除経路)` | `purge_persona_entries` の呼び出し前提 | PERS-07 | — |
| `tests/` 全体の grep (tutorial / user/me / usage API / summonable) | 該当テストの有無 | §5 | — |

---


---

## 領域 E. 画面のない自動処理 (AUTO-01〜34) — 調べた入口


| 調べた入口 (ファイル/ディレクトリ/画面) | 何を確認したか | 対応する項目 ID | 未対応の場合の理由 |
|---|---|---|---|
| `saiverse/autonomy_wiring.py` (1765 行) | 止め具・ゲート・判断点の起動・watchdog・会話終了・実イベント・仲裁 | AUTO-01〜04, 07, 08 | — |
| `saiverse/judgment_points.py` (2150 行) 冒頭 + kind 定義 | 判断点 4 種と Playbook 対応表・起動配線を持たない設計 | AUTO-02 | 個々の状況テキスト組成・スキーマ生成は領域 E の粒度を超えるので未読 |
| `saiverse/autonomy_manager.py` | watchdog tick の器・既定 50 分・env 上書き | AUTO-03 | — |
| `saiverse/saiverse_manager.py` `start()` / `shutdown()` / `ensure_autonomy_for` / `stop_autonomy` / `_on_persona_registered` / `__init__` の背景ループ節 | 背景ループの起動順・構築/起動分離の不変条件・終了順 | AUTO-06, 24, 27, 32 | 世界・アイテム・建物系のメソッドは他領域 |
| `sea/pulse_controller.py` | 優先度・割り込み・キュー上限・再開キュー | AUTO-31 | — |
| `sea/runtime.py` `run_cache_keepalive` | 保温 LLM コールのゲートと連鎖 | AUTO-13 | runtime の Playbook 実行本体は他領域 |
| `saiverse/event_scheduler.py` | min-heap + Condition の push 駆動・`schedule_periodic` の例外契約・仮想クロック | AUTO-06 | — |
| `saiverse/schedule_manager.py` (1268 行) | アラームの登録・発火・台帳・再試行・判断点への分岐 | AUTO-05 | 世代照合・occurrence token の詳細は読んだが項目化せず (AUTO-05 に内包) |
| `api/routes/people/schedule.py` + `frontend/src/components/ScheduleModal.tsx` | アラームの作成口が v0.3 の画面に出ていること | AUTO-05 | — |
| `saiverse/day_plan.py` (5618 行) の予約部分 (`_slot_fire_at` / `_push_slot` / `_fire_slot_by_id`) | 時間割のコマ予約と発火の入口 | AUTO-04 | 5618 行全体は読んでいない。時間割の編成ロジックは止め具で走らないため優先度を下げた |
| `sea/work_session.py` 冒頭 / `saiverse/slot_close.py` 冒頭 | コマ発火の先 (予算付き作業セッション + 締めの一手) | AUTO-04 | 本体は未読 (止め具で到達しない) |
| `saiverse/execution_ledger_wiring.py` | 起動時回復・60 秒掃除 tick・reconciliation | AUTO-12 | `execution_ledger.py` 本体 (台帳の状態機械) は他領域寄り |
| `sea/session_lifecycle.py` (6000 行超) の自動発火部分 | 保温予約・冷えたウィンドウ見張り・Metabolism 発火判定・スルースの飛ばし・束ねループ | AUTO-13〜17 | 窓の再構成・読み戻し・fold 計算は記憶領域の担当 |
| `sai_memory/arasuji/bands.py` `run_band_overflow` | 1 呼び出し 3 件の安全弁 | AUTO-17 | 整列計画本体は記憶領域 |
| `sea/sluice.py` の env 読み | 飛ばしの閾値・圧力弁・全体トグル | AUTO-16 | スルース本体は記憶領域 |
| `saiverse/observer_manager.py` | pull observer の定期実行・閾値通知・push 受信 | AUTO-10 | metric 記録の JSON1 詳細は項目化せず |
| `saiverse/feed_manager.py` (1833 行) | 定期取得・サイクル予算・配送・剪定 | AUTO-11 | フィード解析・記事保存の詳細は他領域 |
| `phenomena/manager.py` + `phenomena/triggers.py` + `builtin_data/phenomena/inject_persona_event.py` | トリガー種別・非同期ワーカー・実イベントの Pulse 起動 | AUTO-08, 09 | — |
| `saiverse/integration_manager.py` + `saiverse/integrations/` | 30 秒 tick + 統合ごとの poll 間隔。**同梱の統合実装はゼロ (base のみ)** | AUTO-29 | — |
| `main.py` 全体 | 起動順序・DB 工事・バックアップ・終了経路 | AUTO-18〜23, 27, 33, 34 | — |
| `api/main.py` | ルータ登録のみ。lifespan は `main.py` 側 | — | 自動処理を持たない (登録だけ) |
| `database/backup.py` | 起動バックアップ・pre_upgrade バックアップ・剪定・復元 | AUTO-18 | 復元 (`restore_saiverse_db_backup`) は手動操作なので項目化せず |
| `saiverse_memory/adapter.py` の起動バックアップ | ペルソナごとの別スレッドバックアップ | AUTO-25 | adapter 本体は記憶領域 |
| `saiverse/runtime_marker.py` | 二重起動の見張りと fail-closed | AUTO-21 | — |
| `manager/initialization.py` `_init_city_config` | City 識別子の自動修復と 2 つの関所 | AUTO-22 | — |
| `saiverse/data_paths.py` `migrate_legacy_user_data` | repo 内 user_data の移送 | AUTO-23 | — |
| `saiverse/playbook_sync.py` | 起動時のファイル→DB 同期と孤児削除 | AUTO-33 | — |
| `manager/sds.py` | SDS heartbeat の間隔と backoff | AUTO-28 | 既定オフなので浅く |
| `llm_clients/llama_server.py` idle checker | 待機時間でのサーバー停止 | AUTO-30 | 起動側は他領域 |
| `saiverse/conversation_manager.py` / `saiverse/remote_persona_proxy.py` | no-op / 凍結の実体 | AUTO-32 | — |
| `api/routes/people/debug.py` | 保守操作の口 (埋め込み一括生成ほか)。自律系の口は削除済み | AUTO-26 | — |
| `saiverse/game_lifecycle.py` | 自動アーカイブ・移動フック。**周期処理は持たない** (移動イベント駆動) | — | 定期実行・自動発火が無いので項目化せず。移動フック自体は世界領域 |
| `saiverse/day_simulator.py` / `saiverse/day_scenario.py` | 仮想クロックの DES ドライバ。本番の背景スレッドは動かない | AUTO-06 に注記 | 開発者専用 (スクリプト経由) |
| `docs/overview/landscape.md` §3/§8/§9 | 駆動の期待像・凍結・死んだ概念 | 全体 | — |
| `docs/overview/roadmap_status.md` §0〜§3 | v0.3 中心軸と止め具の位置づけ | AUTO-01 | — |
| `docs/intent/autonomous_behavior_v3.md` §11 / §11.1 | 止め具の裁定原文 (まはー裁定として本文に記載) | AUTO-01 | — |
| `docs/intent/sluice_coverage_gaps.md` / `chronicle_coverage_gaps.md` 冒頭 | 事故 2 件の設計側の受け皿 | AUTO-16, 17 | 第二段は未起草 |
| `docs/intent/observer.md` | Observer の設計と実装の差 | AUTO-10 | — |
| `docs/reference/environment-vars.md` | 本領域の env の記載有無 | 矛盾 §3-6 | — |
| `tests/conftest.py` | 全テストで止め具を外す autouse 固定具 | AUTO-01 | — |
| `tests/test_v03_autonomy_gate.py` ほか自律系テスト群 | 何を実コードで通し、何を差し替えているか | AUTO-01〜03 | — |

---


---

## 領域 F. 導入・運用・拡張・外部連携 (OPS-01〜35) — 調べた入口


| 調べた入口 (ファイル/ディレクトリ/画面) | 何を確認したか | 対応する項目 ID | 未対応の場合の理由 |
|---|---|---|---|
| `setup.bat` / `setup.sh` | 全 13 ステップの内容と両者の差分。破壊的分岐 (seed) の条件 | OPS-01, OPS-08, OPS-32, OPS-33 | — |
| `start.bat` / `start.sh` | 更新自己回復の分岐、City 引数、SearXNG 起動条件、frontend の build/start | OPS-02 | — |
| `start-dev.bat` / `start-dev.sh` | dev モード。更新検査を持たないこと | OPS-02 | — |
| `start_dev.sh` / `start_dev.ps1` | 中身が旧世代 (Gradio / port 7860 / conda) | OPS-02 §4 | 現行の起動経路ではないので凍結・遺物として §4 に記載 |
| `update.bat` / `update.sh` / `scripts/update_from_github.ps1` / `scripts/self_update.py` | 4 入口すべてが `scripts/update_engine.py` に集約されていること | OPS-03 | — |
| `scripts/update_engine.py` (1177 行) | 全フェーズ (git 検査 → snapshot → pull → pip/npm → 完了印)、rollback、`--check-complete` の 3 終了コード | OPS-03, OPS-04, OPS-05 | — |
| `snapshot.bat` / `snapshot.sh` / `scripts/snapshot.py` | save/list/inspect/restore/delete、除外集合、稼働中拒否、復元前自動 snapshot | OPS-05 | — |
| `database/seed.py` | `--force` の挙動、確認プロンプト、`.bak` バックアップ、消える範囲 | OPS-08 | — |
| `database/migrate.py` (2484 行) | CLI 引数 (`--db` / `--force`)、バックアップ→再構築→失敗時戻し | OPS-07 | 個々の backfill 関数 60 本超の中身は追い切っていない (§追跡できていない境界) |
| `database/backup.py` | 起動時バックアップ、integrity_check、世代整理、restore CLI | OPS-06 | — |
| `database/api_server.py` | inter-city / persona-proxy が 503 で封鎖済みなこと。子プロセスとして起動され続けること | OPS-29, OPS-35 | — |
| `main.py` (CLI / 起動シーケンス / FastAPI 構築) | 引数、LAN モードの門、起動時 migration・upgrade・backup、子プロセス起動、CORS、owner auth の差し込み条件 | OPS-02, OPS-07, OPS-30, OPS-34, OPS-35 | — |
| `api/routes/db_manager.py` | 4 ルート。任意テーブルの read/upsert/delete | OPS-09 | — |
| `api/routes/admin.py` | `.env` の読み書き、再起動、item description の一括生成 | OPS-10, OPS-11 | `backfill-item-descriptions` は LLM 課金を伴うので接点表に記載 |
| `api/routes/system.py` | version/announcements/alerts/quarantine×3/legacy-log/update の 8 ルート | OPS-04, OPS-12, OPS-13 | — |
| `api/routes/providers.py` | 9 ルート。作成制限、builtin 上書き、削除、接続テスト、reload | OPS-14 | — |
| `api/routes/config.py` (40 ルート) | 目的別に 3 群 (モデルファイル CRUD / モデルロール・パラメータ / トグル類) に整理 | OPS-15, OPS-16, OPS-17 | 個別ルート 40 本の逐条は書かない (依頼の許可どおり利用目的でまとめ、根拠行を添えた) |
| `api/routes/addon.py` (16 ルート) | 有効/無効、global/persona 設定、ファイル添付パラメータ | OPS-20 | — |
| `api/routes/addon_catalog.py` | registry / installed / install / update / uninstall (SSE) / debug | OPS-19 | — |
| `api/routes/addon_actions.py` | アクション定義 CRUD と test 実行 | OPS-21 | アドオン内部のアクション実装は担当外 (境界を §追跡できていない境界 に明記) |
| `api/routes/addon_events.py` | 常設 SSE | OPS-22 | — |
| `api/routes/mcp.py` | servers/tools/failures/reconnect/stop/retry/tool-call | OPS-24 | — |
| `api/routes/oauth.py` + `saiverse/oauth/handler.py` | start/callback/status/disconnect、state と PKCE、トークン保存先 | OPS-25 | — |
| `api/routes/codex_auth.py` + `llm_clients/openai_codex_auth.py` (入口のみ) | デバイスコードログインの 5 ルート | OPS-18 | トークンストアの内部実装までは追っていない |
| `api/owner_auth.py` | OwnerAuthMiddleware の有効条件と検査内容 | OPS-30 | — |
| `saiverse/model_configs.py` / `model_defaults.py` / `provider_configs.py` | 3 層読み込み、`provider_ref` 継承、source スタンプ、保存/削除 | OPS-14, OPS-15, OPS-26 | — |
| `saiverse/data_paths.py` | 3 層ヘルパー 6 種の形の違い | OPS-26 | — |
| `saiverse/addon_installer.py` / `addon_registry.py` / `addon_loader.py` / `addon_paths.py` / `addon_deps.py` | clone/checkout/setup steps、registry の署名検証、router/integration/hook の登録 | OPS-19, OPS-20 | アドオン各々の内部実装は担当外 |
| `tools/mcp_config.py` | MCP 設定の 4 層探索と addon プレフィックス | OPS-24, OPS-26 | `tools/mcp_client.py` の接続ライフサイクル内部は追っていない |
| `frontend/src/components/GlobalSettingsModal.tsx` | タブ構成 (8 個) と各タブが叩く API | OPS-16 | — |
| `frontend/src/components/settings/{Provider,Model}*Panel.tsx` / `*EditorModal.tsx` | 存在と接続先。詳細な入力検証は未読 | OPS-14, OPS-15 | UI 内部の検証ロジックは別領域 (UI) の担当と判断 |
| `frontend/src/components/AddonManagerModal.tsx` / `AddonCatalogPanel.tsx` / `AddonInstallProgressDialog.tsx` / `AddonActionConfirmDialog.tsx` / `MCPSection.tsx` / `OAuthFlowSection.tsx` | タブ (installed/catalog)、確認ダイアログ、delete_data チェックボックス、SSE 受信 | OPS-19, OPS-20, OPS-24, OPS-25 | — |
| `frontend/src/addon-panels/` + `frontend/scripts/sync-addon-panels.mjs` | アドオン UI の取り込みがビルド時コピーであること | OPS-23 | — |
| `frontend/next.config.ts` / `frontend/package.json` | `/api` の rewrite 先、dev の `-H 0.0.0.0` | OPS-30 | — |
| `discord_gateway/` (integration/config/runtime/orchestrator の入口) | 有効化条件、必要な外部 relay、設定変数 | OPS-27 | bot 側 (`discord_gateway/bot/`) の実装詳細は未読 |
| `unity_gateway/server.py` / `protocol.py` / `unity_client/` | 既定 ON、bind 先、handshake、user_speak の呼び出し先 | OPS-28 | Unity 側プロジェクト (`unity_client/`) は gitignore 済みで配布物に含まれない |
| `sds_server.py` / `manager/sds.py` / `manager/visitors.py` | SDS 登録・heartbeat が凍結対象に含まれていないこと | OPS-29 | — |
| `.github/workflows/release.yml` / `discord_gateway.yml` / `.github/FUNDING.yml` | ワークフローは 2 本だけ。本体テストを回す CI は無いこと | OPS-31 | — |
| `.gitattributes` / `VERSION` / `CHANGELOG.md` / `scripts/set_version.py` | 配布 ZIP の中身に影響する設定、版の刻印 | OPS-31 | `set_version.py` の中身は未読 (版刻印のみ) |
| `expansion_data/` の実体 (`ls`) | 7 アドオンが導入済み。gitignored なので Grep では出ない | OPS-19, OPS-20, OPS-26 | — |
| `docs/` の期待側 (README / getting-started / user-guide / reference / intent / overview) | 実装との突き合わせ | §3 に集約 | — |
| `tests/` 内の領域 F 関連 23 ファイル | 何を実コードで通し、何を mock しているか | 各項目の「既存テスト」欄 | — |
| `conftest.py` / `pyproject.toml` の pytest 設定 | アドオンテストの opt-in、`SAIVERSE_HOME` の隔離が **無い** こと | §5 | — |
| `scripts/` 全 70 ファイル (`ls` + 主要 10 本の head) | `docs/reference/scripts.md` との突き合わせ | §3 | 一回きりの移行スクリプトは領域外 (記憶・Chronicle 系は別担当) |
| `builtin_data/providers/` (12 件) / `builtin_data/models/` (142 件) | 件数とドキュメントの一致 | OPS-14 | 個々のモデル JSON の中身は未検査 |
| `sbert/` の実体 | setup の `ignore_patterns` が ONNX 版だけを落とす意図であること (バグではない) | OPS-33 | — |
| `.env` / `.env.example` / `docs/reference/environment-vars.md` | 領域 F の env 変数 16 個の記載有無 | §3 | `.env` の値そのものは読んでいない (秘密情報) |

---


---

# 第二部: 既存テストと検証手段の棚卸し (テスト側から見た地図)

> 領域 G として独立に調査した結果。領域 A〜F が「製品の入口」から見た地図であるのに対し、
> ここは「テストの側」から製品を見た地図で、両者を突き合わせると検査の穴が出る。


調査日: 2026-09-09 / 対象ブランチ: `feature/chronicle-coverage-gaps` (HEAD `7d7214be`)
**テストは一切実行していない。**「存在する」と「実行して合格した」を厳密に分けて書いた。
`ruff` の実効設定だけは `--show-settings` (読み取り専用) で実測した — その旨を該当箇所に明記する。

**テストファイル総数: 312 本** (`tests/*.py` 288 + `tests/sea/` 6 + `tests/llm_clients/` 4 + `discord_gateway/tests/` 14)。
AST で数えた `test*` 関数は 5,698 個 (parametrize 展開前。コミット `fa6eed8d` のメッセージは「フルスイート 5,850 件合格」と書いており、展開後の数はそれに近いと考えられる)。

---

## 0. テスト実行手段の事実

### 0-1. どのコマンドが何を走らせるか

| コマンド | 実体 | 根拠 |
|---|---|---|
| `python -m pytest` | `tests/`・`tests/sea/`・`tests/llm_clients/`・`discord_gateway/tests/` を並列収集 | `pyproject.toml` `[tool.pytest.ini_options]` `addopts = "-ra --ignore=temp --ignore=test_data -n auto --dist worksteal"` |
| `python -m pytest --addons` | 上記 + `expansion_data/<addon>/tests/` (既定は収集しない) | `conftest.py:29-46` `pytest_addoption` / `pytest_ignore_collect` |
| `python -m pytest ... -n 0` | 並列を切って同一プロセスで実行 | `pyproject.toml` の addopts コメント / `docs/developer-guide/testing.md` |
| `python test_fixtures/test_api.py` | 隔離テスト環境 (`127.0.0.1:18000`) に対する **HTTP 疎通スクリプト** 8 項目 | `test_fixtures/test_api.py:305-345` |
| `python test_fixtures/test_api.py --quick` | 上記から **チャットテストだけを外す** | `test_fixtures/test_api.py:348-352`、`381`、`391` |
| `python test_fixtures/setup_test_env.py` | `test_data/` に隔離環境を作る (本番 `~/.saiverse` は触らない) | `docs/test_environment.md §クイックスタート` |
| `python scripts/run_conversation.py` | **実 LLM・実コスト**で台本をテスト環境のペルソナへ流す | `docs/test_environment.md §会話テスト` |
| `python scripts/run_day_sim.py` | 一日シム (mock モードと `--real` モードがある) | `scripts/run_day_sim.py:400` `run_mock_scenario` |
| `python scripts/check_in_flight.py` | 台帳の字数・過去形マーカーの機械検査 | `CLAUDE.md §Progress Tracking` / `tests/test_in_flight_check.py` |
| `python scripts/gen_reference_docs.py --check` | 自動生成 doc のドリフト報告 (**CI ゲートは無い**) | `CLAUDE.md §Documentation Maintenance` |

**`--quick` が省くもの (ソースで確認)**: `run_all_tests(skip_chat=True)` は `test_chat()` だけを飛ばす (`test_fixtures/test_api.py:348-352`)。
残る 7 項目は City / Buildings / Personas / Playbooks の存在確認と、Models Config / User Status / User Buildings の GET。
つまり **`--quick` は「チャットが動くか」を一切見ない**。
さらに `--quick` を付けない完全版でも、チャットの検査は `test_fixtures/test_api.py:265-275` で
`response_text` が真値かどうかを見るだけ — 内容も、SAIMemory / building_messages への永続化も見ない。

### 0-2. CI で自動実行されるもの / 手動でしか走らないもの

`.github/workflows/` にはファイルが **2 本しかない**。

| ワークフロー | 発火条件 | 走らせるもの |
|---|---|---|
| `discord_gateway.yml` | `push` / `pull_request` の **paths フィルタ付き** — `discord_gateway/**`, `requirements.txt`, `requirements.lock`, `discord_gateway/requirements-dev.txt`, `discord_gateway/pyproject.toml`, 自分自身 | `ruff check discord_gateway --config discord_gateway/pyproject.toml` と `pytest discord_gateway/tests -q` |
| `release.yml` | `push` の tag `v*` | `git archive` で zip を作り `gh release create`。**テストも lint も走らせない** |

したがって:

- **`tests/` 配下 298 本は CI で一度も実行されない。** `sea/`, `sai_memory/`, `api/`, `saiverse/`, `frontend/`, `database/` を変更した PR では **ワークフローが 1 本も起動しない** (paths フィルタにこれらのパスが無い)。
- CI が走る唯一の製品領域は Discord Gateway (テスト 14 本、38 テスト関数)。
- リリースはタグ push で zip を作るだけで、検査の通過を条件にしていない。

⚠️ **文書との矛盾**: `docs/developer-guide/testing.md:131` は「プルリクエスト時に自動でテストが実行されます」と書いている。
上の YAML の実体と食い違う (自動実行されるのは discord_gateway だけ)。どちらが「あるべき姿」かはこの調査では判定しない。

### 0-3. lint / 型検査の位置づけ

**Python (ruff)**: 設定ファイルが 2 つあり、内容が違う。

| ファイル | line-length | select |
|---|---|---|
| `ruff.toml` (リポジトリルート) | 200 | `F`, `E9` (`F401`/`F403`/`F405` は ignore) |
| `pyproject.toml` `[tool.ruff]` | 100 | `E`, `F`, `I`, `W`, `B`, `UP` |

**実測**: `.venv/Scripts/ruff.exe check --show-settings saiverse/clock.py` の出力は
`linter.line_length = 200`、有効ルール 41 件、isort (`unsorted-imports`) は**含まれない**。
→ ルートで効いているのは `ruff.toml` の方で、`pyproject.toml` の `[tool.ruff]` 節は効いていない。
`pyproject.toml:36` のコメントは「merged from discord_gateway/pyproject.toml」と書いているが、
`discord_gateway/pyproject.toml` は削除されておらず、CI はそちらを `--config` で名指ししている。

ruff は CI で走らない (discord_gateway サブツリー限定)。`CLAUDE.md` は「Python を書いたら `ruff check` を必ず走らせる」と
人間 (と Claude) の手作業として定めている — 機械の歯止めではない。

**フロントエンド (TypeScript)**: `frontend/package.json` の scripts は `dev` / `build` / `start` / `lint` / `predev` / `prebuild` / `sync:addon-panels` のみ。
- `test` スクリプトは**無い**。
- `typecheck` スクリプトも無い。`tsconfig.json` は `"strict": true, "noEmit": true` だが、`tsc` を単体で呼ぶ入口は用意されておらず、型検査は `next build` の中でしか走らない。
- `eslint.config.mjs` は `@typescript-eslint/no-explicit-any` と react-hooks 系 5 件を `warn` に降格している (「エラー 0 のベースラインを維持するための措置」と自コメント)。
- lint も CI で走らない。

---

## 1. テストファイル一覧と、それが触る製品領域

**この表の作り方 (限界を明記する)**: 312 本すべての本文を読み切ってはいない。
AST で各ファイルの `import` 文・`test*` 関数数・assert 数と、
`TestClient` / `fastapi` / `sqlite3` / `tempfile` / `SAIMemory` / `init_db` / `MagicMock` / `patch` / `monkeypatch` / `AsyncMock` の出現を機械抽出し、
そこから「対象」「深さ」「差し替え」を導いた。**docstring とテスト名は判定に使っていない** (共通規約の要求どおり)。
本文を実際に読んで裏を取ったファイルには行頭に ✔ を付けた (**18 本**)。それ以外の 294 行は *import と印からの推定* であり、
個々の assert が何を見ているかまでは確認していない。

「LLM 名の参照あり (差し替えの有無は本文未確認)」は、ファイル中に `llm` / `LLM` の語はあるが
mock/monkeypatch の印が無かったもの。実際に LLM を呼ぶという意味ではない (呼ぶなら課金が発生するので、
そういうテストがあれば別途の問題になる — 本調査では**実 LLM を呼ぶ pytest は見つけていない**が、全件は確認していない)。

領域の記号: A=会話とコンテキスト / B=記憶 / C=世界 / D=ペルソナ設定・生活・導入導線 /
E=画面のない自動処理 / F=導入運用・設定・アドオン/MCP・外部連携。
「判定不能」は、import した製品モジュールが領域割り当ての規則に当たらなかったもの
(例: `api.routes` を丸ごと import している、製品モジュールを一切 import しない設定/契約検査)。
領域が無いという意味ではなく、機械では決められなかったという意味。

**既知の誤分類 1 件**: `tests/test_day_sim_regression.py` は表では L1 だが、
本文を読むと `scripts.run_day_sim.run_mock_scenario` 経由で実 SQLite を作る (実際は L2 相当)。
テストファイル自身が DB を触らず、呼んだ先が触るケースは印では拾えない。
同じ理由で L1 に落ちているファイルが他にもある可能性がある。

| テストファイル | 本数 | 主な対象 (import から) | 実コードをどこまで通すか | mock / 無効化 | 領域 |
|---|---|---|---|---|---|
| `discord_gateway/tests/test_auth_service.py` | 5 | discord_gateway.bot.auth / discord_gateway.bot.database | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_command_processor.py` | 3 | discord_gateway.bot.command_processor | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_config.py` | 2 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | monkeypatch | 判定不能 |
| `discord_gateway/tests/test_connection_manager.py` | 7 | discord_gateway.bot.connection_manager / discord_gateway.bot.database | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_gateway_service.py` | 1 | discord_gateway.auth / discord_gateway.config / discord_gateway.gateway_service ほか | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_mapping.py` | 3 | discord_gateway.mapping | L1: 関数/クラス単体 | monkeypatch | F |
| `discord_gateway/tests/test_memory_transfer_manager.py` | 3 | discord_gateway.orchestrator / discord_gateway.visitors | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_orchestrator.py` | 5 | discord_gateway.mapping / discord_gateway.orchestrator / discord_gateway.translator ほか | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_permissions.py` | 4 | discord_gateway.mapping / discord_gateway.permissions | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_saiverse_adapter.py` | 1 | discord_gateway.mapping / discord_gateway.orchestrator / discord_gateway.saiverse_adapter ほか | L1: 関数/クラス単体 | mock/patch あり / monkeypatch | F |
| `discord_gateway/tests/test_security.py` | 2 | discord_gateway.bot.security | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_translator.py` | 1 | discord_gateway.translator | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_visitors.py` | 2 | discord_gateway.visitors | L1: 関数/クラス単体 | 差し替えなし | F |
| `discord_gateway/tests/test_ws_integration.py` | 2 | discord_gateway.bot.command_processor / discord_gateway.bot.connection_manager / discord_gateway.bot.database ほか | L1: 関数/クラス単体 | monkeypatch | F |
| `tests/llm_clients/test_anthropic_request_builder.py` | 7 | llm_clients.anthropic_request_builder | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F |
| `tests/llm_clients/test_anthropic_response_parser.py` | 3 | llm_clients.anthropic_response_parser | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F |
| `tests/llm_clients/test_anthropic_retry_policy.py` | 3 | llm_clients.anthropic_retry_policy / llm_clients.exceptions | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | F |
| `tests/llm_clients/test_openai_codex_auth.py` | 44 | api.routes / llm_clients / llm_clients.openai_codex | L3: HTTP (TestClient) | monkeypatch / LLM は差し替え | F |
| `tests/sea/test_langgraph_runner_boundary.py` | 3 | sea.langgraph_runner / sea.playbook_models | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A |
| `tests/sea/test_resolve_template_arg.py` | 9 | sea.runtime_utils | L1: 関数/クラス単体 | 差し替えなし | A |
| ✔ `tests/sea/test_runtime_engine.py` | 9 | api.routes / sea.runtime | L1: 関数/クラス単体 | mock/patch あり / AsyncMock / monkeypatch | A |
| `tests/sea/test_runtime_regression.py` | 16 | llm_clients.exceptions / sea.cancellation / sea.runtime ほか | L1: 関数/クラス単体 | mock/patch あり / monkeypatch / LLM は差し替え | F,A |
| `tests/sea/test_runtime_state.py` | 4 | sea.runtime / sea.runtime_state | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/sea/test_tool_node_tuple_result.py` | 3 | sea.runtime / tools / tools.core | L2: 実 SQLite (記憶 DB) | 差し替えなし | A,F |
| `tests/test_addon_config_mcp_reconnect.py` | 8 | api.routes / api.routes.addon / tools ほか | L1: 関数/クラス単体 | mock/patch あり / AsyncMock | F |
| `tests/test_addon_external_loader.py` | 4 | tools | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_addon_hooks.py` | 13 | saiverse | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_addon_installer_constraints.py` | 3 | saiverse / saiverse.addon_manifest / saiverse.data_paths | L1: 関数/クラス単体 | mock/patch あり | F |
| `tests/test_addon_loader_integrations.py` | 9 | database / database.models / saiverse ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | F |
| `tests/test_addon_paths.py` | 5 | saiverse.addon_paths | L1: 関数/クラス単体 | mock/patch あり | F |
| `tests/test_addon_registry_trust.py` | 3 | saiverse | L1: 関数/クラス単体 | mock/patch あり | 判定不能 |
| `tests/test_addon_routes_params_merge.py` | 7 | api.routes.addon | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_addon_secret_param_deletion.py` | 11 | api.routes / api.routes.addon | L1: 関数/クラス単体 | mock/patch あり / AsyncMock | F |
| `tests/test_addon_toggle_propagation.py` | 16 | api.routes / api.routes.addon / tools | L1: 関数/クラス単体 | mock/patch あり | F |
| `tests/test_admin_ai_edit_contract.py` | 10 | database.models / manager.admin / manager.persona ほか | L2: 実 SQLite (中央 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | C,D |
| `tests/test_api_file_boundaries.py` | 5 | api.file_safety / api.routes / saiverse.data_paths ほか | L3: route 関数を直接呼ぶ | mock/patch あり | F |
| `tests/test_arasuji_absorption.py` | 98 | api.routes.people.arasuji / api.routes.people.models / database.models ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch / LLM は差し替え | A,B |
| `tests/test_arasuji_alignment.py` | 25 | sai_memory.arasuji.alignment / sai_memory.memory.storage | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | B |
| ✔ `tests/test_arasuji_bands.py` | 57 | sai_memory.arasuji.absorption / sai_memory.arasuji.bands / sai_memory.arasuji.generator ほか | L2: 実 SQLite (中央 DB) | mock/patch あり / LLM は差し替え | B |
| `tests/test_arasuji_diagnosis_api.py` | 10 | api.routes.people.arasuji / sai_memory.arasuji.storage / saiverse_memory | L2: 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch | B |
| `tests/test_arasuji_executor.py` | 19 | llm_clients.exceptions / sai_memory.arasuji.alignment / sai_memory.arasuji.executor ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | F,B |
| `tests/test_arasuji_generation_status_mapping.py` | 12 | api.routes.people / saiverse.dynamic_state | L1: 関数/クラス単体 | monkeypatch / LLM は差し替え | C |
| `tests/test_arasuji_generator.py` | 10 | llm_clients.exceptions / sai_memory.arasuji.generator | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | F,B |
| `tests/test_arasuji_interleaved_consolidation.py` | 2 | sai_memory.arasuji.alignment / sai_memory.arasuji.bands / sai_memory.arasuji.context ほか | L2: 実 SQLite (中央 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | B |
| `tests/test_arasuji_regenerate.py` | 14 | sai_memory.arasuji / sai_memory.arasuji.generator / sai_memory.arasuji.storage ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B |
| `tests/test_aspect_derivation.py` | 12 | sea.playbook_models / sea.pulse_context | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | A |
| `tests/test_attachment_paths.py` | 10 | api.routes.chat / saiverse.media_utils | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A,F |
| `tests/test_audit_batch_one_safety.py` | 8 | database.backup / database.migrate / saiverse.upgrade ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | E |
| `tests/test_audit_second_batch_world.py` | 10 | database.backup / database.models / manager.initialization ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | E |
| `tests/test_auto_recall.py` | 51 | sai_memory.unified_recall / sea / sea.auto_recall ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,A |
| `tests/test_autonomy_manager.py` | 15 | saiverse.autonomy_manager / saiverse.event_scheduler | L1: 関数/クラス単体 | mock/patch あり | E |
| `tests/test_autonomy_wiring.py` | 88 | database.models / llm_clients.exceptions / saiverse ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch / LLM は差し替え | F,E,D,B |
| `tests/test_available_playbooks_section.py` | 3 | sea.head_pipeline.sections.available_playbooks | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/test_beat_finalize.py` | 36 | sea.runtime_llm | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | A |
| `tests/test_beat_gate.py` | 17 | sea / sea.beat_gate / sea.cancellation ほか | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A |
| `tests/test_body_to_fragment_ops.py` | 42 | sai_memory.memopedia.body_to_fragment / sai_memory.memopedia.storage | L2: 実 SQLite (記憶 DB) | monkeypatch | B |
| `tests/test_body_to_fragment_split.py` | 34 | sai_memory.memopedia.body_to_fragment | L1: 関数/クラス単体 | 差し替えなし | B |
| `tests/test_budget_gate.py` | 12 | database.models / saiverse / saiverse.day_simulator ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | D,E,F |
| `tests/test_build_memopedia_core.py` | 2 | scripts.memopedia.build_memopedia_core | L1: 関数/クラス単体 | 差し替えなし | B |
| `tests/test_building_admin_id.py` | 22 | database.models / manager.admin / manager.ids | L2: 実 SQLite (中央 DB) | 差し替えなし | C |
| `tests/test_building_ids_gate.py` | 12 | tools / tools.core / tools.mcp_client | L1: 関数/クラス単体 | mock/patch あり / monkeypatch | F |
| `tests/test_building_ingest_m8.py` | 14 | database.building_messages / database.models / persona.history_manager ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | A,F |
| `tests/test_building_messages_db.py` | 16 | database.building_messages / database.models / persona.history_manager | L2: 実 SQLite (中央 DB) | mock/patch あり / LLM は差し替え | A |
| `tests/test_buildings.py` | 4 | saiverse.buildings | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | C |
| `tests/test_cache_keepalive.py` | 9 | sea.runtime | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | A |
| `tests/test_cache_lifecycle.py` | 16 | api.routes.people.cache_status / database.models / saiverse.saiverse_manager ほか | L2: 実 SQLite (中央 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | A,B |
| `tests/test_calculator.py` | 4 | tools | L1: 関数/クラス単体 | 差し替えなし | F |
| ✔ `tests/test_chat_boundary_w7.py` | 19 | api.routes.chat / api.routes.user / database.building_messages ほか | L3: route 関数を直接呼ぶ + 実 SQLite (中央 DB) | mock/patch あり | A,C |
| `tests/test_chatgpt_importer.py` | 2 | scripts.import_chatgpt_conversations / tools.utilities.chatgpt_importer | L1: 関数/クラス単体 | 差し替えなし | B |
| `tests/test_chatlog_markdown_import.py` | 13 | tools.utilities.chatlog_exporter_importer | L1: 関数/クラス単体 | 差し替えなし | B |
| ✔ `tests/test_check_lock_platforms.py` | 16 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | monkeypatch | 判定不能 |
| `tests/test_chronicle_char_budget_resolution.py` | 7 | database.models / sai_memory.arasuji.context / tools.context | L2: 実 SQLite (中央 DB) | mock/patch あり | B,F |
| `tests/test_city_identity.py` | 22 | api.deps / api.routes / database.migrate ほか | L3: HTTP (TestClient) + 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | F,E,C |
| `tests/test_clips.py` | 22 | sai_memory / saiverse | L2: 実 SQLite (記憶 DB) | 差し替えなし | 判定不能 |
| `tests/test_clock_and_simulator.py` | 18 | saiverse / saiverse.day_simulator / saiverse.event_scheduler | L1: 関数/クラス単体 | 差し替えなし | D,E |
| `tests/test_clone_persona.py` | 8 | database.models / scripts.clone_persona_to_test_env | L2: 実 SQLite (中央 DB) | 差し替えなし | E |
| `tests/test_clone_world.py` | 10 | database.models / scripts.clone_world_to_test_env | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | E |
| `tests/test_cognitive_model_schema.py` | 6 | database.models | L2: 実 SQLite (中央 DB) | 差し替えなし | 判定不能 |
| `tests/test_config_set_playbook.py` | 3 | api.routes / database.session | L3: route 関数を直接呼ぶ | monkeypatch | 判定不能 |
| `tests/test_content_tags.py` | 9 | saiverse.content_tags | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/test_context_status.py` | 25 | api.routes.people.context_status / sai_memory.arasuji.alignment / sai_memory.arasuji.generator ほか | L3: route 関数を直接呼ぶ | monkeypatch / LLM は差し替え | B |
| `tests/test_copresence_recall.py` | 18 | sai_memory.room_state / sea.head_pipeline / sea.head_pipeline.integration ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | C,A |
| `tests/test_core_memory_scene.py` | 26 | database.models / sai_memory.core_memory / sai_memory.memory.storage ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | B,A,F |
| `tests/test_core_memory_scene_api.py` | 32 | api.routes.people.core_memory / database.models / sai_memory.clips ほか | L3: route 関数を直接呼ぶ + 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | D,B |
| `tests/test_core_memory_section.py` | 6 | sai_memory.core_memory / saiverse_memory / sea.head_pipeline.integration ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり | B,A |
| `tests/test_core_memory_storage.py` | 19 | sai_memory.core_memory | L2: 実 SQLite (記憶 DB) | 差し替えなし | B |
| `tests/test_coverage_repair.py` | 47 | api.routes.people / database.models / sai_memory.arasuji.estimate ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch / LLM は差し替え | B,A |
| `tests/test_curation_p4a.py` | 41 | database.models / sai_memory.curation_ops / saiverse ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | B |
| `tests/test_curation_p4a2.py` | 70 | sai_memory.curation_ops / sai_memory.memopedia / sai_memory.memopedia.core ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,D |
| `tests/test_database_paths.py` | 2 | database | L1: 関数/クラス単体 | monkeypatch | 判定不能 |
| `tests/test_day_plan.py` | 79 | database.models / saiverse / saiverse.day_simulator ほか | L2: 実 SQLite (中央 DB) | mock/patch あり / monkeypatch / LLM は差し替え | D,E |
| ✔ `tests/test_day_scenario.py` | 13 | database.models / saiverse / saiverse.day_report ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch / LLM は差し替え | D,E,A |
| ✔ `tests/test_day_sim_regression.py` | 13 | database.models / saiverse / saiverse.day_report ほか | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | D,E |
| `tests/test_db_locks.py` | 8 | sai_memory.db_locks / sai_memory.memopedia / sai_memory.memory.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | B |
| ✔ `tests/test_db_manager_api.py` | 9 | api.routes / database.models | L3: HTTP (TestClient) + 実 SQLite (中央 DB) | 差し替えなし | 判定不能 |
| `tests/test_desire_stage_migration.py` | 4 | database.migrate / database.models | L2: 実 SQLite (中央 DB) | 差し替えなし | E |
| `tests/test_desk.py` | 24 | sai_memory / saiverse | L2: 実 SQLite (記憶 DB) | 差し替えなし | 判定不能 |
| `tests/test_document_item_ref.py` | 6 | manager.items / tools.context | L1: 関数/クラス単体 | monkeypatch | C,F |
| `tests/test_document_tools_spell.py` | 1 | tools.core | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_entity_extractor.py` | 82 | sai_memory.arasuji / sai_memory.arasuji.storage / sai_memory.memopedia ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B |
| `tests/test_entrance_topology.py` | 21 | saiverse.occupancy_manager | L1: 関数/クラス単体 | 差し替えなし | C |
| `tests/test_episode_context.py` | 25 | sai_memory.arasuji.context / sai_memory.arasuji.storage | L2: 実 SQLite (記憶 DB) | 差し替えなし | B |
| `tests/test_episode_read.py` | 6 | database.models / saiverse / tools.context | L2: 実 SQLite (中央 DB) | 差し替えなし | F |
| `tests/test_episodes_table.py` | 13 | database.models / saiverse | L2: 実 SQLite (中央 DB) | 差し替えなし | 判定不能 |
| `tests/test_episodes_wiring.py` | 10 | database.models / saiverse | L2: 実 SQLite (中央 DB) | 差し替えなし | 判定不能 |
| `tests/test_event_scheduler.py` | 18 | saiverse.event_scheduler | L1: 関数/クラス単体 | 差し替えなし | E |
| `tests/test_eviction_plan.py` | 46 | sai_memory.arasuji.generator / sea.eviction_plan / sea.session_window | L2: 実 SQLite (記憶 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | B |
| `tests/test_execution_context.py` | 24 | llm_clients.exceptions / saiverse.model_defaults / sea.pulse_context ほか | L1: 関数/クラス単体 | mock/patch あり / monkeypatch / LLM は差し替え | F,A |
| `tests/test_execution_ledger.py` | 73 | database.migrate / database.models / saiverse | L2: 実 SQLite (中央 DB) | mock/patch あり / LLM は差し替え | E |
| `tests/test_execution_ledger_wiring.py` | 49 | database.models / sai_memory.perception_buffer / sai_memory.room_state ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch | B,C,E,D,A |
| `tests/test_experience_inheritance.py` | 28 | database.models / saiverse | L2: 実 SQLite (中央 DB) | 差し替えなし | 判定不能 |
| `tests/test_experience_ledger.py` | 17 | api.deps / api.routes.people / database.models ほか | L3: HTTP (TestClient) + 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | F,B,D |
| `tests/test_external_retry_commit_point.py` | 3 | llm_clients.anthropic / llm_clients.ollama / tools.mcp_client | L1: 関数/クラス単体 | mock/patch あり / AsyncMock / LLM は差し替え | F |
| `tests/test_facility_map.py` | 14 | saiverse / saiverse.buildings / saiverse.day_plan ほか | L1: 関数/クラス単体 | 差し替えなし | C,D,A |
| `tests/test_feed_intake.py` | 179 | database.migrate / database.models / sai_memory.perception_buffer ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch | E,B,F |
| `tests/test_feeds_api.py` | 43 | api.deps / api.routes / database.models ほか | L3: HTTP (TestClient) + 実 SQLite (中央 DB) | mock/patch あり | F,E |
| `tests/test_free_slot_handlers.py` | 18 | database.models / saiverse / saiverse.event_scheduler | L2: 実 SQLite (中央 DB) | mock/patch あり / monkeypatch / LLM は差し替え | E |
| `tests/test_fuzzy.py` | 11 | tools.fuzzy | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_game_lifecycle.py` | 40 | database.models / saiverse.data_paths / saiverse.game_lifecycle ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | F,C |
| `tests/test_game_region_gate.py` | 11 | saiverse.occupancy_manager | L1: 関数/クラス単体 | 差し替えなし | C |
| `tests/test_game_session_log.py` | 10 | database.building_messages / database.models | L2: 実 SQLite (中央 DB) | 差し替えなし | A |
| `tests/test_game_world_tools.py` | 12 | tools.context | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_gemini_afc_disabled.py` | 4 | persona.emotion_module / saiverse.llm_router / tools | L1: 関数/クラス単体 | mock/patch あり / monkeypatch / LLM は差し替え | A,F |
| `tests/test_gemini_auto_cache_settings.py` | 14 | api.routes / llm_clients / llm_clients.gemini | L1: 関数/クラス単体 | mock/patch あり / monkeypatch / LLM は差し替え | F |
| `tests/test_gemini_cache.py` | 7 | llm_clients.gemini_cache | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F |
| `tests/test_gemini_latest_contract.py` | 13 | llm_clients.exceptions / llm_clients.gemini / saiverse ほか | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F,A |
| `tests/test_gemini_utils.py` | 3 | llm_clients.gemini_utils | L1: 関数/クラス単体 | monkeypatch / LLM は差し替え | F |
| ✔ `tests/test_generation_stage_signals.py` | 34 | api.routes.chat / database.building_messages / database.models ほか | L3: route 関数を直接呼ぶ + 実 SQLite (中央 DB) | mock/patch あり / LLM は差し替え | A |
| `tests/test_head_fail_closed.py` | 28 | sea.head_pipeline / sea.head_pipeline.integration / sea.playbook_models ほか | L1: 関数/クラス単体 | monkeypatch / LLM は差し替え | A |
| `tests/test_head_mutation_notify.py` | 13 | database.models / saiverse / saiverse.execution_ledger ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | C,A,F |
| `tests/test_head_pipeline.py` | 30 | database.models / sea.head_pipeline | L2: 実 SQLite (中央 DB) | 差し替えなし | A |
| `tests/test_head_pipeline_anchor_ttl.py` | 11 | sea.head_pipeline / sea.head_pipeline.integration | L1: 関数/クラス単体 | mock/patch あり | A |
| `tests/test_head_pipeline_building.py` | 11 | sea.head_pipeline / sea.head_pipeline.sections.building | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/test_head_pipeline_building_occupants.py` | 15 | database.models / saiverse / saiverse.dynamic_state ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | C,A |
| `tests/test_head_pipeline_desk.py` | 18 | sai_memory.clips / sai_memory.desk / sai_memory.memopedia ほか | L2: 実 SQLite (記憶 DB) | monkeypatch | B,A |
| `tests/test_head_pipeline_spell_list.py` | 21 | database.models / sea.head_pipeline / sea.head_pipeline.sections.spell_list ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | A,F |
| `tests/test_head_section_wiring.py` | 4 | sea.head_pipeline / sea.head_pipeline.integration / sea.runtime_context | L1: 関数/クラス単体 | 差し替えなし | A |
| ✔ `tests/test_head_static_sections_follow_code.py` | 10 | sea.head_pipeline.integration / sea.head_pipeline.pipeline / sea.head_pipeline.registry ほか | L1: 関数/クラス単体 | monkeypatch / LLM は差し替え | A |
| `tests/test_history_manager.py` | 13 | database.models / persona.history_manager / sai_memory.memopedia.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | A,B |
| `tests/test_image_generator.py` | 3 | tools / tools.core | L1: 関数/クラス単体 | mock/patch あり | F |
| `tests/test_import_chatlog_titles.py` | 2 | api.routes.people.import_chatlog | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_in_flight_check.py` | 25 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | monkeypatch | 判定不能 |
| `tests/test_info_life_state.py` | 2 | api.routes.info / database.models / saiverse | L2: 実 SQLite (中央 DB) | mock/patch あり | F |
| `tests/test_inspect_world.py` | 18 | database.models / scripts.inspect_world | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | E |
| `tests/test_judgment_layer2_tags.py` | 11 | database.models / sai_memory.purpose_tags / saiverse ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch | B,E,D,F |
| `tests/test_judgment_playbook_prompt_contract.py` | 3 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_judgment_points.py` | 69 | database.models / llm_clients.exceptions / saiverse ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | monkeypatch / LLM は差し替え | F,E,D,A |
| `tests/test_legacy_log_archive_api.py` | 6 | api.routes / saiverse | L3: HTTP (TestClient) | 差し替えなし | 判定不能 |
| `tests/test_legacy_log_find_files_naming.py` | 6 | saiverse.legacy_log_import | L1: 関数/クラス単体 | mock/patch あり | B |
| `tests/test_legacy_log_startup_repair.py` | 7 | database.models / manager.initialization | L2: 実 SQLite (中央 DB) | mock/patch あり | E |
| `tests/test_life_confirmation.py` | 22 | database.models / saiverse / saiverse.event_scheduler | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | E |
| `tests/test_life_phase2.py` | 58 | database.models / saiverse / saiverse.day_simulator ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | D,E,F |
| `tests/test_life_phase3.py` | 18 | database.models / saiverse / saiverse.day_simulator ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | D,E,A |
| `tests/test_life_view_api.py` | 4 | api.deps / api.routes.people / database.models ほか | L3: HTTP (TestClient) + 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | F,B |
| `tests/test_llama_server_manager.py` | 39 | llm_clients.llama_cache / llm_clients.llama_server / llm_clients.openai | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F |
| `tests/test_llm_clients.py` | 82 | llm_clients / llm_clients.exceptions / llm_clients.factory ほか | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F |
| `tests/test_llm_router.py` | 5 | saiverse.llm_router / tools | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A,F |
| `tests/test_location_occupancy_w7.py` | 28 | database.migrate / database.models / database.occupancy_repair ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | E,D,C |
| `tests/test_marker_parser.py` | 20 | saiverse.marker_parser | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/test_marker_store_memory.py` | 6 | sai_memory.clips / saiverse_memory / sea.runtime | L2: 実 SQLite (記憶 DB) | mock/patch あり | B,A |
| `tests/test_mcp_config.py` | 67 | tools / tools.core / tools.mcp_client ほか | L1: 関数/クラス単体 | mock/patch あり / AsyncMock | F |
| `tests/test_mcp_connection_owner_task.py` | 4 | tools.mcp_client | L1: 関数/クラス単体 | mock/patch あり | F |
| `tests/test_mcp_error_classification.py` | 13 | tools.mcp_client | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_mcp_reconnect_outcome.py` | 8 | api.routes / api.routes.mcp / tools ほか | L1: 関数/クラス単体 | mock/patch あり / AsyncMock | F |
| `tests/test_mcp_subprocess_errlog.py` | 3 | tools.mcp_client | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_mcp_tool_refresh.py` | 9 | sea.mcp_tool_refresh | L1: 関数/クラス単体 | mock/patch あり | A |
| `tests/test_memopedia_atomic_writes.py` | 12 | sai_memory.memopedia / sai_memory.memopedia.core / sai_memory.memory.storage | L2: 実 SQLite (中央 DB) | mock/patch あり | B |
| `tests/test_memopedia_category_registry.py` | 21 | sai_memory.memopedia.storage / sai_memory.memory.entity_extractor | L1: 関数/クラス単体 | 差し替えなし | B |
| `tests/test_memopedia_index_toggle.py` | 5 | sai_memory.memopedia / sea.head_pipeline.sections.memopedia_index | L2: 実 SQLite (記憶 DB) | 差し替えなし | B,A |
| `tests/test_memopedia_rebuild_cursor.py` | 3 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L2: 実 SQLite (記憶 DB) | 差し替えなし | 判定不能 |
| `tests/test_memopedia_rollback.py` | 3 | sai_memory.memopedia / sai_memory.memopedia.storage / sai_memory.memory.storage | L2: 実 SQLite (中央 DB) | 差し替えなし | B |
| `tests/test_memopedia_root_trunk.py` | 5 | sai_memory.memopedia.storage / saiverse.curation | L2: 実 SQLite (記憶 DB) | 差し替えなし | B |
| `tests/test_memorize_dict_content.py` | 7 | sai_memory.memory.storage / saiverse_memory / saiverse_memory.adapter | L2: 実 SQLite (記憶 DB) | mock/patch あり | B |
| `tests/test_memory_atlas.py` | 129 | database.models / sai_memory.arasuji / sai_memory.arasuji.storage ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | B,D |
| `tests/test_memory_db_connection_leak.py` | 5 | api.routes.people / sai_memory.memory | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | B |
| `tests/test_memory_notes.py` | 15 | sai_memory.memory.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | B |
| `tests/test_memory_weave_llm.py` | 7 | persona.core / sai_memory.curation_ops / sai_memory.memopedia.storage ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | A,B,F |
| `tests/test_message_stamp.py` | 38 | saiverse_memory / sea / sea.message_stamp ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch / LLM は差し替え | B,A |
| `tests/test_messages_pulse_id_backfill.py` | 6 | sai_memory.memory.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | B |
| `tests/test_meta_layer.py` | 7 | database.models / saiverse.meta_layer | L2: 実 SQLite (中央 DB) | 差し替えなし | E |
| `tests/test_meta_playbooks_day_rhythm.py` | 2 | api.routes.people.summon / database.models | L2: 実 SQLite (中央 DB) | 差し替えなし | 判定不能 |
| `tests/test_metabolism_global_defaults.py` | 41 | api.routes / database.migrate / database.models ほか | L3: route 関数を直接呼ぶ + 実 SQLite (中央 DB) | monkeypatch | E,F,B |
| `tests/test_metabolism_rate_limit_cooldown.py` | 8 | database.models / llm_clients.exceptions / sea.session_lifecycle | L2: 実 SQLite (中央 DB) | mock/patch あり / LLM は差し替え | F,B |
| ✔ `tests/test_metabolism_two_layer.py` | 61 | database.models / llm_clients.exceptions / sai_memory.arasuji ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | F,B,C,A |
| `tests/test_migrate_building_logs_to_db.py` | 41 | database.building_messages / database.models / saiverse ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | A,B |
| `tests/test_mixin_host_contract.py` | 1 | manager.admin | L1: 関数/クラス単体 | 差し替えなし | C |
| `tests/test_mode_spell_permissions.py` | 7 | sea / sea.mode_spell_permissions / sea.pulse_context | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/test_model_change_seq.py` | 7 | api.routes.config | L3: route 関数を直接呼ぶ | monkeypatch | F |
| `tests/test_model_configs.py` | 35 | saiverse / saiverse.data_paths | L1: 関数/クラス単体 | mock/patch あり | F |
| `tests/test_model_watermark_validation.py` | 15 | api.routes / api.routes.config / saiverse | L3: route 関数を直接呼ぶ | monkeypatch | F |
| `tests/test_move_entity_ledger.py` | 11 | database.models / saiverse.execution_ledger / saiverse.execution_ledger_wiring ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | C |
| `tests/test_multi_city_freeze.py` | 6 | database / manager.visitors / saiverse.saiverse_manager | L3: HTTP (TestClient) | 差し替えなし | C |
| `tests/test_native_import_separation.py` | 14 | sai_memory.memory.storage / saiverse_memory.native_export | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | monkeypatch | B |
| `tests/test_native_tool_addon_prefix.py` | 12 | tools / tools.core | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_note_executor.py` | 11 | sai_memory.memory.note_executor / sai_memory.memory.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B |
| `tests/test_note_extractor.py` | 23 | sai_memory.memory.note_extractor / sai_memory.memory.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B |
| `tests/test_note_organizer.py` | 19 | sai_memory.memory.note_organizer / sai_memory.memory.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B |
| `tests/test_note_theme_migration.py` | 14 | database.migrate / sai_memory.desk / sai_memory.memopedia ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり | E,B |
| `tests/test_oauth_handler.py` | 16 | database / database.models / database.session ほか | L2: 実 SQLite (中央 DB) | mock/patch あり | F |
| `tests/test_ollama_endpoint.py` | 14 | llm_clients.ollama / saiverse / saiverse.data_paths ほか | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F |
| `tests/test_open_persona_memory.py` | 9 | saiverse_memory / tools.context | L2: 実 SQLite (記憶 DB) | mock/patch あり | B,F |
| `tests/test_openai_reasoning.py` | 4 | llm_clients.openai / llm_clients.openai_reasoning | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F |
| `tests/test_owner_auth.py` | 4 | api.owner_auth | L3: HTTP (TestClient) | monkeypatch | F |
| `tests/test_p1_migration.py` | 3 | database.migrate / database.models / saiverse.persona_task_manager | L2: 実 SQLite (中央 DB) | 差し替えなし | E,D |
| `tests/test_p4b_naming.py` | 17 | sai_memory.memopedia / sai_memory.theme_pages / saiverse.curation ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,D |
| `tests/test_p4c_vividness_removal.py` | 13 | sai_memory / sai_memory.desk / sai_memory.memopedia ほか | L2: 実 SQLite (記憶 DB) | 差し替えなし | B,A |
| `tests/test_p4d_memopedia_index_section.py` | 15 | sai_memory / sai_memory.memopedia / sea.head_pipeline.sections.memopedia_index ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり | B,A |
| `tests/test_payload_context_filter.py` | 28 | saiverse_memory.adapter | L1: 関数/クラス単体 | 差し替えなし | B |
| `tests/test_people_get_adapter.py` | 9 | api.routes.people.utils / database.models / saiverse_memory | L3: route 関数を直接呼ぶ + 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | B |
| `tests/test_perception_buffer.py` | 29 | sai_memory.perception_buffer | L2: 実 SQLite (記憶 DB) | 差し替えなし | B |
| `tests/test_perception_call_contracts.py` | 4 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | 判定不能 |
| `tests/test_perception_presentation_cap.py` | 64 | sai_memory.arasuji.executor / sai_memory.perception_buffer / sai_memory.room_state ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり | B,C,A |
| `tests/test_perception_rendering.py` | 59 | api.routes.people.memopedia / sai_memory.arasuji / sai_memory.arasuji.alignment ほか | L3: route 関数を直接呼ぶ + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,D,E,A |
| `tests/test_persona_creation_wiring.py` | 16 | database.models / manager.admin / manager.blueprints ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | C,D |
| `tests/test_persona_mixins.py` | 1 | persona.mixins | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/test_persona_voiced_context.py` | 10 | saiverse / sea.playbook_models / sea.runtime_context ほか | L1: 関数/クラス単体 | monkeypatch / LLM は差し替え | A,B |
| `tests/test_playbook_contract_w10.py` | 29 | llm_clients.exceptions / sea.playbook_models / sea.runtime ほか | L1: 関数/クラス単体 | mock/patch あり / monkeypatch / LLM は差し替え | F,A |
| `tests/test_playbook_dry_run.py` | 2 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_playbook_sync.py` | 3 | database.models / saiverse / saiverse.playbook_sync | L2: 実 SQLite (中央 DB) | monkeypatch | E |
| `tests/test_playbook_update_validation.py` | 5 | api.routes.world | L3: route 関数を直接呼ぶ | LLM 名の参照あり (差し替えの有無は本文未確認) | F |
| `tests/test_pocketbook_and_edges.py` | 70 | sai_memory.memory / sai_memory.memory.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | B |
| `tests/test_pocketbook_api.py` | 16 | api.routes.people.pocketbook / database.models / sai_memory.memory.pocketbook ほか | L3: route 関数を直接呼ぶ + 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | D,B |
| `tests/test_pocketbook_spells.py` | 29 | database.models / sai_memory.memory.pocketbook / saiverse ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,D,F |
| `tests/test_pre_spells_dynamic_args.py` | 8 | sea.runtime_llm | L1: 関数/クラス単体 | mock/patch あり / AsyncMock / LLM は差し替え | A |
| `tests/test_preview_perception_badges.py` | 4 | llm_clients.openai_message_preparer / sai_memory.perception_buffer / sai_memory.room_state ほか | L2: 実 SQLite (記憶 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | F,B,C,A |
| `tests/test_provider_configs.py` | 71 | api.routes / llm_clients.factory / saiverse ほか | L3: HTTP (TestClient) | mock/patch あり / LLM は差し替え | F |
| `tests/test_pulse_controller_resumption.py` | 1 | sea.pulse_controller | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/test_pulse_controller_shutdown.py` | 9 | saiverse.saiverse_manager / sea.cancellation / sea.pulse_controller | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | A |
| `tests/test_pulse_dispatcher_phenomenon.py` | 6 | saiverse.pulse_dispatcher | L1: 関数/クラス単体 | 差し替えなし | E |
| `tests/test_purpose_tags.py` | 9 | sai_memory / saiverse | L2: 実 SQLite (記憶 DB) | 差し替えなし | 判定不能 |
| `tests/test_quick_spell.py` | 10 | sea | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | 判定不能 |
| `tests/test_realtime_spells_media.py` | 2 | sea | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | 判定不能 |
| `tests/test_recall_on_enter_gate.py` | 15 | persona.history_manager / sea.head_pipeline.integration | L2: 実 SQLite (記憶 DB) | mock/patch あり | A |
| `tests/test_recall_on_enter_user.py` | 5 | sea.head_pipeline.integration | L1: 関数/クラス単体 | 差し替えなし | A |
| `tests/test_recall_walk.py` | 9 | database.models / sai_memory / sai_memory.memopedia ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,D |
| `tests/test_reference_cutover.py` | 12 | manager.items / saiverse.uri_resolver | L1: 関数/クラス単体 | 差し替えなし | C |
| `tests/test_references.py` | 20 | saiverse | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_region_admin.py` | 45 | database.models / manager.admin | L2: 実 SQLite (中央 DB) | 差し替えなし | C |
| `tests/test_requirements_ascii.py` | 1 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| ✔ `tests/test_requirements_lock_contract.py` | 6 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_response_schema_no_numeric_fields.py` | 5 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_response_schema_source.py` | 6 | sea.runtime_llm / tools.core | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A,F |
| `tests/test_room_state_diff.py` | 140 | database.models / sai_memory.arasuji.executor / sai_memory.perception_buffer ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,C,A |
| `tests/test_ruler_dispatch.py` | 8 | manager.runtime | L1: 関数/クラス単体 | 差し替えなし | A |
| ✔ `tests/test_run_conversation.py` | 5 | database.models / scripts.run_conversation | L2: 実 SQLite (中央 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | 判定不能 |
| `tests/test_run_playbook_spell.py` | 19 | sea.pulse_context / sea.runtime | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A |
| `tests/test_run_spell_tool_async_return_normalization.py` | 7 | sea.runtime_llm / tools.core | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A,F |
| `tests/test_runtime_lan_ip.py` | 7 | saiverse / tools | L1: 関数/クラス単体 | mock/patch あり | F |
| `tests/test_runtime_llm_helpers.py` | 20 | sea.runtime_llm | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A |
| `tests/test_runtime_marker_failclosed.py` | 10 | sai_memory / saiverse | L1: 関数/クラス単体 | monkeypatch | 判定不能 |
| `tests/test_runtime_utils_regression.py` | 3 | sea | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | 判定不能 |
| `tests/test_sai_memory_chunking.py` | 3 | sai_memory.memory.chunking | L1: 関数/クラス単体 | 差し替えなし | B |
| `tests/test_sai_memory_storage.py` | 18 | sai_memory.memory.recall / sai_memory.memory.storage | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | B |
| `tests/test_saiverse_home_paths.py` | 4 | sai_memory.backup / saiverse.data_paths | L2: 実 SQLite (記憶 DB) | mock/patch あり | B,F |
| `tests/test_saiverse_memory_adapter.py` | 11 | sai_memory.memory.storage / saiverse_memory.adapter | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | B |
| `tests/test_schedule_api_sync.py` | 15 | api.deps / api.routes.people / database.migrate ほか | L3: HTTP (TestClient) + 実 SQLite (中央 DB) | 差し替えなし | F,E |
| `tests/test_schedule_default_playbook.py` | 13 | api.deps / api.routes.people / database.models ほか | L3: HTTP (TestClient) + 実 SQLite (中央 DB) | 差し替えなし | F,E |
| `tests/test_schedule_dispatch_outcome.py` | 15 | llm_clients.exceptions / saiverse.pulse_dispatcher / saiverse.schedule_manager ほか | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F,E,A |
| `tests/test_schedule_manager_ledger.py` | 20 | database.models / saiverse / saiverse.event_scheduler ほか | L2: 実 SQLite (中央 DB) | monkeypatch / LLM は差し替え | E,C,D |
| `tests/test_schedule_reconciliation.py` | 30 | database.models / saiverse / saiverse.event_scheduler ほか | L2: 実 SQLite (中央 DB) | mock/patch あり / monkeypatch / LLM は差し替え | E,C |
| `tests/test_searxng_search.py` | 13 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | mock/patch あり | 判定不能 |
| `tests/test_session_anchor_rows.py` | 61 | database.migrate / database.models / sai_memory.arasuji ほか | L2: 実 SQLite (中央 DB) | mock/patch あり / LLM は差し替え | E,B,A |
| `tests/test_session_head_snapshot_rows.py` | 17 | database.migrate / database.models / sea.head_pipeline ほか | L2: 実 SQLite (中央 DB) | mock/patch あり / LLM は差し替え | E,A |
| `tests/test_session_window_folds.py` | 12 | sea.session_window | L1: 関数/クラス単体 | 差し替えなし | B |
| `tests/test_slot_close_note.py` | 28 | database.models / sai_memory.memopedia.storage / sai_memory.purpose_tags ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,D,A |
| `tests/test_slot_kind_catalog.py` | 8 | saiverse | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| ✔ `tests/test_sluice.py` | 125 | database.models / llm_clients.exceptions / sai_memory.core_memory ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | F,B,C,A |
| `tests/test_sluice_capture.py` | 21 | api.routes.people.sluice / database.models / llm_clients.exceptions ほか | L3: route 関数を直接呼ぶ + 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | F,B,A |
| ✔ `tests/test_sluice_cold_isolation.py` | 12 | database.models / llm_clients.exceptions / persona.history_manager ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | F,A,B,C |
| `tests/test_snapshot_exclusions.py` | 14 | scripts | L1: 関数/クラス単体 | mock/patch あり / monkeypatch | 判定不能 |
| `tests/test_snapshot_script_standalone.py` | 1 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_spell_args_parsing.py` | 21 | sea.runtime_llm / tools.core | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | A,F |
| `tests/test_spell_auto_mode_w10.py` | 13 | sea.runtime / sea.runtime_engine / sea.runtime_graph ほか | L1: 関数/クラス単体 | mock/patch あり / monkeypatch / LLM は差し替え | A,F |
| `tests/test_spell_misfire_feedback.py` | 14 | sea.runtime_llm | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A |
| `tests/test_stop_autonomy.py` | 1 | saiverse.saiverse_manager | L1: 関数/クラス単体 | mock/patch あり | 判定不能 |
| `tests/test_streaming_placeholder_salvage.py` | 32 | llm_clients.exceptions / sea / sea.cancellation ほか | L1: 関数/クラス単体 | mock/patch あり / monkeypatch / LLM は差し替え | F,A |
| `tests/test_subplay_line.py` | 11 | sea.playbook_models / sea.pulse_context / sea.runtime_nodes ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | A |
| `tests/test_system_update_api.py` | 2 | api.routes / saiverse | L3: HTTP (TestClient) | mock/patch あり / monkeypatch | 判定不能 |
| `tests/test_task_book.py` | 46 | database.migrate / database.models / saiverse | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | E |
| `tests/test_tell_spell.py` | 13 | persona.history_manager / sea.beat_gate / sea.pulse_context ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | A,F |
| `tests/test_thread_push_pop.py` | 12 | llm_clients.exceptions / saiverse_memory / sea.pulse_context ほか | L2: 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | F,B,A |
| `tests/test_thread_switch_tool.py` | 3 | sai_memory.memory.storage / saiverse_memory / tools.context | L2: 実 SQLite (記憶 DB) | mock/patch あり | B,F |
| `tests/test_time_order_canonical_w8.py` | 18 | sai_memory.memory.storage | L2: 実 SQLite (中央 DB) | 差し替えなし | B |
| `tests/test_timetable_template.py` | 19 | api.routes / database.models / saiverse ほか | L3: HTTP (TestClient) + 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | LLM 名の参照あり (差し替えの有無は本文未確認) | E,D,F |
| `tests/test_tls_trust_fallback.py` | 4 | saiverse | L1: 関数/クラス単体 | mock/patch あり | 判定不能 |
| `tests/test_tool_execute_authorization.py` | 6 | saiverse.composite_actions / sea / sea.pulse_context ほか | L1: 関数/クラス単体 | mock/patch あり | A,F |
| `tests/test_tool_registration_resilience.py` | 3 | tools / tools.adapters / tools.adapters.gemini ほか | L1: 関数/クラス単体 | 差し替えなし | F |
| `tests/test_unified_recall.py` | 31 | sai_memory.arasuji / sai_memory.arasuji.storage / sai_memory.memopedia ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | B |
| `tests/test_update_completion_marker.py` | 56 | scripts | L1: 関数/クラス単体 | mock/patch あり | 判定不能 |
| ✔ `tests/test_update_engine_safety.py` | 17 | scripts | L1: 関数/クラス単体 | mock/patch あり / monkeypatch | 判定不能 |
| `tests/test_upgrade_handlers_dev5_legacy_import.py` | 8 | database.models / saiverse / saiverse.data_paths ほか | L2: 実 SQLite (中央 DB) | monkeypatch | F,B,E |
| `tests/test_upgrade_handlers_legacy_schedule.py` | 7 | database.models / saiverse.upgrade_handlers | L2: 実 SQLite (中央 DB) | 差し替えなし | E |
| `tests/test_upgrade_handlers_legacy_selected_playbook.py` | 9 | database.models / saiverse.upgrade_handlers | L2: 実 SQLite (中央 DB) | 差し替えなし | E |
| `tests/test_upgrade_handlers_memopedia_index_default.py` | 9 | database.models / saiverse.upgrade / saiverse.upgrade_handlers | L2: 実 SQLite (中央 DB) | 差し替えなし | E |
| `tests/test_upgrade_handlers_playbook_replacement.py` | 11 | database.models / saiverse / saiverse.upgrade ほか | L2: 実 SQLite (中央 DB) | monkeypatch | E |
| `tests/test_upgrade_handlers_retired_autonomy.py` | 11 | database.models / saiverse / saiverse.upgrade ほか | L2: 実 SQLite (中央 DB) | 差し替えなし | E |
| `tests/test_upgrade_handlers_spell_enabled_default.py` | 9 | database.models / saiverse / saiverse.upgrade ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | E |
| `tests/test_upgrade_release_chain.py` | 2 | saiverse | L1: 関数/クラス単体 | 差し替えなし | 判定不能 |
| `tests/test_uri_resolver_memopedia.py` | 5 | sai_memory.memopedia.core / sai_memory.memopedia.storage / saiverse.uri_resolver | L2: 実 SQLite (記憶 DB) | 差し替えなし | B,C |
| `tests/test_usage_tracker.py` | 8 | saiverse.usage_tracker | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | F |
| `tests/test_user_conversation.py` | 39 | database.models / saiverse / saiverse.event_scheduler ほか | L2: 実 SQLite (中央 DB) | mock/patch あり / monkeypatch / LLM は差し替え | E |
| ✔ `tests/test_user_utterance_durability.py` | 32 | manager.runtime | L1: 関数/クラス単体 | mock/patch あり / LLM は差し替え | A |
| `tests/test_v030_hidden_spells.py` | 3 | api.routes.people.realtime_spell / api.routes.people.summon | L1: 関数/クラス単体 | mock/patch あり | 判定不能 |
| ✔ `tests/test_v03_autonomy_gate.py` | 12 | saiverse / saiverse.saiverse_manager | L1: 関数/クラス単体 | monkeypatch | 判定不能 |
| `tests/test_v3_shape_migration.py` | 36 | database.migrate / database.models / sai_memory.core_memory ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり | E,B |
| `tests/test_virtual_clock_persona_time.py` | 3 | saiverse / saiverse.schedule_manager / sea.runtime | L1: 関数/クラス単体 | LLM 名の参照あり (差し替えの有無は本文未確認) | E,A |
| `tests/test_visual_context_feed_stand.py` | 3 | (製品モジュールを import しない — ファイル/文字列/スキーマ検査) | L1: 関数/クラス単体 | mock/patch あり | 判定不能 |
| `tests/test_watermark_headroom_validation.py` | 16 | api.routes / api.routes.config / database.models ほか | L3: route 関数を直接呼ぶ + 実 SQLite (中央 DB) | monkeypatch | F |
| `tests/test_window_floor.py` | 47 | database.models / persona.history_manager / sai_memory.arasuji.storage ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / monkeypatch / LLM は差し替え | A,B |
| ✔ `tests/test_window_refill.py` | 57 | database.models / sai_memory.arasuji / sai_memory.arasuji.storage ほか | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | B,A |
| `tests/test_work_session.py` | 17 | database.models / sea.pulse_context / sea.work_session | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | mock/patch あり / LLM は差し替え | A |
| `tests/test_working_memory_recalled.py` | 10 | sai_memory.memory.storage / saiverse_memory.adapter | L2: 実 SQLite (中央 DB) + 実 SQLite (記憶 DB) | 差し替えなし | B |

### 1-1. 手で本文を確認した 18 本の補足 (+ conftest 2 本)

| ファイル | 確認した内容 |
|---|---|
| `tests/conftest.py` | **autouse fixture が全テストで `autonomy_wiring.AUTONOMOUS_DRIVING_SHIPPED` を True へ差し替える** (`tests/conftest.py:33`)。本番の値は `saiverse/autonomy_wiring.py:91` で `False`。→ 自律駆動系のテストは「v0.4 で配線する設計」を固定していて、**v0.3 の出荷挙動 (何も発火しない) を検査していない**。例外は `tests/test_v03_autonomy_gate.py` で、そこだけ明示的に False へ戻す (`test_v03_autonomy_gate.py:34`)。 |
| `conftest.py` (ルート) | `expansion_data/<addon>/` のテストは `--addons` を渡したときだけ収集。`**/external/` は常に除外。 |
| `tests/test_chat_boundary_w7.py` | `RuntimeService.__new__` でインスタンスを作り、`SessionLocal` / `pulse_dispatcher` を MagicMock 化 (`:29-42`)。`database.building_messages.insert_...` を patch。assert は `dispatch_user_utterance` が**呼ばれた/呼ばれない**まで (`:63`, `:77`)。**その先 (Pulse → LLM → 返答 → 永続化) には一切入らない。** |
| `tests/test_user_utterance_durability.py` | 同じ `_runtime()` フェイク。永続化失敗時に dispatch しないこと等を見る。やはりディスパッチャの手前で止まる。 |
| `tests/test_generation_stage_signals.py` | 実 SQLite (`create_engine` + `Base.metadata`) に building_messages を作り、`RuntimeEmitters.emit_speak_finalize` の三値を検査。`history_manager` は MagicMock (`:48-52`)。SEA の実行そのものは通らない。 |
| `tests/test_day_sim_regression.py` | `scripts.run_day_sim.run_mock_scenario` を実走。実 DB・実 event_scheduler・実 day_plan / judgment_points / work_session を通す。**ただし `PulseController` は `MockJudgmentPulseController` に差し替わり** (`scripts/run_day_sim.py:319,380`)、`sea.runtime_llm._run_spell_tool_async` も mock (`run_day_sim.py:418-421`)。LLM は一切呼ばない。 |
| `tests/test_day_scenario.py` | `RealConversationUserEventDriver` (実チャット経路を叩くドライバ) を扱うが、`manager.run_sea_user` を**テスト内で定義した関数に差し替えている** (`:644-657`)。`get_open_conversation` / `start_conversation` も monkeypatch (`:666-672`)。→ 実 Pulse は走らない。 |
| `tests/test_run_conversation.py` | docstring に「実チャット経路そのもの (`RealConversationUserEventDriver`) は**一日シム側で実証済み**のためここでは対象にしない」(`:5-6`)。上の 2 行から、その一日シム側でも実 Pulse は走っていない (mock 経路)。→ **この免除の根拠は成立していない** (2 章・4 章参照)。 |
| `tests/sea/test_runtime_engine.py` | `SEARuntime` の `_lg_*_node` が `_runtime_engine` へ委譲するかだけを見る。`_runtime_engine` のメソッドは `Mock` (`:23`)。ノードの中身は通らない。 |
| `tests/test_metabolism_two_layer.py` | `ChronicleClaimTest._generate_interleaved` (`:505-600`) は **`sai_memory.arasuji.bands.run_band_overflow` を `fake_band` に、`plan_band_overflow` を定数に、`sai_memory.arasuji.executor.execute_plan` をチャンク模擬に差し替える**。→ 実際の束ね (と、その中の安全弁 3 件) は一度も走らない。4 章の主役。 |
| `tests/test_arasuji_bands.py` | 実 SQLite + `_Client` (LLM フェイク、`:46`) で `plan_band_overflow` / `run_band_overflow` の**本物**を通す。`TestApprovedCallCap::test_execution_stops_at_approved_count_even_if_cascade_grows` (`:679`) は「予算で止まる」側を固定。`DEFAULT_MAX_CONSOLIDATIONS_PER_RUN`(=3) との相互作用は 4 章参照。 |
| `tests/test_sluice_cold_isolation.py` | 実 SQLite (temp) + `test_sluice.FakeLLMClient` / `FakeRuntime`。**`CHRONICLE_ENABLED=False`** (`:181`, 意図は docstring `:36-39`)。規模は `TOTAL_MESSAGES=240` × `MESSAGE_CHARS=1_000` = 24 万字 (`:65-67`)。実機事故は 3,744 通 / 212 万字 (docstring `:12-14`) — **約 1/9 の縮尺**。チャット送信〜返答受信は通さない (`session_lifecycle` を直接呼ぶ)。 |
| `tests/test_sluice.py` | `FakeRuntime` (`:125-155`) が `SEARuntime._prepare_context` の役を務める。→ **実際のコンテキスト組成 (窓に何字入るか) は通らない。** |
| `tests/test_window_refill.py` | 実 SQLite。`test_refill_reads_uncompiled_tail_only_up_to_target` (`:418`) が未編纂生ログの読み量に上限を課す唯一のテスト。`git log -S` で **このテストはコミット `305ab748` (=事故2 の修正) で初めて追加された**と確認した。 |
| `tests/test_db_manager_api.py` | `FastAPI()` を組んで `api.routes.db_manager` の router を載せ、`TestClient` で HTTP を叩く。実 SQLite (StaticPool の `:memory:`)。→ 本物の L3。 |
| `tests/test_head_static_sections_follow_code.py` | snapshot の復元がコード側の定数に追従するかを検査。実装への追随ではなく「配布済みの旧文言が残らない」という独立した期待。妥当。 |
| `tests/test_requirements_lock_contract.py` | `requirements.txt` (意図) と `requirements.lock` (固定) の契約 3 件。`docs/intent/dependency_management.md §2-4` を出典に持つ。 |
| `tests/test_check_lock_platforms.py` | `scripts/check_lock_platforms.py` の判定をネットワーク無しで固定。 |
| `tests/test_update_engine_safety.py` | `scripts/update_engine` の portable git 差し込み等。`os.environ` を patch。 |
| `tests/test_v03_autonomy_gate.py` | 該当行のみ確認 (`:34`, `:72`, `:192`)。止め具を False へ戻して「v0.3 では何も発火しない」側を検査する唯一のファイル。 |
| `discord_gateway/tests/conftest.py` | リポジトリルートを `sys.path` に足し、`BotSettings` のフィクスチャを配る。 |

未読だが名前を挙げたもの (推定であることを明示する): `tests/test_inspect_world.py` / `tests/test_clone_persona.py` /
`tests/test_clone_world.py` は import から開発者向けスクリプトの検査と判断した (本文未読)。
`tests/test_arasuji_absorption.py` (3,283 行 / 98 テスト / 379 assert) は最大のテストだが本文未読 —
import から実 SAIMemory + 実 SQLite を通し LLM は mock と読めるが、扱うデータ規模は確認していない。

---

## 2. 「本物の経路をどこまで通すか」の分類

分類は 1 章の機械抽出に基づく (件数は 312 本の内訳)。

| 段階 | 定義 | 件数 | 代表例 |
|---|---|---|---|
| **L1 純粋な単体** | 関数/クラス単体。DB も API も通さない | **141** | `tests/test_session_window_folds.py`、`tests/test_body_to_fragment_split.py`、`tests/test_aspect_derivation.py`、`tests/sea/test_resolve_template_arg.py`、`discord_gateway/tests/test_translator.py` |
| **L2 実 DB あり** | 実 SQLite を作るが LLM と API 層は通さない | **142** | `tests/test_sluice.py`、`tests/test_window_refill.py`、`tests/test_arasuji_bands.py`、`tests/test_memory_atlas.py`、`tests/test_room_state_diff.py`、`tests/test_sluice_cold_isolation.py` |
| **L3 API 層あり** | FastAPI のルート/TestClient を通す。LLM は mock | **29** | (TestClient=14) `tests/test_db_manager_api.py`、`tests/test_feeds_api.py`、`tests/test_city_identity.py`、`tests/test_provider_configs.py`、`tests/test_experience_ledger.py` / (route 関数を直接呼ぶ=15) `tests/test_chat_boundary_w7.py`、`tests/test_context_status.py`、`tests/test_metabolism_global_defaults.py` |
| **L4 端から端** | 利用者の操作に相当する入口から結果の永続化まで | **0** | 該当なし (下記参照) |

**L4 が 0 である根拠**:

- **チャット**: 入口 (`api/routes/chat.py` の `send_message` / `handle_user_input_stream`) を通すテストは
  `tests/test_chat_boundary_w7.py` と `tests/test_user_utterance_durability.py` の 2 本あるが、
  どちらも `pulse_dispatcher` が MagicMock で、**そこから先に進まない**。
- **自律行動**: `tests/test_day_sim_regression.py` が最も遠くまで行く (スケジューラ → コマ → 判断点 → 実 DB 永続化)。
  ただし `PulseController` が `MockJudgmentPulseController` に置き換わっているので、
  「実 Pulse が SEA Playbook を実行して発話が残る」経路は通っていない。
  L3.5 と書きたくなるが、定義上 L4 には届かない。この 1 本は**表の外に置かず、L2 に数えた**
  (TestClient も fastapi も使わないため機械分類が L2 になっている)。
- **フロントエンド**: テスト自体が存在しない (3 章)。

**追跡できていない境界**: L1/L2/L3 の機械分類は「印」に基づくので、
たとえば実 DB を使いながら本質的には純粋関数を試しているファイル (逆も) は誤分類しうる。
件数は目安であって、1 本 1 本の正確な段階付けは本文を読まないと確定しない。

---

## 3. 検査が存在しない領域 (最重要)

1 章の対応表から漏れた「利用者に見える結果」を列挙する。

### TEST-01: チャット送信 → 返答受信を端から端で通すテストが 1 本も無い

- pytest 側: 2 章のとおり、実 Pulse の手前で全部止まる。
- `test_fixtures/test_api.py` の `test_chat()` は唯一の端から端の疎通だが、
  (a) **実 LLM を呼ぶので課金が発生する**、(b) `--quick` で真っ先に外れる、
  (c) assert は `response_text` が空でないことだけ、(d) 手動でサーバーを起動していないと動かない、
  (e) CI では走らない。根拠: `test_fixtures/test_api.py:250-282`, `348-352`。
- 結果として「ユーザーが話しかけて返事が返る」という製品の中核が、
  **機械の回帰検査を一つも持っていない**。

### TEST-02: フロントエンドのテストがゼロ

- `frontend/` 配下に `*.test.*` / `*.spec.*` / `__tests__` / jest / vitest / playwright / cypress の設定は
  **1 つも存在しない** (`node_modules` を除外して検索)。
- `frontend/package.json` に `test` スクリプトが無い。
- したがって、`ChatOptions.tsx` / `MemoryModal.tsx` などの画面の挙動、
  `saiverse://` の sanitize、ダークモード、NDJSON ストリームの解釈は、
  すべて手で見る以外に検査手段が無い。
- `frontend/scripts/sync-addon-panels.mjs` (`predev`/`prebuild` で走る生成器) にもテストが無い。

### TEST-03: 起動 (`main.py`) の通し検査が無い

- `tests/` の中で `main.py` を import しているファイルは **1 本も無い**。
- 起動列の部品は個別に検査されている — `manager.initialization` (`tests/test_legacy_log_startup_repair.py`,
  `tests/test_audit_second_batch_world.py`)、`saiverse.upgrade` / `upgrade_handlers` (7 本)、
  `saiverse.playbook_sync` (`tests/test_playbook_sync.py`)、ランタイムマーカー (`tests/test_runtime_marker_failclosed.py`)。
- しかし「起動列を順番どおりに通す」検査は無い。`argv=["main.py", "city_a"]` を渡している
  `tests/test_audit_second_batch_world.py:44` は slug 修復の関数を直接呼んでいるのであって、`main.py` を走らせてはいない。

### TEST-04: セットアップ / 更新スクリプトの同期 (parity) を機械で見ていない

- `CLAUDE.md §Setup/Update Script Parity` は
  「`update.bat` / `update.sh` / `scripts/self_update.py` は同期必須」「`setup.bat` / `setup.sh` も同様」と定めている。
- `tests/` を検索した限り、`update.sh` / `setup.sh` を読むテストは
  `tests/test_requirements_lock_contract.py` と `tests/test_system_update_api.py` の 2 本のみで、
  **3 本 (2 本) のスクリプトの内容が一致するかを比べる検査は無い**。
- 更新まわりで存在する検査は `tests/test_update_engine_safety.py` (17)、`tests/test_update_completion_marker.py` (56)、
  `tests/test_check_lock_platforms.py` (16)、`tests/test_requirements_ascii.py` (1) — いずれも部品単位。
- `setup.bat` / `setup.sh` そのものを対象にしたテストは無い。

### TEST-05: DB マイグレーションの通し検査が「その回の変更」単位でしか無い

- `database.migrate` を import するテストは 14 本 (`tests/test_p1_migration.py`, `tests/test_desire_stage_migration.py`,
  `tests/test_v3_shape_migration.py`, `tests/test_session_anchor_rows.py`, `tests/test_task_book.py` ほか)。
  いずれも「その変更で入った列・表」を検査する。
- **「v0.2 の実在する形の DB を最新まで一気に上げる」通し検査は見つけていない。**
  `tests/test_sluice_cold_isolation.py` は v0.2 形の DB を**手で組み立てて**いるが、
  マイグレーションを通して作ってはいない (temp DB に直接 INSERT)。
- 追跡できていない境界: `database/migrate.py` の全ステップを順に適用する検査があるかどうかは、
  14 本の本文をすべて読んだわけではないので断定しない。

### TEST-06: 大量データを扱うテストが実質存在しない (規模の実数)

`tests/` 全体で、テストデータの生成規模を機械検索した結果:

| 場所 | 規模 |
|---|---|
| `tests/test_arasuji_bands.py:1064` | 乱数生成 600 件 (`TestPlanProperties` — 計画の性質検査。DB も LLM も通さない) |
| `tests/test_arasuji_bands.py:1085` | 乱数生成 300 件 (同上) |
| `tests/test_arasuji_bands.py:1076` | `arrivals = [(500, 10_000)] * 1_000` (同上) |
| `tests/test_feed_intake.py:250 付近` | 250 件 |
| `tests/test_pocketbook_and_edges.py` | 1,001 件 |
| `tests/test_experience_inheritance.py` | 140 件 |
| `tests/test_sluice_cold_isolation.py:65-67` | **240 通 × 1,000 字 = 24 万字** ← 記憶系で最大の実 DB 規模 |
| `tests/test_feed_intake.py:373,1082` | 100 万字の文字列 1 本 (guid の長さの検査。編纂とは無関係) |

対して**実機の事故の規模は 3,744 通 / 212 万字** (`docs/handoff/2026-09-09_sluice_cold_skip_and_v0311_handoff.md §②`)。
つまり:

- 未編纂ログの**大量**編纂を実 DB で回す検査は無い (最大 24 万字、しかも Chronicle は無効化されている)。
- 大量履歴のコンテキスト読み込みを実 DB で回す検査も無い (同上)。
- 束ねの大量発生を扱う `TestPlanProperties` (600〜1,000 件) は**計画関数の純粋な性質検査**で、
  DB も LLM も安全弁も通らない。

### TEST-07: `expansion_data/` のアドオンテストは既定で走らない

- `conftest.py:38-46` により、アドオンのテストは `--addons` を明示したときだけ収集される。
- 常用の `python -m pytest` にも CI にも含まれない。凍結ではなく「opt-in」なので、
  アドオン側の回帰は事実上手動。

### TEST-08: API モジュールのうち 10 本はどのテストからも名前が出てこない

`api/` 配下の非 `__init__` モジュール 55 本のうち、`tests/` 全体でモジュール名 (basename) が
一度も現れないもの:

`api.routes.addon_actions` / `api.routes.addon_catalog` / `api.routes.addon_events` /
`api.routes.people.native_export_import` / `api.routes.people.pulse_timeline` /
`api.routes.people.reembed` / `api.routes.people.storage_layers` /
`api.routes.phenomena` / `api.routes.tutorial` / `api.utils.enum_resolver`

- 判定方法: basename の完全語一致を `tests/**/*.py` に対して検索。`from api.routes import X` 形も拾う。
- **注意**: 名前が出てくる = テストされている、ではない。逆に、TestClient にアプリ全体を載せている
  テストがあれば名前が出なくても間接的に通る可能性はある — ただし 1 章で確認した TestClient テストは
  いずれも必要な router だけを個別に載せる形だった (`tests/test_db_manager_api.py:20-26` など)。
- `api.routes.tutorial` (導入導線) と `api.routes.phenomena` (世界) は、
  利用者に見える機能でありながら名前すら出てこない。

---

## 4. 事故二件に対する既存テストの位置

### 事故1: 大量の未編纂ログを編纂した際、レベル2あらすじが一つしか生成されない

**実体 (コミット `7d7214be` のメッセージと実機記録から)**:
承認 5 件の補修が「まとめ 3 件」で完了顔になった。
`run_band_overflow` には **1 回の呼び出しあたり 3 件**の安全弁 (`DEFAULT_MAX_CONSOLIDATIONS_PER_RUN = 3`,
`sai_memory/arasuji/bands.py:57`) があり、承認件数はチャンクごとの呼び直しの累計で届く設計だった。
編纂ゼロ・束ねだけの走行は `after_chunk` が一度も走らないので、最後の 1 回きりで 3 件で頭打ちになった。

**いちばん近くにいた既存テスト**: `tests/test_metabolism_two_layer.py::ChronicleClaimTest`

このクラスには、まさにこの経路を対象にしたテストが**修正前から存在していた**。
`test_consolidation_runs_after_each_chunk_and_once_at_the_end` は、修正前こう書かれていた:

```python
# 3 チャンク分 + 最後の 1 回。各呼び出しには残り予算だけを渡す。
self.assertEqual(calls, [5, 4, 3, 2])
```

修正 (`7d7214be`) はこの行を `[5, 4, 3, 2, 1]` に書き換えている。
つまり **既存テストは欠陥のある挙動 (予算 1 件を残して終わる) を「仕様」として固定していた**。

**なぜ捕まえられなかったか — 三つ**:

1. **本物の安全弁が走らない。** `_generate_interleaved` (`tests/test_metabolism_two_layer.py:505-600`) は
   `sai_memory.arasuji.bands.run_band_overflow` を `fake_band` に差し替える (`:577`)。
   `fake_band` は `folded = min(folds_per_call, kwargs["max_folds"])` を返すだけで、
   `folds_per_call` の既定値は **1** (`:506`)。実物の 3 件上限はこのテストの世界に存在しない。
   → 「1 回の上限」と「呼び直しの回数」の掛け算で欠陥が出る形なのに、片方が定数 1 に潰されていた。
2. **束ねだけの走行 (チャンク 0) を試すケースが無かった。** `n_chunks` の既定は 3 (`:505`)。
   修正で追加された `test_final_consolidation_loops_until_the_approved_budget_is_done` は
   `n_chunks=0, folds_per_call=3` を渡す — **この組み合わせは修正前どこにも無かった**。
   実機の事故はまさにその組み合わせ (編纂ゼロ + 実物の valve 3) で起きた。
3. **期待値の根拠が実装だった。** `[5, 4, 3, 2]` という数列は、
   `docs/intent/chronicle_consolidation.md` などの仕様ではなく、当時のコードが出す値をそのまま書いたもの
   (5 章参照)。`plan_band_overflow` が返す承認件数 5 と、実際に束ねられた 4 件が食い違っていることを
   誰も (テストも) 突き合わせていなかった。

**もう一つ近くにいたテスト**: `tests/test_arasuji_bands.py::TestApprovedCallCap::test_execution_stops_at_approved_count_even_if_cascade_grows` (`:679`)。
こちらは**本物の** `run_band_overflow` を実 SQLite で通す。ただし検査しているのは
「承認件数**を超えない**」側だけで、「承認件数**に届く**」側は見ていない。
上限だけを守らせて下限を見ないテストは、頭打ちの欠陥を素通しする。

### 事故2: 大量の未整理履歴をコンテキストに読み込んだ状態で、スルースにも全量を読ませてしまう

**実体**: 起動直後の読み戻しが v0.2 履歴 212 万字を窓に開き、非常畳みのスルースがそれを一発で読もうとして
OpenAI の 429。429 の本文 `"Request too large"` がコンテキスト超過の判定文字列に一致して誤分類され、
「直近 1〜2 通を外して再試行」が夜通し 14,475 回回った
(`docs/handoff/2026-09-09_sluice_cold_skip_and_v0311_handoff.md §②`)。

**修正前に存在した検査**:

- `tests/test_sluice.py` (3,367 行 / 125 テスト) — スルースの本体を実 SAIMemory で通す最大のテスト。
  ただし `FakeRuntime` (`:125-155`) が `SEARuntime._prepare_context` の役を務める。
  → **窓に何字入るか (= 読み戻しが開く量) はテストの入力として与えられる定数であって、検査の対象ではない。**
- `tests/test_window_refill.py` (2,154 行 / 57 テスト) — 読み戻しの本体を実 SQLite で通す。
  `git log -S "test_refill_reads_uncompiled_tail_only_up_to_target"` の結果、
  **未編纂の生ログの読み量に上限を課すテストは、修正コミット `305ab748` で初めて追加された**。
  それ以前は「あらすじの区間を丸ごと開く」側 (`test_refill_opens_the_newest_arasuji_whole_and_stops_at_target` など) は
  厚く検査されていたが、「あらすじが存在しない未編纂の塊をどこまで読むか」は誰も見ていなかった。
- **429 の分類**: `tests/test_sluice_cold_isolation.py::RateLimitMisclassificationTest::test_context_overflow_marker_no_longer_reclassifies_the_429` (`:468`) は
  修正と同時に (`3e29ec8a`) 追加されたもの。修正前に相当する検査は見つけていない。

**なぜ捕まえられなかったか — 三つ**:

1. **境界の両側が別々のテストにいて、間をつなぐテストが無かった。**
   読み戻し (`sea/window_refill.py`) は `tests/test_window_refill.py`、
   スルース (`sea/sluice.py`) は `tests/test_sluice.py`。
   前者が窓に何字入れるかを後者に渡す — その受け渡しを検査するテストが存在しなかった。
   後者は前者を `FakeRuntime` で置き換えることで、**自分の入力が現実にどこから来るかを問わない形**になっていた。
2. **v0.2 形の DB (あらすじが途中で途切れ、パンマーカーが無い) を作るテストが無かった。**
   そういう DB を組み立てるのは `tests/test_sluice_cold_isolation.py` が初めてで、これも修正と同時 (`3e29ec8a`)。
3. **規模を大きくする検査が無かった。** 3 章 TEST-06 のとおり、記憶系の実 DB テストの最大規模は
   修正後でも 24 万字。事故は 212 万字で起きた。
   量の閾値で挙動が変わる機構 (水位・上限・一発に入る量) を持ちながら、
   閾値を跨ぐ規模のデータを作るテストが無かった。

### 依頼元の予備調査の裏取り (自分でも確認した)

「`tests/test_sluice_cold_isolation.py` は実 DB + mock LLM だが Chronicle を無効化し、チャット送信〜返答受信を通していない」
→ **確認した。**

- 実 DB: `tempfile.TemporaryDirectory()` に persona ディレクトリを作り、`saiverse_memory.adapter.Embedder` を
  `DummyEmbedder` に差し替えて実 SAIMemory を作る (`:88-104`)。
- mock LLM: `test_sluice.FakeLLMClient` を再利用 (`:52-57`)。
- Chronicle 無効化: `CHRONICLE_ENABLED=False` (`:181`)。docstring が理由を明記 (`:36-39`)。
- チャット経路: 通していない。`session_lifecycle` を直接呼ぶ形。

**他のテストにも同じ検査をかけた結果**:

| 観点 | 結果 |
|---|---|
| チャット送信〜返答受信を通すテスト | **312 本中 0 本** (2 章) |
| 実 Chronicle 生成 + 実 sluice を同じテストで通すもの | 見つからない。`tests/test_metabolism_two_layer.py` は Chronicle 側で `run_band_overflow` を fake、`tests/test_sluice.py` は sluice 側で `_prepare_context` を fake |
| 実 `PulseController` を通すもの | `tests/test_pulse_controller_shutdown.py` (9)、`tests/test_pulse_controller_resumption.py` (1)、`tests/test_beat_gate.py` (17) が触るが、いずれも SEARuntime 側が差し替わっている。一日シムは `MockJudgmentPulseController` |
| 実 LLM を呼ぶ pytest | **見つけていない** (全 312 本の本文を読んだわけではないので断定はしない)。実 LLM を使う入口は `scripts/run_conversation.py` と `run_day_sim.py --real` と `test_fixtures/test_api.py`(--quick 無し) の 3 つで、いずれも pytest の外 |

---

## 5. テストが「仕様」を固定してしまっている疑いのある箇所

**断定はしない。「期待の根拠が実装のみに見える」という所見**として書く。

### TEST-09: `tests/test_metabolism_two_layer.py` の束ね回数の数列

- `test_consolidation_runs_after_each_chunk_and_once_at_the_end` の `assertEqual(calls, [5, 4, 3, 2])` は、
  修正で `[5, 4, 3, 2, 1]` に書き換わった。
- 数列そのものに対応する仕様記述を `docs/intent/chronicle_consolidation.md` / `chronicle_coverage_gaps.md` に
  探したが、この調査では見つけていない (全文精読はしていない)。
- **根拠が実装のみに見える** — かつ、その実装が欠陥を含んでいた。4 章の主役。
- 同じ疑いが `test_band_progress_labels_stay_monotone_across_a_failed_call` (`(1, 3, 4)` → `(1, 3, 4, 5)`) と
  `test_extraction_failures_are_reported_once_at_the_end` (「4 件」→「5 件」) にもかかる。
  **1 つの欠陥に対して 3 本のテストが同時に書き換わった**のは、3 本とも同じ実装を写していたことの徴候。

### TEST-10: ルート `conftest.py` の autouse fixture が全テストの前提を反転させている

- `tests/conftest.py:33` が `AUTONOMOUS_DRIVING_SHIPPED` を全テストで True にする。本番は False。
- fixture の docstring はこれを「v0.4 で配線する運転の設計を固定する資産だから」と説明していて、
  意図的な判断であることは明記されている (**実装のみの根拠ではない**)。
- ただし帰結として: **自律行動系 53 本 (領域 E) は「v0.3 で利用者に起きること」を検査していない。**
  「止め具が効いている」側を見るのは `tests/test_v03_autonomy_gate.py` の 12 本だけ。
- 疑義として残すのは、この 53 : 12 の比が、出荷物の検査としては逆立ちしていることの方。

### TEST-11: `tests/test_run_conversation.py` の免除の根拠

- docstring (`:5-6`): 「実チャット経路そのもの (`RealConversationUserEventDriver`) は一日シム側で実証済みのため、
  ここでは対象にしない」。
- 一日シム側 = `tests/test_day_scenario.py` は `manager.run_sea_user` をテスト内の関数に差し替えており (`:644`)、
  `tests/test_day_sim_regression.py` は `MockJudgmentPulseController` を使う。
- **「実証済み」の指す先が実証していない。** これは実装を写したというより、
  文書 (docstring) の中で検査の空白が「他所で済んでいる」と記述されて閉じてしまっている形。
  1 章の免除記述が 4 章の空白と正確に重なる。

### TEST-12: `test_fixtures/test_api.py` のチャット検査の合格条件

- 合格条件は `if response_text:` (`:275`) — 空文字でなければ PASS。
- 送っているメッセージは `"Hello, this is a test. Please respond with exactly: TEST_OK"` (`:258`) だが、
  **`TEST_OK` が返ってきたかは検査していない**。
- 期待の根拠は実装以前に「何も定義されていない」に見える。
  「返事が空でない」は利用者の期待の写しではない。

### TEST-13: assert が 0 本のファイルが相当数ある

機械集計で `assert` 文が 0 のファイルが多数ある。`unittest.TestCase` の `self.assertX` を使っていれば
`assert` 文は 0 でも問題ない — 1 章の表で `assert` 0 かつ `unittest_cls` 印ありのファイル (例:
`tests/test_addon_hooks.py` 13 テスト、`tests/test_mcp_config.py` 67 テスト) はその形と考えられる。
`assert` 文 0 のファイルは 312 本中 150 本。うち `unittest_cls` 印も無いものは
`tests/test_sluice_capture.py` の 1 本だけで、これは `test_sluice.py` の基底クラスを継承していて
`self.assertX` が 123 箇所ある (確認済み)。→ **「アサートが一つも無いテストファイル」は存在しない。**

残る疑いは中身の側で、アサートの期待値が「実装が返す値をそのまま書いたもの」かどうかは
本文を読まないと判定できない。ここは追跡できていない境界であり、
5 章で挙げた 4 件以外に同種のものが無いとは言えない。

---

## 6. 調べた入口の一覧 (網羅性の検算用)

| 調べたファイル/ディレクトリ | 何を確認したか | 未対応の場合の理由 |
|---|---|---|
| `tests/` 全 288 ファイル (`test_*.py`) | AST で import・テスト関数数・assert 数・mock 印を全件抽出。1 章の表に全件掲載 | 24 本のみ本文を精読。残りは印からの推定と明記 |
| `tests/sea/` (6 本) / `tests/llm_clients/` (4 本) | 同上。1 章の表に含む | — |
| `tests/conftest.py` | 全文を読んだ。autouse fixture が `AUTONOMOUS_DRIVING_SHIPPED` を反転させることを確認 | — |
| `conftest.py` (ルート) | 全文を読んだ。`--addons` opt-in と `external/` 常時除外を確認 | — |
| `tests/tool_loader.py` / `tests/schema_scan.py` / `tests/audit_20260730_probes.py` / `tests/audit_20260730_model_change.mjs` | ファイル名を確認。`test_*` ではないので pytest には収集されないヘルパー・監査スクリプト | 本文未読。テスト本体ではないため項目にしていない |
| `discord_gateway/tests/` (14 本) + `conftest.py` | AST 抽出 + conftest 全文。CI で走る唯一のスイート | — |
| `test_fixtures/test_api.py` | 全 397 行のうち先頭 60 行と 250-397 行を精読。`--quick` が飛ばす対象をソースで確定 | 中間部 (60-250 行) は個別の GET テスト。未読 |
| `test_fixtures/setup_test_env.py` | 317 行。存在と役割を `docs/test_environment.md` と突き合わせた | 本文未読。隔離環境の構築手順であり、テストの検査対象ではない |
| `test_fixtures/definitions/test_data.json` / `scenarios/*.json` | ファイル一覧を確認 (`day_absent.json` / `day_quon.json` / `day_standard.json`) | 中身未読。`test_day_sim_regression.py` の入力データ |
| `test_fixtures/start_test_server.{sh,bat}` / `start_test_frontend.bat` | 存在を確認 | 中身未読。`docs/test_environment.md` が内容を説明している |
| `test_data/` | ディレクトリ構造のみ確認 (`.saiverse/` と `user_data/`)。**pytest の収集から除外されている** (`--ignore=test_data`) | 生成物であり検査対象ではない。中身は読まない |
| `.github/workflows/discord_gateway.yml` | 全文精読。paths フィルタと実行コマンドを確定 | — |
| `.github/workflows/release.yml` | 全文精読。テスト・lint を一切走らせないことを確定 | — |
| `.github/FUNDING.yml` | 存在のみ確認 | テストと無関係 |
| `pyproject.toml` | 全文精読。`[tool.pytest.ini_options]` と `[tool.ruff]` を確定 | — |
| `ruff.toml` | 全文精読。`--show-settings` で実効値を実測 | — |
| `discord_gateway/pyproject.toml` | 全文精読。CI が `--config` で名指しする設定 | — |
| `discord_gateway/requirements-dev.txt` / `requirements.lock` | pytest-xdist の有無を確認 (lock に `pytest-xdist==3.8.0`) | どちらの pytest 設定が実際に選ばれるかは**実行して確かめていない**。CI のコマンドは repo ルートからの `pytest discord_gateway/tests -q` |
| `setup.cfg` / `pytest.ini` / `tox.ini` | **存在しない**ことを確認 | — |
| `frontend/package.json` | 全文精読。`test` スクリプトが無いことを確定 | — |
| `frontend/` 配下のテストファイル探索 | `*.test.*` / `*.spec.*` / `__tests__` / jest / vitest / playwright / cypress 設定を検索し、**0 件**を確定 | — |
| `frontend/eslint.config.mjs` | 全文精読。warn 降格 6 件を確認 | — |
| `frontend/tsconfig.json` | 全文精読。`strict: true` / `noEmit: true` | `tsc` を呼ぶ入口が無いことは package.json 側で確認 |
| `frontend/next.config.ts` | 精読。`SAIVERSE_BACKEND_ORIGIN` で隔離環境へ向けられること | — |
| `docs/developer-guide/testing.md` | 全 131 行精読。CI に関する記述と YAML の矛盾を発見 | — |
| `docs/test_environment.md` | 221 行のうち 1-120 行精読 | 残りはコマンド詳細。`--quick` の意味はソースで直接確定済み |
| `docs/handoff/2026-09-09_sluice_cold_skip_and_v0311_handoff.md` | §① §② §③ を精読。事故二件の実体・規模の実数を取得 | 以降の節は未読 (レビュー・リリース手順) |
| `docs/intent/sluice_coverage_gaps.md` §検証の旅 / §経緯 | 精読。設計側が想定する検証の三段 (単体 / 隔離 / 実機) を確認 | 全文未読 |
| `git show 7d7214be` (製品コード + テスト) | 全 diff 精読。事故1 の既存テストが欠陥を仕様として固定していたことを確定 | — |
| `git log -S "test_refill_reads_uncompiled_tail_only_up_to_target"` | このテストが修正コミットで初めて現れることを確定 | — |
| `sai_memory/arasuji/bands.py` | `DEFAULT_MAX_CONSOLIDATIONS_PER_RUN` (=3) の定義と、修正で書き換わったコメントを確認 | 全文未読 |
| `sea/session_lifecycle.py` (修正差分) | 呼び直しループの追加を確認 | 全文未読 (6,300 行超) |
| `saiverse/autonomy_wiring.py` | `AUTONOMOUS_DRIVING_SHIPPED = False` (`:91`) と参照箇所を確認 | 全文未読 |
| `saiverse/day_scenario.py` | `ScenarioPlayer` / `RealConversationUserEventDriver` / `MockJudgmentPulseController` の関係を確認 | 全文未読 |
| `scripts/run_day_sim.py` `run_mock_scenario` | mock の差し替え対象 3 件を確認 | 全文未読 |
| `api/` 配下 55 モジュール | テストからの名前参照を機械照合。10 本が一度も出てこないことを確定 | 各モジュールの中身は未読 (領域 G の担当外) |
| `scripts/` 54 本 | 一覧を取得。検査系 (`check_in_flight.py` / `check_lock_platforms.py` / `gen_reference_docs.py --check`) の位置づけを確認 | 個別の中身は未読 |
| `*.bat` / `*.sh` (ルート 12 本) | 一覧を取得。parity 検査の不在を確認 | 中身未読 |
| `.worktrees/` | **調査対象外** (共通規約 6)。検索から除外した | — |
| `temp/` / `.venv/` | pytest の収集対象外・第三者コード。`find` の結果から除外 | — |
| `expansion_data/` | ルート `conftest.py` の扱い (opt-in) だけ確認 | インストール済みアドオンの中身は領域 F の担当と考え、立ち入らない |
