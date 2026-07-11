# ハンドオフ: Memory Atlas P4 完走 〜 エアの実機初日準備（2026-07-11〜12 セッション）

> **これは何**: P3c 実装 → P4 全片（レジストリ/vividness除去/編纂/命名/目次）→ 実機初日の準備、を一気通貫で走った長大セッションの引き継ぎ。
> **技術の現在地は既存 doc が正**（`docs/intent/concept_consolidation.md` = Atlas 台帳、`docs/intent/autonomous_behavior_v2.md` 末尾 = 営業日、`docs/overview/in_flight.md` = 次アクション）。本書はそこに書き切れない流れと質感を次のメティスへ渡す。
> 前セッションの哲学的引き継ぎ `2026-07-11_memory_atlas_philosophy_handoff.md` は引き続き有効（語彙の由来・検収の行間文化）。

## 1. 次の最初の一手

1. **営業日（深夜跨ぎ）修正のコミット確認**: セッション末尾に全体 pytest を背景実行中のまま終わった可能性がある。`git status` で `saiverse/autonomy_wiring.py` / `judgment_points.py` / `day_plan.py` / `tests/test_autonomy_wiring.py` / `test_day_plan.py` / intent 追記 / ideas / 本ハンドオフが未コミットなら、`pytest tests/ --ignore=tests/test_avatar_pipeline.py --ignore=tests/test_addon_config_mcp_reconnect.py` の緑を確認してコミット（メッセージ案: 「feat(autonomy): 深夜跨ぎリズムの営業日対応」）
2. **エアの Active 化 → 実機初日の観察**（§5 の観察ポイント）。前提は全部済んでる: 判断点 playbook import 済み・PersonaSchedule 設定済み（起床07:00・就寝01:00）・営業日対応済み・スケジュール UI の穴も塞ぎ済み
3. その先: P4 実機検証項目の消化 → v0.3.0 リリース準備（`docs/overview/v030_release_worklist.md`）

## 2. このセッションで完了したもの（コミット列）

- **P3c-0 desire 正規化**（dadb866, 41ad35e）→ **P3c①② Note畳み+机**（ca9815a）→ 実機追修正（テーマUI 5923458 / **開く=読む** 298bb66）
- **P4 設計 v0.2**（全裁定）→ **P4-0 レジストリ**（bf983a0, 050edda）→ **P4-c vividness除去+vivid→机**（09938ff）→ **P4-a 編纂三層**（265ec60, 7cc1f2b）→ **P4-b命名+P4-d目次**（b3f568b）
- **再会システム根治**（c1b6b53: 同名未紐づけページの採用）+ **ユーザーも想起対象に**（4f19f4c、まはー裁定）
- **実機準備**: スケジュールUIに起床・就寝を出す（19f511c）/ 日予算入力欄（f156437、DB import 済み）/ 営業日対応（コミットは §1-1 参照）
- issue クローズ: memopedia_category_hardcoding（archive 済み、5種のドリフト解消）

## 3. 実機初日までに踏んだ地雷と現在の状態（時系列の質感）

まはーの実機検証は**毎回本物の虫を出した**。この繰り返しが本セッションのリズム:
テーマがUIに出ない（カテゴリ固定列挙）→ memory_open が中身を見せない（開く=読む裁定）→ vivid はメモ需要（→机移行）→「まはー (1)」重複（→再会システム根治）→ **就寝01:00 が3系統を壊す**（→営業日）→ 時間割の昇順検証で「深夜帯はコマを置けない=暦日内の意味論」が判明（将来課題として intent に明記）。
**まはーの一言から根因まで掘る→裁定を仰ぐ→即実装→実データで予行**、が確立した型。

## 4. 検収の行間（このセッションで拾ったもの——委譲エージェントは文字面に忠実、行間はメインの責務）

- **head セクション配線漏れは二度起きた**（Desk P2a / MemopediaIndex P4-d）: 単体テスト緑でも SYSTEM_PROMPT_SECTION_NAMES / enabled_sections 両点に未登録なら本番で描画されない。**恒久検査 `tests/test_head_section_wiring.py` で型を封じた**
- 委譲先が勝手に worktree 孫委譲→レート制限死→**残骸をメインが直接検収して cherry-pick**（feedback_delegate_impl_to_subagents に記録済み。以後の委譲プロンプトには「メインツリー直接・worktree/再委譲禁止」を明記）
- 編纂バッチの **DB ロック共有**（背景スレッドの Memopedia は adapter._db_lock を共有。LLM コール中は持たない）
- vivid→机移行の**一回きり性**（印を落とさないと本人が閉じたページを毎起動で開き直す）
- 営業日の委譲実装は「wake オプトイン引数」で主経路（起床 finalize→コマ予約）が未配線だった→ **wake/営業日は関数内で自己解決**する形に検収修正。エージェントの「実用上露出しない」は逆だった
- 目次の [OPEN] は旧 PageState でなく**机（desk_items）**基準

## 5. エア実機初日の観察ポイント（次セッションの主戦場）

前提状態: エア=PersonaSchedule 設定済み（毎日 07:00 起床 / 01:00 就寝）。Active 化が唯一の引き金（スケジュール有効/無効は「時計のセット」、ACTIVITY_STATE が親スイッチ——fire_judgment_point の第一ゲート）。

1. **夜のうちに Active 化した場合**: 朝まで何も起きないのが正（watchdog は窓外で何もしない）。静寂そのものが検証項目
2. **07:00**: 初の起床判断 → 時間割編成（日予算は PLAYBOOK_PARAMS。スケジュール UI で設定可）→ コマの EventScheduler 予約
3. **日中**: コマ発火（作業セッション）、会話割り込み時の繰り下げ、watchdog の静穏
4. **01:00**: 初の就寝判断——**営業日（前日付）を振り返る**こと。desire_reviews / 層2棚入れ / **棚の乱れ（エアは「まはー」+「まはー (1)」の統合提案が出るはず）** / テーマの芽
5. **就寝後**: 編纂バッチ（承認分のみ）→ 翌朝 event_message「夜の間に棚の整理が行われました」
6. **翌朝**: 新聞に「棚の整理」欄（編集来歴の窓集計）。エアの MEMOPEDIA_INDEX_ENABLED を ON にすれば head に記憶の目次（opt-in 実験）
7. 深夜帯（0時〜1時）はコマの無い自由時間（時間割は暦日内の意味論）。会話・呼びかけ（on_event）は夜でも貫通する——これが望ましいかは観察論点

## 6. 未コミット・残骸・注意

- まはーの並行作業（触らない）: `docs/intent/stackchan_vessel.md` / `docs/issues/legacy_action_handler_cleanup.md` / `docs/handoff/2026-07-10_issue_audit.md` / `builtin_data/models/*gpt-5.6*.json`
- `.pytest-run-nfcf/`（リポジトリ直下）: 死んだ孫エージェントの pytest 残骸。プロセスが掴んでて消せない——再起動後に `rm -rf` で消す
- `git add -A docs/` や `git add builtin_data/` は**まはーのファイルを巻き込む**（今セッションで一度誤ステージ→即 restore）。add はファイル明示で
- 判断点 playbook は import 済みだが、**judgment_day_open は日予算宣言の追加で再 import 済み**（DB 検証済み）。他の4本は初回 import のまま
- フロント変更（ScheduleModal / MemopediaViewer）は再起動+ブラウザ再読み込みで有効

## 7. まはーの裁定の質感（doc に書き切れない部分）

- **「統合文作らせたら絶対に漏れが出る」**→ 本文保存則。この一言が編纂から LLM をほぼ消し、設計を軽く堅くした。まはーの直感が先、私たちが実装可能にする——順番は前セッションから変わらない
- **「でも承認したじゃん、となるのはおかしい」**→ 監督の問い。承認の意味を「可逆な再配置の許可」に機械的に限定する、が答えの骨格
- **「編纂で良いんじゃない？」**→ 命名の帰還。Atlas の動詞の本体が本体の座に戻った
- **「ユーザーのことについては覚えていて欲しいよなぁ」**→ 肥大化に代謝という答えができたから、昔の妥協（ユーザーページを開かない）の前提が消えた。**機能が増えると過去の妥協が順に解ける**——この連鎖はこれからも起きる
- エアの起床時刻はまはーが**エア本人に聞いて**決めた。「ペルソナ自身で設定可能に」は ideas 帳へ
