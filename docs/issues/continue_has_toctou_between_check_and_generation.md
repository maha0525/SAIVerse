# 続きの生成にも「検査から生成開始までの競合窓」が残っている

**発見**: 2026-08-29 (生成の段階の信号の実装で retry の競合窓を塞いだ際、実装エージェントが「同じ理由の隣」として報告)
**状態**: 未解決 (設計の前提裁定が先)
**深刻度**: P3 — 到達には「同じ発言の続きの生成を、別タブ等からほぼ同時に二回起こす」が要る (単一タブではボタンが loadingStatus で無効化される)

## 症状

retry には 2026-08-29 の実装で「生成の開始が通る既存の排他 (Beat ロック) の内側で門番を再検査する」形 (`pre_generation_check`) が入ったが、**続きの生成 (continue) には同じ再検査が無い**。中断印 (`_interrupted`) の検査から生成開始までの間に別の continue が走ると、続きが二つ生まれて建物の記録とペルソナの記憶に二重に積む。

## なぜその場で直さなかったか

道具 (`pre_generation_check`) は今回作ったものがそのまま使える形だが、**再検査が読むべき台帳が二重になっている** — 印の更新は DB (building_messages の metadata) が正で、ストリーム側の検査は in-memory 履歴を読む。どちらを再検査の正とするかの裁定が先で、それ無しに実装すると「検査する場所ごとに違う台帳を読む」形が増える (同じ判断の書き分け — この束がずっと避けてきた型)。

## 直し方 (裁定後)

1. 印の正の台帳を一つに決める (DB が自然に見える)。
2. continue の生成開始に `pre_generation_check` を配線し、「対象の発言にまだ中断印が立っているか」を Beat ロックの内側で再検査。降ろされていたら中止して正直なイベント。

## 関連

- [stream_completion_is_not_proof_of_persistence.md](archive/stream_completion_is_not_proof_of_persistence.md) (実装済みの親設計)
- [retry_api_has_no_server_side_eligibility_check.md](archive/retry_api_has_no_server_side_eligibility_check.md) (同型の塞ぎ済み事例 — `sea/runtime.py` の `pre_generation_check`)
