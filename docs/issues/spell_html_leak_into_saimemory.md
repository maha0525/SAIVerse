# Issue: spell の HTML ブロックが SAIMemory に混入する

**ステータス**: 🟡 修正済み・実機検証待ち
**優先度**: high
**作成日**: 2026-05-25
**関連**: `sea/runtime_llm.py` (`_run_spell_loop` 呼び出し 3 経路), `saiverse/content_tags.py`

## 背景

spell を含む LLM 応答で、Building ログ / UI 表示用の HTML ブロック
(`<user_only alt="...">` + `/spell ...` + `<details class="spellResult"><summary class="spellSummary">...</summary>...</details>` + `</user_only>`)
が SAIMemory の messages にもそのまま保存されていた。本来 SAIMemory には plain な最終発言のみが残るべき (Building 履歴との内容重複 + HTML タグ混入を避ける)。まはーが自律 Track の sub_line Pulse (note_open spell) の記憶内で実際に観察。

## 原因

`_run_spell_loop` は `(full_merged_text(HTML 含む), final_continuation(plain), loop_count)` を返す。3 つの呼び出し経路のうち **streaming 経路** (`runtime_llm.py` ≈L2413) だけが spell 実行後に `text = _continuation_ns` で plain に置換してから state → SAIMemory に渡していた。

- **sync 経路** (≈L2648): 置換が抜けており、`full_merged_text` (HTML) が `state[output_key]` / `text` 経由で SAIMemory に保存
- **tool mode 経路** (≈L1898/1912): `result["content"] = _spell_text` (HTML) が後段 (≈L2069/2076) で `state[text_key]` / `text` に入り SAIMemory へ

自律 Track の sub_line Pulse は軽量モデルで sync 経路を通るため混入が顕在化した。非対称バグ = 実装ミスマッチ。

## 修正

3 経路を対称化 (emit には merged 全文 HTML を使い、SAIMemory 行きだけ plain に):
- sync 経路 (≈L2648): emit 後に `if _spell_loop_count_sync > 0: text = _continuation_sync` を追加
- tool mode 経路 (≈L1912): `result = {"type": "text", "content": _spell_continuation}` に変更

## 残課題

- 修正前に既に SAIMemory に入った汚染レコード (HTML 含む) は残る。7 層ストレージタブの削除 UI で除去可能。
- 実機検証: Pulse タイムラインで sub_line Pulse の保存 content が plain になっているか確認。

## ログ

- 2026-05-25: 根本特定 + 3 経路修正 (ruff / 既存テスト通過)。Pulse 可視化 (Pulse タイムライン) 整備の直後に追跡。サーバー再起動後に有効、実機検証待ち。
