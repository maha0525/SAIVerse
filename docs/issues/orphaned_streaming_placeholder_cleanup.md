# Issue: Beat が例外で落ちると、空の下書き行が建物の記録に残り続ける

**ステータス**: 🔲 未着手 (2026-08-26 起票、意図的に切り出したもの)
**優先度**: low — 害はゴミが溜まることに限られる (画面にも記憶にも出ないことを実測で確認済み)
**関連**: `sea/runtime_emitters.py` `emit_speak_start` / `emit_speak_finalize` ・
`sea/runtime_llm.py` の pipeline streaming ・
[user_utterance_path_failure_inventory.md](user_utterance_path_failure_inventory.md) 束 2

## 何が起きているか

ペルソナが喋り始めるとき、`emit_speak_start` が **`content: ""` の行を先に建物の記録へ置く**
(`runtime_emitters.py:243-253`)。ストリーミングの断片は画面へ送られるだけでこの行には書かれず、
最後に `emit_speak_finalize` が本文を流し込んで確定する。

中断されても確定は通る経路が用意されている:

- サーバー側のストリーム中断 → 部分文で確定 + `_interrupted` の印 (2026-08-25 実装)
- ユーザーの停止 → 同じく部分文で確定 + `_interrupted`
- `speak: false` のノード → 空文字で確定

**残っているのは「Beat が例外で丸ごと落ちた場合」だけ。** そのときだけ確定が走らず、
`content: ""` の行が `_streaming_placeholder: True` のまま残る。

## 害はどこまでか (2026-08-26 実測)

- **画面には出ない** — 履歴 API が content 空を除外する (`api/routes/chat.py:293-296`)
- **誰の記憶にも入らない** — 取り込みも content 空を `consumed` で飛ばす
  (`builtin_data/tools/get_building_messages.py:206`)
- 残るのは `building_messages` テーブルと `log.json` の実体だけ

つまり**記憶の汚染ではなく、ゴミと seq 番号の消費**。放置しても嘘は生まれない。

## なぜ切り出したか

置き場が「placeholder の発番 (`_emit_speak_start`) から finalize までの**約 300 行**」を
try/finally で囲む形になる。システムで最も熱い経路への構造変更で、害の小ささに見合わない。
束 2 を緑で仕上げた時点で、その危険を積む価値が薄いと判断した (まはーへ報告済み)。

## 直し方の候補 (未検討)

1. pipeline streaming の区間を try/finally で囲み、`pipeline_msg_id` が生きていたら
   空文字で finalize する。**最も直接的だが、囲む範囲が大きい**
2. Beat の終わり (`_finalize_beat`) で「発番したが確定していない placeholder」を回収する。
   そのためには placeholder の id を Beat の器へ載せる必要がある
3. 起動時に `_streaming_placeholder: True` のまま残っている行を掃く。
   実行中の Beat の placeholder を誤って消さないよう、起動直後に限る

どれを採るにしても、**中断と停止の経路は既に印つきで確定している**ので、対象は例外経路だけ。
