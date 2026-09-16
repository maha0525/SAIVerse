# 続きの生成の指示文 (CONTINUE_INSTRUCTION) は一度も LLM に届いていない

**発見**: 2026-09-13 (続きの生成が Gemini 3.5 系で「末尾がモデル発話」と拒否される件の調査中)
**状態**: ✅ 解決済み (2026-09-16 撤去完了 — 方向は撤去 — 2026-09-13 まはー裁定「直すっていうか撤去する流れ」)
**深刻度**: P3 — 挙動の実害は無い (届いていないものを消すだけ)。無意味なコードが「届いている」顔で残っていることが害

## 事実

`manager/runtime.py` の `CONTINUE_INSTRUCTION` (「あなたの直前の発言は、途中で途切れたまま終わっています。その続きを…」) は、続きの生成の Pulse に `user_input` として渡されるが、**プロンプトに載る経路がどこにも無い**。

プロンプトに文章が届く道は二つで、この指示文はどちらにも乗っていない。

1. **履歴の道**: 建物の記録に保存された発言が、次の生成のときに履歴としてプロンプトに載る。通常のユーザー発言はこの道 (送信時に API 側が先に保存する)。指示文は「記憶に残さない」約束で保存しないので、乗らない。
2. **Playbook のテンプレートの道**: ノードの `action` テンプレートが `{input}` を描画すればプロンプト末尾に載る。会話用 Playbook (`track_user_conversation` / `sub_speak`) の LLM ノードは `action` を持たないので、乗らない。

`user_input` は Pulse の状態変数 (`state["input"]` / `state["last"]`) に積まれるだけで、どのノードも読まずに終わる。

続きの生成が Claude 等で「動いて見えていた」のは、指示のおかげではなく、会話の末尾がペルソナの途中発言のままだと続きから書く挙動 (プリフィル) がプロバイダ側の仕様として在ったから。テスト (`tests/test_user_utterance_durability.py`) は Pulse を丸ごとモックしており、指示文が LLM に届くかを検証する網は無かった。

## 撤去の中身

- `manager/runtime.py` の `CONTINUE_INSTRUCTION` 定数 (974-978 行) と、その由来を説明する直前のコメント塊、`continue_persona_message_stream` からの参照 (1217 行) を撤去する。`_stream_persona_pulse` へは空の入力を渡す形に直す。
- 監査文書 `docs/audits/2026-09-09_product_verification_inventory/inventory.md` の CHAT-05 は「入力欄の席に指示文を載せる」と**誤った現状認識**を記録している。監査記録は歴史なので書き換えず、本 issue を訂正の記録とする。

## 撤去しても挙動が変わらない根拠

届いていないものを消すだけなので、続きの生成の挙動は変わらない。中断の文脈をペルソナに伝える仕事は通告「(ここで発言が中断されました)」(建物の記録 → 取り込みで user + `<system>` として各ペルソナの記憶へ) が担っており、2026-09-13 の修正でサーバー側切断の経路にも通告が入る。

「言い直しや要約をせず、続きだけを述べよ」という指示を本当に届けたい場合は、届ける仕組み (保存するか、テンプレートに載せるか) の設計が別途要る — それをやるかどうかは撤去とは独立の判断で、現時点ではやらない (通告だけでひとまず足りる、2026-09-13 まはー)。

## 関連

- [continue_has_toctou_between_check_and_generation.md](continue_has_toctou_between_check_and_generation.md) (同じ続きの生成の別の未解決)
- `docs/issues/archive/user_utterance_path_failure_inventory.md` (続きの生成の親設計)

## ログ

- 2026-09-13: issue 起票 (Gemini 3.5 系で続きの生成が拒否される件の調査中に発見)。同日、まはーの撤去裁定。
- 2026-09-16: 撤去完了。`CONTINUE_INSTRUCTION` 定数と由来のコメント塊、`continue_persona_message_stream` からの参照を撤去し、`_stream_persona_pulse` へは空の入力を渡す形にした。監査文書の CHAT-05 は書き換えていない (本 issue が訂正の記録)。`tests/test_user_utterance_durability.py` 34 件緑、`ruff check` 通過。
