# Issue: 失敗を文字列で返すツールが、スペルの記録で「成功」に数えられる

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-10-10
**関連**: docs/intent/quick_spell.md §3.3 (2) (論理的失敗の宣言の規約) / docs/intent/autonomous_behavior_v04_plan.md §5 段 2 / `sea/runtime_llm.py` の `_declares_logical_failure` / v0.4 段 2 の Codex 敵対レビュー 2 巡目 (2026-10-10) の high 指摘

## 背景

スペルの実行結果が失敗かどうかを、スペルの仕組みは二通りでしか知らない。

1. 機械的な失敗: ツールの実行中に例外が出た、ツールが登録されていない、名前や引数が通らなかった、モードの許可で止められた。
2. 論理的な失敗: ツールが戻り値の metadata に `{"error": True}` を載せて「これは失敗です」と宣言した (quick_spell.md §3.3 (2) の規約)。

どちらでもない返り方 — 例外を投げず、「エラー: …」のような失敗の文面を素の文字列で返す — は、仕組みからは成功と区別がつかない。文字列の中身で判定するヒューリスティックは使わない方針 (quick_spell.md 不変条件) なので、この形のツールは失敗しても周の記録の `success` が真のままになる。

結果として次のものが失敗を成功として扱う。

- スペルの折りたたみ表示 (失敗の × ではなく成功の ☆ になる) と、続きの生成に渡す札 (`[Spell Error]` ではなく `[Spell Result]`)。
- activity_trace の `success` 欄。
- /quick_spell の終端判定 (失敗があれば応答機会を返すはずが、成功として黙って終端する)。
- 一回で閉じる Beat (ティック) の帰結の知覚で、失敗した行に付く印 (`（失敗）`)。

2026-10-10 の修正で、帰結の知覚は成功も失敗も全部届けるようになったので、失敗の文面そのものは本人に届く。印が付かないだけで、本人は文面を読めば失敗に気づける。ただし quick_spell の終端判定と表示は、宣言が無い限り成功として読み続ける。

宣言を入れているツールは、2026-10-10 時点で `tell` だけ (`builtin_data/tools/tell.py` の `_failure`)。

## 同族 (Codex レビューが挙げたもの + 確認したもの)

- `schedule_add` / `schedule_delete` / `schedule_list` — 入力の拒否・対象なし・内部エラーを `"エラー: …"` の素の文字列で返す。
- `memory_read` / `memory_write` / `memory_delete` / `memory_open` / `memory_close` / `memory_clip` — `AtlasRefError` を `f"Error: {exc}"` の素の文字列で返す。
- `read_url_content` — 取得・変換の失敗を `(文字列, ToolResult)` で返す (`ToolResult` は snippet の器で、失敗の印にはならない)。
- `run_playbook` — 拒否 (権限・資格情報・Playbook 不在・深さの上限) と子ラインの実行失敗を `"[run_playbook error] …"` の素の文字列で返す。

他のツールにも同じ形があるはずなので、直すときは `builtin_data/tools/` 全体を棚卸しする。

## 直し方

各ツールの失敗の返却を、metadata に `{"error": True}` を載せる形に揃える。

- 素の文字列を返しているツールは `(文字列, {"error": True})` を返す (`tell.py` の `_failure` と同じ形)。
- `(文字列, ToolResult)` のように 2 要素目を別の用途に使っているツールは、`tools.core.parse_tool_result` の 4 要素形 `(文字列, ToolResult, None, {"error": True})` で metadata を運ぶ。
- 宣言を入れたツールごとに、`_run_spell_loop` を通して周の記録が `success=False` になる回帰テストを足す (表示の × と `[Spell Error]` の札、quick_spell の昇格、ティックの帰結の知覚の印)。

文字列の中身による判定は入れない (規約の方針どおり)。

## ログ

- 2026-10-10: 起票。同じ日に `_declares_logical_failure` を周の記録の組み立て (スペルループの `valid_results` と pre_spells の `results`) に入れ、`meta.error` の宣言が `success=False` に写るようにした。宣言を持つツール側の棚卸しはこの issue に残す。
