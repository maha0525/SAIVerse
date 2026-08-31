# Issue: SubLineScheduler が ACTIVITY_STATE を見ずに sub_line Pulse を回す

**ステータス**: ❌ 対象消滅により閉鎖 (2026-08-09 archive、v0.3.0 の門 §6 の棚卸しで確認)
— SubLineScheduler (`saiverse/pulse_scheduler.py`) は 2026-07-06 に削除され (v1→v2 切り替え、commit eb5ccca)、ACTIVITY_STATE カラム自体も 2026-07-14 に撤去された (landscape §9)。指摘の主語と述語の両方が存在しないため、修正対象なし。
**優先度**: medium
**作成日**: 2026-05-25
**関連**: `saiverse/pulse_scheduler.py:156-180` (`_tick_persona`)、`saiverse/meta_layer.py:377-384` (on_periodic_tick の Active 抑止)、`docs/intent/persona_cognition/04_handlers.md`

## 背景

`SubLineScheduler._tick_persona` は ACTIVITY_STATE を一切見ず、全ペルソナの running な autonomous Track に対して 30 秒間隔 (default_pulse_interval) で sub_line Pulse を起動する。コード内コメント (`pulse_scheduler.py:158`) に「Phase C-3b 最小実装では ACTIVITY_STATE フィルタは未実装 (Active 化機構が無いため、全ペルソナを対象)。Phase C-3c 以降で ACTIVITY_STATE=Active の制約を追加する」とある通り、最小実装の名残。

結果として ACTIVITY_STATE=Sleep / Idle のペルソナでも、autonomous Track が running のまま残っていると 30 秒ごとに Pulse が走り続ける。これは ACTIVITY_STATE の意味 (Active 以外は自律稼働しない) と矛盾する。メタ判断側 (`MetaLayer.on_periodic_tick`) は既に `activity_state != "Active"` で skip する抑止を持っているので、SubLineScheduler だけが抜け穴になっている。

## 解決案候補

- `_tick_persona` の冒頭で `persona.activity_state == "Active"` を条件に追加し、Active 以外なら early return する。`on_periodic_tick:377-384` の抑止ロジックと揃える。
- ペルソナの activity_state 属性名は要確認 (`meta_layer.py` では `getattr(persona, "activity_state", "Idle")` で読んでいる)。

## 関連リソース

- `saiverse/pulse_scheduler.py:156-180` — `_tick_persona` (抑止を足す対象)
- `saiverse/meta_layer.py:377-384` — `on_periodic_tick` の ACTIVITY_STATE 抑止 (参考実装)
- 発見経緯: UC-2「割り込みと復帰」(persona_cognition Phase 5) の自律 Track 運用フロー精査中に判明 (2026-05-25)

## ログ

- 2026-05-25: 起票。UC-2 検証のための運用フロー把握中に発見。今すぐ直す必要はない (まはー判断) が、自律稼働の本格運用前に塞ぐべき。
