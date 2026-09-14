# サーバー側でストリームが切れた回だけ、中断の通告が書かれない

**発見**: 2026-09-13 (まはーの実機報告: ⅰマークが二重 + 続きの生成が Gemini 3.5 Flash-Lite で「prefilled model turn を受け付けない」エラー)
**状態**: 検証待ち (まはーの実機確認 — 切れた発言の後ろに通告が入るか、続きの生成が Gemini で通るか)
**深刻度**: P2 — プリフィル不可の Gemini 系モデルでは、サーバー切断後の「続きの生成」が必ず失敗する

## 症状 (実機、2026-09-13 22:19-22:20)

1. 「メッセージの生成が途中で終了しました」の通知に ⅰ アイコンが二つ並ぶ。
2. その後の生成 (続きの生成) が `Gemini model gemini-3.5-flash-lite does not accept a prefilled model turn` で失敗する。

## 原因

生成の途中終了の後始末は三経路あり、そのうち一つだけ通告を書かない。

- **停止ボタンで止めた回** と **エラーで Beat が落ちた回** は `_settle_interrupted_utterance` (sea/runtime_llm.py) が走り、途中の発言の確定に加えて **「(ここで発言が中断されました)」を host 名義で建物の記録に置く**。取り込みがこれを user ロール + `<system>` に組み替えて在室ペルソナの記憶へ配るので、次の生成の会話末尾はユーザー側の発話になる。
- **サーバーがストリームを途中で切ったが部分文が残っている回** (今回の 500) は、部分文の確定と「言い切っていない」印と画面向け通知だけで Beat を閉じ、**通告を書かない** (sea/runtime_llm.py の `_stream_err` ブロック、~5440 行)。ペルソナの記憶の末尾が本人の途中発言のままになり、続きの生成のプロンプト末尾がモデル発話になる。Gemini 3.x の契約 (`supports_model_prefill=false`) は末尾モデル発話を受け付けないため、llm_clients/gemini.py の事前検査が拒否する。

ⅰ二重は、この画面向け通知の文面先頭にバックエンドが「ℹ️」を書き込み (runtime_llm.py:5451)、画面側も info 種別に自前のアイコンを描く (frontend/src/app/page.tsx:3365) ため。

## 直し方

1. サーバー切断の経路にも、停止経路と同じ通告 (`by_user=False` の文面「(ここで発言が中断されました)」、host 名義、在室者 + 本人の `heard_by`) を書く。書き込みは `_settle_interrupted_utterance` から共通の関数へ括り出し、両経路で同じ一枚を使う (同じ判断の書き分けを作らない)。
2. 画面向け通知の文面から「ℹ️ 」を外す (アイコンを描く権威は画面側)。

## 関連

- [continue_instruction_is_dead_code.md](continue_instruction_is_dead_code.md) (この調査で見つかった隣の欠陥 — 続きの生成の指示文は LLM に届いていない。撤去方向、別件)
- `docs/issues/archive/user_utterance_path_failure_inventory.md` (続きの生成の親設計)
- `docs/issues/archive/stop_path_and_lost_utterances` 系: 通告の文面と配り方の裁定は 2026-08-26/27 (`_settle_interrupted_utterance` docstring)
