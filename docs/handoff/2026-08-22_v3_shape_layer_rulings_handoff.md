# Handoff: 掃討フェーズの起票 5 件を裁定 — 修正 4 本の作業指示と、その後の順序

**書いた状況**: 2026-08-22 夜。[掃討ハンドオフ](2026-08-22_v3_shape_layer_sweep_handoff.md) が残した起票 5 件を、まはーと Fable で裁定した (5 件とも まはー承認済み)。この文書は**次の Opus セッションがそのまま実装に入れる**ようにする作業指示。判断はもう済んでいるので、ここでは「何を・どう直すか」だけを書く。

裁定の理由は各 issue の「裁定」節に書いてある。実装前にそこを読むこと (Opus の見立てと違う結論になった件が 3 つある — (2)(4)(5))。

---

## 1. 裁定の一覧

| 件 | 裁定 | 扱い |
|---|---|---|
| (1) [手帳の `commit` 引数が所有を検査しない](../issues/archive/pocketbook_commit_flag_is_not_ownership_check.md) | 所有判定をヘルパに切り出し、モジュール全体へ同じ形で当てる (B+D)。例外化・既定反転はしない | **修正** |
| (2) [scene を先頭だけ見せて書き換えさせる](../issues/archive/sluice_truncated_scene_update.md) | scene は長さ不問で update 対象外 (不変条件「scene は参照コピーのみ」から導く)。提示は 80 字のまま | **修正** |
| (3) [手帳メモだけ重複防止が無い](../issues/archive/sluice_memo_duplicate_across_spans.md) | 同じ日・同じアクティビティ・同じ本文をスキップ (B) + プロンプトに今回の対象範囲を明示 (供給源) | **修正** |
| (4) [504 空継続で部分文が二重保存](../issues/archive/stamp_empty_continuation_double_save.md) | 二重保存そのものを直す (C)。数字だけ落とす B は採らない | **修正** |
| (5) [会話ロックを持ったまま DB 書き込み](../issues/conversation_lock_held_across_db_write.md) | v0.3 では触らない。v0.4 のスケジューラ再設計へ (実時計 tick と同じ机) | **凍結** |

---

## 2. 作業指示 (Opus セッション向け)

共通の規律:

- メインの作業ツリーで直接作業。worktree 隔離と再委譲は禁止。
- python は `.venv/Scripts/python.exe`。targeted テストは `-n 0`。
- ファイル編集に PowerShell の Get-Content/Set-Content を使わない (Edit/Write か utf-8 明示の Python)。
- 本番 `~/.saiverse` には触らない。テストは一時 DB とフェイク LLM のみ。
- 修正ごとに回帰テストを付け、**修正を外すと落ちる**ことを確認する (掃討一巡と同じ流儀)。
- 4 本は独立なので、1 本ずつコミットしてよい (コミットは検収後、まはーの確認を経て)。

### 2-1. 手帳の所有判定 (issue 1)

対象: `sai_memory/memory/pocketbook.py` (必要なら `recall_edges.py` の同型も)。

- `get_or_create_activity` (pocketbook.py:305 付近) の `manage_txn = commit and not conn.in_transaction` を、モジュール内の小さなヘルパ (例: `_owns_txn(conn, commit)`) に切り出す。
- `if commit:` の形を持つ関数すべて (issue によれば 10 箇所以上) で、**最初の execute より前**にヘルパの結果を一度だけ取り、成功時の commit と失敗時の rollback の**両方**をその結果で判定する。commit 側だけ直すのは禁止 (失敗時に呼び出し元のトランザクションを巻き戻す経路が残る — issue の ⚠ 節)。
- 同じ形を `recall_edges.py` の `add_chunk_page_edge` にも当てる (issue が名指ししている関数)。`continuity.py` の `add_thread_edge` は既に所有判定を持つので触らない。
- 回帰テスト: 呼び出し元が `BEGIN` 済みの接続で既定 (`commit=True`) のまま呼んだとき、①成功しても呼び出し元の未確定分が確定されない (呼び出し元が rollback すれば全部消える)、②`IntegrityError` でも呼び出し元のトランザクションが巻き戻されない、の二つ。

### 2-2. scene の書き換え拒否 (issue 2)

対象: `sea/sluice.py` の `_apply_ops` (560 行付近の `preview_only` 判定) と `_is_presented_truncated`。

- `update` の拒否条件を「`_is_presented_truncated(current)`」から「`current.kind == "scene"`」へ変える (長さ不問)。`remove` は変えない。
- 結果行の文言を「update 失敗: core:{id} は場面の記憶 (実会話の写し) なので書き換えの対象外です」にする。
- `_is_presented_truncated` は提示側の切り詰め規則だけの役目に戻す (docstring の「適用側の歯止めに使う」を直す)。提示は先頭 80 字のまま。
- 既存テスト (`tests/test_sluice_core_ops.py` の「提示と判定を貫くテスト」) は、歯止めが種類に変わったことに合わせて直す。
- 回帰テスト: **80 字以下の scene** への update が拒否されること (いまの歯止めではここが穴)。

### 2-3. 手帳メモの重複防止と対象範囲の明示 (issue 3)

対象: `sea/sluice.py` の `_apply_memos` と `_build_sluice_prompt`、およびその呼び出し元 (`_compute_span` の結果が届く場所)。

- `_apply_memos`: 同じ `date`・同じ `activity_id`・同じ本文の生存メモがあればスキップ (成功扱い、結果行に「既に手帳にあるため採りませんでした」)。判定は書き込みと同じロック・同じトランザクションの中で行う (check-then-act の隙間を作らない — コア記憶の内容一致ガードと同じ規律)。
- `_build_sluice_prompt` に対象範囲の情報を渡し、手帳の節 (「この範囲で、やりたいと思ったこと…」) の直前か直後に一行足す: 「今回の対象は直近 N 通の会話です。それより前は前回の整理で採取済みです」。span が窓全体 (初回 / マーカー消失) のときは「今回は手元の会話全体が対象です」。N は `span_ids` の長さ = 機械が知る値だけで組む。
- 回帰テスト: issue の「具体的な並び」(Metabolism #1 で `unseen_tail` → #2 で同じメモを返す) を再現し、二行目が書かれないこと。別の日に同じ本文を書くのは通ること (正しい記録を落とさない)。

### 2-4. 504 空継続の二重保存 (issue 4)

対象: `sea/runtime_llm.py` の `_respeak_after_stream_timeout` (335〜475 行) と、その呼び出し元 (4019〜4036 行) から下流 (`_finalize_beat` / memorize) まで。

- **先に読む**: 戻り値が空文字のとき、呼び出し元と `_finalize_beat` が何をするか (空応答の救済・再試行・エラー表示が動かないか)。部分文は `_respeak_after_stream_timeout` に入る前に UI・建物履歴へ emit 済み (4019 行より上) なので、ユーザーから見える発言は失われない。
- 継続が空のとき、部分文を戻り値として下流へ渡さない。下流が空文字を安全に扱うなら空文字を返す。別の救済が動くなら「新しい発言なし (部分文は保存済み)」を表す印を返し、呼び出し元で memorize を飛ばす。
- 新しい行を書かないと決めた経路では `clear_call_tokens(state)` を呼ぶ (継続コールの三つ組が次の行に乗らないように)。`_record_llm_usage` の呼び出し自体は残す (費用の記帳)。
- 回帰テスト: 「504 → 継続が空」で memory.db に同じ本文の行が一行しか無いこと。「504 → 継続あり」の既存挙動 (部分文 + 継続文の二行、継続文に継続コールの三つ組) が変わらないこと。

---

## 3. その後の順序

1. 上の 4 本を実装・コミット。
2. **Codex の横断走査を 1 巡** — 観点は掃討ハンドオフ §4-1 の「隣を忘れた型」(危険を認識して片方に手当てし、同じ理由が当てはまる隣に同じ手当てをしていない) に絞る。対象は形の層の 7 コミット + 掃討の 2 コミット + 上の 4 本。束ごとの縦の掘り下げは投げない (ローカルで消化済みで重複が多い)。
3. まはーの実機検証 — [全束完了ハンドオフ](2026-08-22_v3_shape_layer_complete_handoff.md) のチェックリスト + 掃討ハンドオフ §3-2 の 2 点。**起動前に `python scripts/import_all_playbooks.py` が必須**。issue 2 の「実数を数える」宿題は裁定で消えた。
4. 判断の割れる件が新たに出たら issue 化し、Fable セッションで裁定 (今回と同じ型)。

---

## 4. 環境メモ

- **Bash ツールはセッションによって死んでいる** (Fable セッションでは `echo ok` すら引用符エラー、Opus セッションでは動いた)。死んでいたら PowerShell か Read/Edit で迂回する。
- Codex 起動: `node C:/Users/shuhe/.claude/plugins/cache/openai-codex/codex/1.0.6/scripts/codex-companion.mjs adversarial-review --wait --scope working-tree "<英語一行>"` を背景実行。観点は scratchpad のファイルに書いて短い ASCII 引数で渡す。
- ローカルレビューの起動と上限は掃討ハンドオフ §4-2〜§5 にある。
