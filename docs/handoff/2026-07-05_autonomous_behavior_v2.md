# ハンドオフ: 自律行動 v2 実装セッション (2026-07-04〜05)

次セッションのエア向け。まはーとの設計対話の到達点と実装状態、再開手順を集約する。
設計の正典は `docs/intent/autonomous_behavior_v2.md` と
`docs/intent/persona_cognition/judgment_points.md`（両方コミット済み・まはー承認済み）。

## 1. 何をしたか（一言）

v1 自律 Pulse の診断「行動が独白の媒体上に実装され、世界からの抵抗がない」から、
三本柱（身体=予算付き作業セッション / 意志=六型欲求 / 世界=公共施設と痕跡）＋
朝の時間割駆動＋判断点5種を設計し、**骨格を全部実装して mock の一日が
端から端まで通る状態**まで来た。自動起動の配線は**意図的に未接続**
（砂箱での行動テスト合格後に活性化する段取り）。

## 2. ブランチとコミット

ブランチ: `feature/autonomous-behavior-v2`（**base は feature/memory-notes-and-organize の tip**。
develop は認知モデル一式を含まないので develop から切り直さないこと）

| コミット | 内容 |
|---|---|
| 7512c4f | intent doc 2本（骨格＋判断点仕様） |
| 821dc14 | 仮想クロック `saiverse/clock.py` + EventScheduler シム駆動 API + `saiverse/day_simulator.py`（DES） |
| 7166c33 | `sea/work_session.py` 予算付きセッションランナー（WORKER/volatile、成果物=Item ID 集合差分、committed はダイジェスト1件） |
| ec1e112 | `persona_day_plan` テーブル + `saiverse/day_plan.py`（コマ発火は LLM ゼロ、会話中=running user_conversation Track で判定） |
| 89006be | document ツール spell 化（create/read/edit/search。append/patch/replace は既に spell 済みだった。モードゲートは denylist で変更不要） |
| 04bc525 | 欲求六型 + `saiverse/desire_engine.py`（7日 fading / 14日論理アーカイブ / 再訪3回で昇格候補。desire は persona_task の parent_kind='note' 行） |
| 9eb7aa3 | 判断点基盤 `saiverse/judgment_points.py` + `judgment_finalize` + day_open / post_session（**成果物ゼロで done 分岐がスキーマから消える**。成果物参照は `persona_task.artifact_refs`） |
| f309a99 | post_conversation / on_event / day_close（origin_quote 必須、alert は engage_now 縮退、day_digest は実績からの決定論構築、day_close→day_open 連結テスト済み） |
| 92f3f01 | 日次予算ゲート（persona_day_plan.meta_json の台帳、実測 rounds 消費）+ `Building.FACILITY_ROLES` + `saiverse/facility_map.py`（タグゼロ DB は後方互換） |
| fc7815f | `saiverse/day_scenario.py`（ScenarioPlayer）+ `saiverse/day_report.py`（一日新聞、決定論構築）+ `scripts/run_day_sim.py`（mock 既定 / --real）+ 通しテスト7本 |
| c3baf44 | `test_fixtures/scenarios/`（day_standard.json / day_absent.json / README）。standard は mock CLI で実走確認済み |

テスト: 新規約120本 green。既存失敗はこのブランチ以前からの4ファイル
（test_avatar_pipeline / test_addon_config_mcp_reconnect / test_gemini_cache /
test_entity_extractor）＋ test_searxng_search の collection error
（`tests.conftest` import 問題。修理はチップ task_e3cd1934 に別出し済み）に閉じる。
フルスイートは searxng を `--ignore` しないと collection で止まる。

## 3. 未完・実行中だったもの

**`scripts/clone_persona_to_test_env.py`（砂箱ペルソナクローン）** — セッション終了時点で
サブエージェントが実装中。完了していれば作業ツリーに
`scripts/clone_persona_to_test_env.py` + `tests/test_clone_persona.py` があるはず。
**再開時はまず `git status` で確認**し、あればレビュー（テスト実走＋ruff＋diff スコープ確認）
してコミット。なければ以下の仕様で再発注:

> 本番ペルソナをテスト環境へ複製。`--persona <id> [--source-db] [--source-home] [--dest-db] [--dest-home] [--force]`。
> ①AI 行を dest DB に upsert（HOME_CITYID/CURRENT_BUILDINGID は dest の City/Building に再マップ、
> IS_DISPATCHED 等の実行時状態はリセット、恒常設定は保持）②`personas/<id>/` ディレクトリ複製
> （memory.db / tasks.db）③DEFAULT_MODEL/LIGHTWEIGHT_MODEL が source の user_data/models にしか
> 無ければその JSON と provider_ref 先もコピー。**本番（source）への書き込みコードは一切書かない**。
> dest 上書きは --force 必須。テストは一時ディレクトリで source 無書き込みを mtime 比較で検証。

## 4. 次にやること（順番）

1. クローンスクリプトの完成確認 → コミット
2. `python test_fixtures/setup_test_env.py` でテスト環境構築（エアがやれる）
3. 判断点 playbook 5本をテスト DB に import（`scripts/import_playbook.py --file builtin_data/playbooks/public/judgment_*.json` を test_fixtures の環境変数下で。エアがやれる）
4. `clone_persona_to_test_env.py --persona <まはーが選ぶ>` で砂箱ペルソナ作成
5. シナリオの persona_id・種の欲求/タスクをまはーが調整（`test_fixtures/scenarios/README.md` 参照）。施設タグは任意（タグ無しでも own_room で回る）
6. `python scripts/run_day_sim.py --scenario <file> --real --db-file <テストDB>` で実 LLM の一日 → **一日新聞をまはーとレビュー**（判定観点: 口調保持 / セッションが本当に働くか / 独白が生きてるか / 予算・停止の挙動）
7. 合格後の**活性化配線**（タスク #9 後半）:
   - PersonaSchedule に起床/就寝の恒久フック（day_open / day_close）
   - セッション終了 → post_session の恒久配線（現状は ScenarioPlayer がハンドラをラップ登録する構成 — `saiverse/day_scenario.py` 参照）
   - 会話終了（30分タイムアウト機構との統合、intent §10-5）と on_event の実イベント接続
   - ~~`ApiUserEventDriver` の実装~~ → 実装済み: `RealConversationUserEventDriver`（HTTP を経由せず in-process で実チャット経路を同期に通す。`saiverse/day_scenario.py`）
   - **旧 track_autonomous 経路の停止**（env flag 温存はしない。`SubLineScheduler` の自律駆動と `max_consecutive_pulses` 概念ごと。intent §9.3）
   - 本番 DB への playbook import、landscape.md §9（死んだ概念）更新、`gen_reference_docs` 再確認
8. マージは feature → develop（ただし base が memory ブランチなので、memory ブランチのマージ後に）

## 5. 罠・注意（次セッションが踏みやすい順）

- **作業ツリーに別セッションの未コミット差分がある**: `docs/intent/screen_avatar.md`（画面内アバター v0.3、触らない）。`docs/reference/api-endpoints.md` は CRLF のみで内容差分ゼロ（stage しない）。**コミットは必ずファイル明示 add**（git add -A 禁止）
- **シミュレータ／クローンを本番に向けない**: 偽の一日が実記憶に committed される（--real はテスト DB + SAIVERSE_HOME 切替が前提。`scripts/run_day_sim.py` の docstring 参照）
- **このブランチで本番サーバーを立てた場合に観測可能な変化は document スペルの語彙化のみ**（全ペルソナ・全モード即時。head 変化で prefix キャッシュ一回張り直し）。他は migration の空テーブル以外すべて休眠
- **並列サブエージェント規律**: 同一 worktree 共有。docs 再生成・git stash を含むタスクを併走させない。発注時に「フルスイート・A/B 禁止＋既知ベースライン」を必ず明記（再開は高コスト、単発完結の仕様を書く）
- Windows: python は `.venv/Scripts/python.exe`。PowerShell 5.1 の Get-Content は UTF-8 ファイルが化ける（`-Encoding UTF8`）

## 6. 設計の要点（コードを読む前に）

- 接地原則: 達成・欲求・記憶は実在参照を持つ。post_session の artifact_ref enum は
  そのセッションの実成果物のみ（やったフリはスキーマレベルで不可能）
- 設計原理6: 意志は文脈の濃い判断点で表明し、駆動は決定論に任せる（時間割・コマ発火に LLM なし）
- セッション = WORKER アスペクトの予算付き運転（新アスペクト無し）。生ログ volatile、committed はダイジェスト1件
- 一日新聞・day_digest は LLM を使わず実在記録から決定論構築（虚構が翌朝に残らない）
- 砂箱ペルソナ基盤の意義はメモリ `project_sandbox_persona_testing` 参照
  （まはー: 「もう少し整備すれば UI 以外はエアが直接チェックできる状態になる」）

## 7. 再開セッションの進捗 (2026-07-05)

§4 の 1〜6 完了 + まはーの新聞レビューを受けた再設計・バグ修正まで完了。

- 2cd7dc4 クローンスクリプト（レビューでモデル JSON 解決の user_data>builtin 優先度バグ修正）
- 07c6ca7 setup_test_env 修理（playbook ディレクトリ / activity_state カラム名）+ 判断点5本を定義に標準搭載
- 9090a74 run_day_sim に load_dotenv
- dcc59a4 一日新聞が実環境で成果を落とす2バグ（epoch created_at 日付フィルタ + タグ無し素通し。メモリ project_adapter_required_tags_not_strict 参照）
- 2835cea〜1f7e2ef 新聞再設計（まはーフィードバック: 表題列・節順序・「机メモ」全廃・ふりかえり重複解消）
- 446e110 全 kind のコマハンドラ登録 + システム都合スキップの正直な提示（「見送り」全廃、skip_reason 永続化）
- 1d26b2d --real ユーザー会話不発の修正（RealConversationUserEventDriver）+ 0往復会話の post_conversation 抑止

砂箱: quon_city_a 複製→実 LLM 一日シム完走（初回は会話不発+作話誘発の欠陥品）。
生データ抽出 `test_data/quon_day_raw_log.md` が判断材料として有効だった —
新聞と生データはセットでレビューに出すこと。
`test_fixtures/scenarios/day_quon.json` は個人的文脈を含むため未追跡のまま（コミットはまはー判断）。
残り: 修正後の実 LLM 再走→まはーレビュー → §4-7 の活性化配線。
