# Handoff: v3 形の層・束 3 (スルース) — Codex クォータ復帰待ちの再開手順

**書いた状況**: 2026-08-20 未明。束 3 の Codex 消し込み七巡まで完了した直後、第八巡を投げる前に Codex のクォータが尽きた (復帰 = **2026-08-21 13:43**)。まはーの指示でセッションを compact するため、再開に要る全てをここへ残す。

## 現在地

- **設計フェーズは完結** (2026-08-19 机上完動判定済み)。v3 intent のステータス行参照。用語裁定 (タスク帳 / ルーチン / clear_due / counterpart='user' / スルース同梱物) は全て intent §4.1・§13.6 へ収録済み
- **束 1 (memory.db の器)** = コミット 02f5f1b1、**束 2 (タスク帳)** = 11df0052 — どちらも Codex 8 巡収束済みで本流入り
- **束 3 (スルース) = 実装完了・七巡消し込み済み・未コミット** (作業ツリーに保持中)。`git status` の変更全部が束 3 (新設 `sea/sluice.py` / `tests/test_sluice.py`、削除 `sea/gold_panning.py` / `tests/test_gold_panning.py`、ほか呼び出し元・コメント追従・docs)
- テスト: `tests/test_sluice.py` 96 件緑 / フルスイート **4727 passed + 7 known skips** (この未コミット状態で実測 2026-08-20)

## 束 3 が実装しているもの (検収の要点)

1. **gold_panning → sluice 世代交代**: 互換シムなしの全改名。旧環境変数 `SAIVERSE_GOLD_PANNING_*` は非推奨フォールバック読み (WARNING 一度)。旧パンマーカー KV キーは一回きり移行
2. **確実に通るゲート** (v3 §13.3): スルース失敗 = 退場停止 (あらすじ失敗と同形、次回 Metabolism が自然再試行)。編纂 failed の回はスルース不実行。no_memory・スナップショット読み失敗・壊れた構造化出力・実入力 ID 取得不能は全部 fail-closed。成功スキップは明示の disabled だけ
3. **実行台帳統合**: kind `sluice.pan`、identity = `{persona_id}:{span_start_id}` (終端はキーに含めない — 再試行で動くため)。構造化結果 + seen_ids + タスク/コア記憶スナップショットを applied で凍結し、再試行は LLM 再コールなしで記録を再適用。unknown は人裁定までブロック (issue: `sluice_ledger_unknown_needs_user_facing_resolution.md`)
4. **確定の二段階化**: 適用と確定 (ナレーション永続 → マーカー保存 → mark_completed → 通知、の順) を分離。Metabolism もセッションクローズも finalize=False + マーカー安全ゲート (「新マーカー位置以前の提示対象 ⊆ seen」) を通ってから確定。退場は別ゲート「計画対象全件 ⊆ seen_ids」。マーカーが未提示メッセージを跨ぐ経路は構造上ない
5. **seen_ids は実入力由来**: `prepare_context_impl` が `context_meta["presented_message_ids"]` を書き戻す契約追加 (既存呼び手に影響なし)。近似・再構成のフォールバックは全廃
6. **冪等と CAS**: memos/tasks の idem_key = span 由来安定キー。タスク update はスナップショット revision CAS、コア記憶 update/remove は本文 SHA-256 スナップショット CAS (照合と書き込みは同一 db_lock 下)。競合・スナップショット無しは要素棄却 + 判断ターン記録
7. **応答スキーマ**: 5 欄 (reflection/ops/want_memos/did_memos/promises) 全て required。promises は due 省略可 (発明しない)・clear_due・task_ref は同梱一覧の閉語彙。**due 解釈不能は「期限なしで保存 + 記録」(約束は失わない — まはー好評の差し戻し裁定)**。プロンプト同梱 = アクティビティ一覧 + open タスク一覧 (ID+中身+期限) + コア記憶現況

**受容済みの残余** (直さなくてよいと判断済み): セッションクローズで窓末尾が提示対象外であり続ける間の「採取するが確定しない」LLM コスト反復 (記憶は壊れない) / mark_completed 直前プロセス死でのナレーション重複 (可視・稀) / 台帳 unknown の裁定 UI は issue 起票済み (別途)。

## 再開手順 (クォータ復帰後)

1. **第八巡を投げる** — 前回コマンド (このセッションでは Bash ツールが壊れていたため PowerShell 経由。新セッションでは Bash が直っているかもしれないので、まず `echo ok` で確かめてから選ぶ):

   ```
   node C:/Users/shuhe/.claude/plugins/cache/openai-codex/codex/1.0.6/scripts/codex-companion.mjs adversarial-review --wait --scope working-tree "<英語一行の依頼文>" を run_in_background で
   ```

   依頼文の中身: 「Bundle 3 (sluice) round 8。第七巡の修正 2 件 = 全 5 欄の required 化 + 実行時必須検証、コア記憶 update/remove の本文ハッシュ スナップショット CAS (台帳凍結・同一ロック下照合・スナップショット無し記録は棄却) の解消を再確認し、新種だけを探せ。観点ファイルを先に読め」。観点ファイルは下の「レビュー観点」をスクラッチパッドに書き直して渡す (旧セッションのスクラッチパッド路は失効している可能性がある)
2. 指摘が出たら: 妥当性を裏取り → 修正はサブエージェント委譲 (委譲文に「メインの作業ツリーで直接作業。worktree 隔離と再委譲は禁止。git add/commit 禁止」を明記) → 私が検収・テスト再実行 → 次巡。**収束の判断はまはーに上げる** (予測での打ち切り禁止)
3. 指摘ゼロ = 収束したら: フルスイート → 束 3 を**ファイル明示 add で 1 コミット** (上の git status の全ファイル + 新設 2 + 削除 2)。コミットメッセージは「feat(v3): 形の層・束3 — スルース (gold_panning の世代交代・確実に通るゲート・実行台帳統合)」系 + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
4. 台帳 (in_flight の門の行) を「次 = 束 4」へ更新して同時コミット
5. **束 4 (想起用タグ)** の委譲へ: entity_extractor の同一コールに `involved_entities` 欄追加 (B2 方式) + タイトル→page_id 解決層 + chunk_page_edges への辺書き込み。正典 = v3 §13.6「B2 欄と辺の格納」+ recall_tags intent §9.3。以降 束 5 (メッセージ刻印: 前駆 + トークン三つ組 + ドット UI)、束 6 (Track 撤廃②〜⑦完遂 + 機械写し移行 + エピソード退役 + 自律 UI 隠し) — 実施順序の正典は gate の「v0.3 実装の実施順序」節

## レビュー観点 (第八巡用 — スクラッチパッドに書き直して渡す)

- 正典: v3 intent §13.3/§13.6/§13.5-1/§7.1、束 1・2 の器の契約 (task_book の idem/revision、pocketbook の get_or_create/commit=False)
- ①ゲートの両方向検算 (効かない並び / 効いてはいけないのに効く並び) ②再試行の整合 (span 安定キー・記録再利用・マーカー) ③要素棄却とストレージ例外送出の粒度分け ④Gemini フラットスキーマ維持 ⑤gold_panning 残存参照 (歴史記述以外) ゼロ

## 環境メモ

- このセッションでは **Bash ツールが全滅** (`echo ok` すら harness 内部スクリプトの引用符エラー)。Codex 起動・ローカルレビューとも PowerShell 迂回で回した。新セッションで直っているか要確認
- python は必ず `.venv/Scripts/python.exe`。targeted テストは `-n 0`
- 並行セッションが作業ツリーを共有しうるので、コミットは常にファイル明示 add
