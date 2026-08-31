# PulseController の割り込み復帰で args / pre_spells が失われる

**発見**: 2026-07-20、W3 (実行台帳 Phase 3 — schedule) の調査中。
**状態**: 未解決 (W3 スコープ外として分離)。

## 事実

`sea/pulse_controller.py` の `_queue_for_resumption` (:287 付近) は、割り込まれた
request の復帰用コピーを作るときに次のフィールドだけを引き継ぐ:

type / persona_id / building_id / user_input / metadata / meta_playbook /
event_callback / origin_track_id / is_resumption / original_prompt

**`args` と `pre_spells` が引き継がれない**。schedule 種別は `on_blocked="wait"`
なので、ユーザー会話などに割り込まれた schedule Pulse は復帰 queue に積まれるが、
復帰実行では PLAYBOOK_PARAMS 由来の Playbook 引数と事前スペルが落ちた状態で走る。

## 影響

- 引数必須の Playbook を持つスケジュールが、割り込み復帰時だけ引数なしで実行される
  (静かな挙動差 — 失敗ではなく「別の入力での実行」になるのが厄介)。
- pre_spells に依存する運用 (発火前のコンテキスト仕込み) が復帰時だけ抜ける。

## 修正方針 (案)

`_queue_for_resumption` のコピーに `args` / `pre_spells` を加える (1 行×2)。
`ExecutionRequest` の他フィールドにも同種の引き継ぎ漏れがないか、dataclass の
全フィールドと突き合わせて棚卸しする (「コピー箇所は定義とズレる」型の再発防止
として、`dataclasses.replace` ベースへの書き換えも検討)。
