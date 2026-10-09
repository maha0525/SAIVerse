# Discord の人間の発話が、受け口の引数の食い違いで処理できない

**発見**: 2026-10-09 (v0.4 段 1-3 の設計調査。刺激の ID の配線先を洗っていて発見)
**状態**: 未解決 — 入口は fail-closed で閉じてある (2026-10-10 — 下の「入口を閉じた (2026-10-10)」)。正しい取り込み経路 (チャンネル対応の建物と送信者の素性を運ぶ) が未実装。旧状態: 実装済み・実機検証待ち (2026-10-09 段 1 — 下の「修正 (2026-10-09 段 1)」。本番 (v0.3) では使われていない経路だったことを確認済み)。その前: 未解決 (修正は v0.4 実装計画 段 1-3 の ID 配線と同じ便で行う)

## 何が起きるか

Discord からの人間の発話が SAIVerse 本体へ渡る受け口で、呼ぶ側と受ける側の形が合っていない。この経路を発話が通った時点で例外になり、ペルソナに届かない。

- 呼ぶ側 `discord_gateway/saiverse_adapter.py:111` は `gateway_handle_human_message(message)` と引数 1 個で呼ぶ。
- 受ける側 `manager/gateway.py:60` の定義は `(message, context)` の 2 個 — TypeError。
- さらに受ける側が読む `message.author_name` / `message.persona_id` / `message.context` は、送られてくる `DiscordMessage` (`saiverse_adapter.py:24-31`) に存在しない — 仮に呼べても AttributeError。

## 影響の範囲 (2026-10-09 に確認済み — 下の「影響の範囲の確認」)

本番 (v0.3) の Discord 連携が実際にこの経路を通っているかは未確認 — 通っているなら本番でも人間の発話が落ちているはずで、動いている実績と矛盾する。実機の Discord 発話がどの経路で届いているか (この受け口を通らない別経路の有無) を、修正の前に backend.log で確かめる。

## 直し方 (段 1-3 で)

刺激の ID の義務化で同じ受け口に Discord のメッセージ ID を通すため、引数の形の修正 (context を運ぶ・DiscordMessage の欄を実物に合わせる) を同じ便で行う。

**影響の範囲の確認 (2026-10-09 段 1-3)**: この受け口は本番 (v0.3) では使われていない経路だったことを確認した (段 1-3 のコミット 8663242b の記録)。上の「動いている実績と矛盾する」は、本番の Discord の発話がこの受け口を通っていなかったことで説明がつく。

## 修正 (2026-10-09 段 1、コミット 8663242b)

受け口 `manager/gateway.py` の `gateway_handle_human_message` を呼ぶ側に合わせて引数 1 個にし、どのチャンネル (= 建物) かは封筒の `message.context` から読む形にした。封筒 `DiscordMessage` (`discord_gateway/saiverse_adapter.py`) には実物に合わせて `message_id` (relay bot の `payload.message_id`) と `author_name` の欄を足した。Discord のメッセージ ID は `client_message_id = "discord:<id>"` として発話の保存 (building_messages、一意制約) まで運ぶので、relay bot の再送やゲートウェイの再接続で同じ発言がもう一度届いても、保存の段で既存の行に合流して何も起動しない。ID の無い発言は ERROR を残して落とす (受け口で代理採番しない — [刺激の ID の issue](on_event_judgment_has_no_idempotency_key.md) の決定 1)。あわせて、受け口が自分で履歴へ書いていた二重書き込み (`_append_gateway_history`) を外した — 経路が動けば同じ発話が二行になり、ペルソナに二度聞かせることになるため。回帰は `tests/test_discord_gateway_human_message.py` (本物のアダプタから building_messages の保存まで。ペルソナ・LLM には触れない)。

残り: Discord を有効にした隔離環境か実機で、人間の発話が一度だけペルソナに届くことを見る。

## 入口を閉じた (2026-10-10、段 1 の Codex 敵対レビュー)

Codex レビューが、上の段 1 の修正 (署名と封筒の欄を呼び手に合わせた) の後でも経路の行き先が間違っていることを隔離再現で確認した。受け口が呼んでいた本体側の取り込み口 `handle_user_input` (`manager/runtime.py`) は、発話を **Web ユーザーの現在地** (`state.user_current_building_id`) に、**オーナー本人の発話として** 流す。Discord チャンネルに対応する建物 (`message.context.building_id`) も、送信者が誰か (Discord ユーザー) も、この取り込み口には渡せない。そのため署名が直ったことで、第三者の Discord の発言が、別の部屋で、オーナーの言葉として、ペルソナの記憶・認知・課金に作用する経路が開いていた — 部屋と著者の両方が間違う。

対処として、受け口 `gateway_handle_human_message` (`manager/gateway.py`) は `handle_user_input` を呼ばず、ERROR を残して `[]` を返す (保存しない・ペルソナを起動しない・応答コマンドを出さない)。Discord のメッセージ ID の無い発言は、従来どおり relay bot の義務違反として別の ERROR で記録する。回帰は `tests/test_discord_gateway_human_message.py` (本物のアダプタから受け口を通し、取り込み口・building_messages・Pulse のどれにも届かないことを見る)。

正しい経路の要件 (この issue を閉じる条件):

- 発話を、Discord チャンネルに対応する建物 (`message.context.building_id`) に保存し、その建物で応答する。Web ユーザーの現在地に依存しない。
- 送信者の素性 (Discord ユーザー — ID と表示名) を発話の記録に明示する。オーナー本人の発話として記録しない。
- 冪等キーは段 1 の決定どおり `client_message_id = "discord:<Discord のメッセージ ID>"` を使う。

上の段 1 の節の「残り: 実機で人間の発話が一度だけペルソナに届くことを見る」は、この経路ができるまで対象が無い。
