# 引き継ぎ: v0.4 自律運転 — 段 2 閉じ・段 3 の頭 (2026-10-10)

**書いた状況**: Fable の週間クォータが限界に達し、まはーの指示でセッションを閉じる。作業は develop-v0.4 (worktree `C:\Users\shuhe\workspace\SAIVerse-v0.4`) で、**未コミット・未 push の作業は無い** (HEAD = 145ccbc1、origin と一致)。

## 現在地 (正本は台帳と計画書 — ここには書き写さない)

- 状態の正本: [in_flight 台帳](../overview/in_flight.md) の「v0.4 自律運転の再開」の行と、[実装計画](../intent/autonomous_behavior_v04_plan.md) のステータス行・§5 の済注記・経緯 (2026-10-10 続き 2)。
- 一言で: **段 1 (v2 撤去) と段 2 (ティックの器) が、どちらも隔離検証と Codex 敵対レビュー approve まで通って閉じた。** 段 2 のレビューは 5 巡 (指摘 4+4+2+1→0)、フルスイートは収束後 8045 件緑。
- ティックのモード名は**「自律モード」**に決定済み (計画 §4 決定 9。META / AUTONOMOUS の表示名とも)。

## 次の一手: 段 3 の頭の 3 件 — まはーに提示済み・**回答待ち**

セッションの最後の報告で、以下 3 件を具体案つきで出した。まはーの答えが来たら計画 §4 に決定として収録し、段 3 (ティックの運転 — 計画 §5 段 3 の 7 項目) の実装に入る。

1. **「最後の標準呼び出し」の定義 (未決 3)** — 私の案: 本人の標準モデルが実際に呼ばれた出来事ぜんぶ (会話の応答・ティック・アラームへの応対・判断・スルース・keep-alive の保温) を数え、分身・投げっぱなしの下請け (軽量) は数えない。意味 =「T 分、本人の頭が止まっていたら起こす」。
2. **T の既定値とコストの歯止め (未決 4)** — 私の案: 既定 45 分 (ライフ 12 時間で 15〜16 回/日 ≒ v3 のコスト設計)、床 10 分 (未満は設定不可)、設定画面に「一日およそ N 回」の目安表示。均等モードの上限 50 分は物理なので強制のまま。
3. **開発者モードの連動 (未決 20)** — 私の案: 撤去 ([issue](../issues/archive/developer_mode_off_mass_disables_autonomy.md)。OFF が全ペルソナの自律を不可逆に一括 OFF にする古い名残)。

## 段 2 で入った設計 (次セッションが前提にしてよい確定事項)

- **器**: Playbook `tick` (単発 LLM ノード)。Aspect.AUTONOMOUS = 標準 tier・メインライン・committed。出力は本人の記憶のみ。入口 `SAIVerseManager.fire_tick` / `POST /api/people/{id}/tick` (同期・手で打つ用。段 3 のスケジューラはこの同じ経路に乗せる)。
- **単発制御**: LLM ノードの `single_beat` 欄。スペル一巡のみ・続きの生成なし。**帰結は成功も失敗も一通の知覚 (spell_outcomes) で次の標準 Pulse へ全件配送** (配送失敗は Beat を失敗として閉じる・media も引き継ぐ)。
- **tell**: `tell(target, message)` 引数式。軽量文脈 (分身モード) からは拒否。本人の記憶に別の発話行は書かない。
- **並列の安全**: 計画 §8 の表と決定 2 件 — 発火装置のスレッドで LLM に届く仕事をしない (実装は段 3-1) / ユーザー発話の仲裁は退役済み (撤去済み)。
- 段 2 のレビューで起票した issue: `nonstreaming_conversation_cancel_loses_memory` / `tool_logical_failures_lack_error_meta` (どちらも段 3 の前提ではない)。

## 運用メモ (このセッションで効いた手順)

- 隔離検証: 台本の偽 LLM (`test_fixtures/scenarios/scripted_llm_server.py serve`、port 18097) + `test_fixtures/scenarios/start_scripted_backend.bat` の env を bash に写して `main.py test_city --db-file test_data\user_data\database\saiverse.db` (port 18000)。**スペルの正書法は `/spell name='tell' args={...}`** — `/tell(...)` ではない (台本を書くときに一度間違えた)。日本語 body の POST は curl でなく Python urllib で。
- 新しい Playbook は `scripts/import_playbook.py --file ...` で DB に入れるまで見つからない (起動時の playbook_sync も入れてくれる)。
- Codex レビューは起動ラッパー + 15 分心拍見張り (MEMORY.md の索引の一行のまま) で、この夜 6 巡 (段 1 の再投げ 1 + 段 2 の 5) 全部が無人区間なしで回った。
- 段 3 の検証には一日の早回しの道具が要る (計画 §5 段 3-1 — 実時刻予約の issue `autonomy_manager_tick_uses_wall_clock` を先に直す)。
