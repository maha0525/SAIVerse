# Discord の人間の発話が、受け口の引数の食い違いで処理できない

**発見**: 2026-10-09 (v0.4 段 1-3 の設計調査。刺激の ID の配線先を洗っていて発見)
**状態**: 未解決 (修正は v0.4 実装計画 段 1-3 の ID 配線と同じ便で行う)

## 何が起きるか

Discord からの人間の発話が SAIVerse 本体へ渡る受け口で、呼ぶ側と受ける側の形が合っていない。この経路を発話が通った時点で例外になり、ペルソナに届かない。

- 呼ぶ側 `discord_gateway/saiverse_adapter.py:111` は `gateway_handle_human_message(message)` と引数 1 個で呼ぶ。
- 受ける側 `manager/gateway.py:60` の定義は `(message, context)` の 2 個 — TypeError。
- さらに受ける側が読む `message.author_name` / `message.persona_id` / `message.context` は、送られてくる `DiscordMessage` (`saiverse_adapter.py:24-31`) に存在しない — 仮に呼べても AttributeError。

## 影響の範囲 (未確認)

本番 (v0.3) の Discord 連携が実際にこの経路を通っているかは未確認 — 通っているなら本番でも人間の発話が落ちているはずで、動いている実績と矛盾する。実機の Discord 発話がどの経路で届いているか (この受け口を通らない別経路の有無) を、修正の前に backend.log で確かめる。

## 直し方 (段 1-3 で)

刺激の ID の義務化で同じ受け口に Discord のメッセージ ID を通すため、引数の形の修正 (context を運ぶ・DiscordMessage の欄を実物に合わせる) を同じ便で行う。
