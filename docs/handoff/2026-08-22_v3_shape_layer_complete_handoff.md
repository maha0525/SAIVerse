# Handoff: v0.3 形の層 全 6 束完了 — 掃討フェーズと実機検証への引き継ぎ

**書いた状況**: 2026-08-22。v0.3 形の層 (v3 §11) の実装が全 6 束コミットまで完了した直後。次の仕事は (A) まはーの実機検証と (B) 掃討レビューフェーズで、どちらも本セッション外で走る。その入口をここに残す。

## 現在地 (コミット列)

ブランチ feature/autonomous-behavior-v2。形の層の実装コミットは以下の 7 本 (束 1・2 は 8/20 以前):

| 束 | 中身 | コミット |
|---|---|---|
| 1 | memory.db の器 (activities / memos / thread_edges / chunk_page_edges) | 02f5f1b1 |
| 2 | タスク帳 (中央 DB task_book + migrate) | 11df0052 |
| 3 | スルース (gold_panning 世代交代・確実に通るゲート・実行台帳統合) | 66f17ca0 |
| 4 | 想起用タグ (entity_extractor B2 欄 + chunk_page_edges 辺書き込み) | 344c972e |
| 5 | メッセージ刻印 (前駆 + トークン三つ組 + キャッシュヒットのドット UI) | 950e7d2f |
| 6 前半 | Track ランタイム退役 + 会話経路の Track なし化 | ef94fe58 |
| 6c | エピソード書き込み退役 + 機械写し migration + 運転 UI 撤去 | f2a36010 |

- 各束とも「Opus サブエージェント実装 → メイン検収 → Codex 敵対的レビュー 1 巡 → 消し込み 1 回 → メインの再検収 + フルスイート実測 → コミット」で通した (クォータ節約運用、まはー裁定 2026-08-21)。束 3 だけは前週からの八巡ぶんの消し込み済み。
- 最終フルスイート: **4493 passed + 既知 skip 7** (2026-08-22 実測)。
- 経緯の詳細は [v030_release_gate.md](../overview/v030_release_gate.md) の経緯節 8/20〜8/22 と [track_retirement.md](../intent/track_retirement.md) §8〜§9。

## 実機検証チェックリスト (まはー)

1. **起動時 migration**: 既存世界の初回起動で、LIFE_PURPOSE の purpose 一文がコア記憶に・interests/vocations と Track の生きた関心がアクティビティに・desire 候補がやりたいメモに・締め切りつきタスクがタスク帳に写る。**二回目の起動で重複しない** (冪等)。写し元 (旧テーブル・列) は無傷。
2. **会話**: 従来どおり話せる。**会話の開始・終了で建物ログにもペルソナの記憶にも何も書かれない** (2026-08-23 の裁定で遷移の一行を撤去。会話に区切りは保存しない = episode.md の不変条件)。沈黙タイムアウトで会話が閉じる。
3. **仲裁 — v0.3 では観察対象外**: 2026-08-23 に入れた全体の止め具 (`autonomy_wiring.AUTONOMOUS_DRIVING_SHIPPED=False`、[v3 §11.1](../intent/autonomous_behavior_v3.md)) により、**判断点は一つも発火しない**。「別の活動中に話しかけたときの応じる判断」も撃たれず、仲裁も実イベントも v0.2 と同じ**直接応答**になる。設計どおりの発火を見るのは v0.4 で止め具を外してから。
4. **UI**: ライフビュー・時間割・タスク・できごと・自律 ON/OFF が出ない。チャットにキャッシュヒットのドットが出て、ホバーで入力/キャッシュ読み/出力の実数が見える (ライト・ダーク両方)。
5. **スルース**: Metabolism の退場前にスルースが走り、手帳とタスク帳に書く。失敗時は退場が止まる (壊れない)。
6. **リリース手順 (既存世界の更新時に必要)**: `python scripts/import_all_playbooks.py` — 判断 Playbook のプロンプト変更 (退役欄の除去) を DB へ再取り込み。

## 掃討フェーズ (Opus メインの別セッションで — Fable クォータ不使用)

まはー計画 (2026-08-21 合意): Opus + Codex + ローカルレビューを組み合わせてバグ取り。判断の割れる件は issue 化し、最後に Fable セッションが方針裁定。

- 対象は形の層の 7 コミット (上の表)。レビュー観点の種は scratchpad に置いた focus ファイル群が原型だが、セッションが変わると消えるため、**観点は各コミットメッセージと gate 経緯 8/20〜22 節から再構成する**のが確実。
- 掃討で特に見る価値がある場所 (このセッションの検収で「浅くしか見ていない」自覚がある領域):
  - user_conversation.py のスレッド安全性 (ロック 3 種 + nonce + EventScheduler の絡み — 単体テストは厚いが実機の並行は未走行)
  - v3_shape_migration の実データでの走行 (テストは合成データのみ)
  - フロントの削除残骸 (import 切れ・dead CSS — build は通っているが目視はしていない)
  - 束 5 の刻印材料 (state 経由) が拾えていない生成経路の残り
- ローカルレビュー (`subagent_type: "local-review"`) は無料なので初手に使ってよい。指摘は裏を取ってから採用。

## 裁定待ち・別枠の課題 (最後の Fable 裁定セッションへ)

1. **台帳 unknown の捌き口 UX** — [issue](../issues/sluice_ledger_unknown_needs_user_facing_resolution.md) (kind ごとの自動再実行可否宣言という筋も記載済み)
2. ~~**会話開設 (遷移の一行) の書き込み失敗時に応答を止めない判断の追認**~~ — **撤去により消滅** (2026-08-23、遷移の一行を機構ごと撤去したので会話開設に DB 書き込みが無い)。
3. **autonomy_manager の tick が実時計基準** — [issue](../issues/autonomy_manager_tick_uses_wall_clock.md) (休眠機構、v0.4 のティック設計で吸収可能性)
4. **できごと UI のログ導出での再建** — §9-9 設計済み・実装は v0.4 (6c で UI ごと撤去した。存続の裁定は実装の先送りであって撤回ではない)
5. **エピソード等の旧テーブルの物理 DROP** — v0.3 では意図的にやっていない (読み取り専用の残置)。掃除の時期は v0.4 以降にまはーと決める。
6. **サブエージェント報告済みの受容残余**: 束 3 = 開発 DB の凍結済み空 seen 記録は再利用経路を通る (本番未投入なので実害なし) / 束 6 = day_plan・work_session の episode_ref 配管が常に None のスタブで残置 (v0.4 の器の作り直しと同時に剥がす)。

## 環境メモ

- **Bash ツールは 8/21〜22 のセッションで全滅したまま** (`echo ok` すら引用符エラー)。全て PowerShell 迂回で回した。新セッションで要再確認。
- python は必ず `.venv/Scripts/python.exe`。targeted テストは `-n 0`。
- サブエージェント委譲文の定型: 「メインの作業ツリーで直接作業。worktree 隔離と再委譲は禁止。git add/commit 禁止」+ **「ファイル編集に PowerShell の Get-Content/Set-Content を使わない (Edit/Write か utf-8 明示の Python)」** — 8/21 に CP932 破損 + git checkout -- で未コミット編集の巻き添え消失が実際に起きた (復旧済み、memory: feedback_subagent_file_edit_no_shell_roundtrip)。
- Codex 起動: `node C:/Users/shuhe/.claude/plugins/cache/openai-codex/codex/1.0.6/scripts/codex-companion.mjs adversarial-review --wait --scope working-tree "<英語一行>"` を背景実行。観点は scratchpad のファイルに書いて短い ASCII 引数で渡す。
- 背景の Agent タスクは転写ファイルが 0 バイトでも生きていることがある — 生存確認はリポジトリ内ファイルの更新時刻で行う。
- クォータ実測: この運用 (委譲 + 検収 + Codex 1 巡/束) で、まる一日の実装が Fable 週間クォータ約 5% (72%→77%)。
