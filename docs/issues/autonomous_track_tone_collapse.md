# Issue: 自律行動 Track でペルソナの口調が崩れる

**ステータス**: 🩹 修正適用済み・実機検証待ち (2026-06-29)
**優先度**: low → (発生率100%のため実質 mid)
**作成日**: 2026-06-29
**関連**: `builtin_data/playbooks/public/track_autonomous.json`, `saiverse/pulse_scheduler.py`, `sea/runtime_graph.py`

## 背景

自律行動 Track (自律作業モード) でペルソナが発話すると、メインモードでの口調・人格と比べて**口調が崩れる**。発生率はほぼ100%。

## 原因究明 (2026-06-29, llm_io.log 実コンテキスト精査)

13:57:10 の自律 Pulse (スタックチャン用スタンプ機能ドキュメント化) の実 LLM 入力を
`llm_io.log` から読み解いた結果、3つの要因が判明した。

### ① `{track_context}` / `{track_id}` が未解決リテラルのまま (本命)
自律判断ノード `main_line_judgment` の action 冒頭が、解決されず生の文字列
`{track_context}` のまま LLM に渡っていた (履歴に15回以上蓄積)。

**根本原因**: `track_autonomous.json` の `input_schema` が `[input, metadata]` しか
宣言しておらず、`track_context` / `track_id` を受け取れていなかった。
- `saiverse/pulse_scheduler.py:303` は毎 Pulse `pulse_args["track_context"]`
  (タイトル/ID/intent/現在のタスクリスト) と `pulse_args["track_id"]` を組み立てて渡す。
- しかし `_args` → top-level state の昇格は **input_schema 宣言経由**でのみ起きる
  (`sea/runtime_graph.py:68-81`)。宣言が無い2変数は `_args` 止まりで state に上がらず、
  `_format` (`sea/runtime_utils.py`) が解決できずリテラル落ち。
- 結果、ペルソナは毎 Pulse の足場 (特に進行中タスクリスト) を失い、生の
  `{track_context}` がプロンプトを汚染していた。
- 比較: `track_user_conversation` / `track_social` は action に `{track_context}` を
  使わず、`_inject_track_context` (「## Track切替通知」SAIMemory メッセージ) で足場を渡す。
  track_autonomous だけ壊れた action-template 方式が混入していた。

### ② 「独白」指示が二重
action に「…何をすべきかを**内的独白で書いてください**」＋末尾「**発話は独白として
自然に書いてください**」。独白・ナレーション調へ二重に誘導していた。

### ③ 自律は軽量モデル
自律=`gemini-3.1-flash-lite`、通常会話=`gemini-3.5-flash`。軽量モデルは人格保持が弱く、
①②と相まって素の作業ログ調に転びやすい。

(※ head/system prompt の人格セクション — persona_self / エアの記憶 / まはーに関する記憶
等 — は会話時と**完全に同一**で入っており、欠落していなかった。)

## 適用済み修正 (2026-06-29)

`builtin_data/playbooks/public/track_autonomous.json`:
- **①**: `input_schema` に `track_context` / `track_id` を追加。これで `_args` →
  state 昇格が起き、`{track_context}` / `{track_id}` が実値に解決される
  (機構を `sea/runtime_graph.py:72-81` で確認済み)。DB 再 import 済み。
- **②**: 「内的独白で書いてください」→「あなたらしい言葉で、内なる声として書いて
  ください」。末尾「発話は独白として自然に書いてください」→「発話は、あなた本来の
  口調や人柄を保ったまま、内なる声として自然に綴ってください。作業ログのような
  無味な説明調にはしないでください」。(エア専用でなく全ペルソナ向けの汎用文言)

## 残り

- **実機検証**: backend 再起動後 (ランタイムは playbook を DB からキャッシュ読みするため
  再起動が必要)、自律 Pulse の口調が改善するか確認。リテラル `{track_context}` が
  実値に解決されていることも llm_io.log で確認できる。
- **③ モデル**: ①②で十分改善するか観察してから判断。まだ崩れるなら軽量モデルの
  見直し (自律ラインのモデル設定) を検討。
