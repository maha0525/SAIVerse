# PulseController の割り込み復帰で args / pre_spells が失われる

**発見**: 2026-07-20、W3 (実行台帳 Phase 3 — schedule) の調査中。
**状態**: 保留（`args` 継承は実装済み。旧修正案は失効。実機での割り込み復帰の検証完了は未確認）。

## 現行仕様（2026-10-02 確認）

起票時の「`args` と `pre_spells` を両方コピーする」案を、そのまま実装してはいけない。
復帰時は**入力データを維持し、副作用のある事前アクションを繰り返さない**。

- [`PulseController._queue_for_resumption`](../../sea/pulse_controller.py) は既に
  `args=request.args` を引き継ぐ。生成前の再検査 `pre_generation_check` も引き継ぐ。
- `pre_spells` は意図的に引き継がない。メール送信・画像生成などを含み得るため、
  復帰のたびに再実行すると副作用が重複する。実装コメントは、未実行のまま中断された
  `pre_spells` が失われる窓を残す判断も記録している。これは今回解消したと扱わない。
- 復帰 request は新しく構築され、`pulse_id` と `cancellation_token` は新しいものになる。
  旧案の `dataclasses.replace` への機械的な置換も、そのままではこの区別を失う。

**参照先**: 起動・中断の設計は [Pulse dispatch](../intent/persona_cognition/pulse_dispatch.md)、
事前スペルの役割は [nested subline spell §13](../intent/persona_cognition/nested_subline_spell.md)。
復帰時のコピー契約は上記実装と
[`test_pulse_controller_resumption.py`](../../tests/test_pulse_controller_resumption.py) が記録する。

**検証範囲**: 隔離環境・LLM なしで上記回帰テスト 1 件を実行し、`args` の維持と
`pre_spells` の非継承を確認した。これは request のコピー契約の検証であり、実際の
スケジュール発火 → ユーザー割り込み → 復帰完了の実機検証を代替しない。

## 経緯: 起票時の記録（以下の修正案は現行手順ではない）

2026-10-02: 現行コード・回帰テストと照合し、入力と副作用の区別を冒頭に追記した。
以下は 2026-07-20 の問題認識と提案を残した履歴。

**発見**: 2026-07-20、W3 (実行台帳 Phase 3 — schedule) の調査中。
**状態**: 未解決 (W3 スコープ外として分離)。

### 事実

`sea/pulse_controller.py` の `_queue_for_resumption` (:287 付近) は、割り込まれた
request の復帰用コピーを作るときに次のフィールドだけを引き継ぐ:

type / persona_id / building_id / user_input / metadata / meta_playbook /
event_callback / origin_track_id / is_resumption / original_prompt

**`args` と `pre_spells` が引き継がれない**。schedule 種別は `on_blocked="wait"`
なので、ユーザー会話などに割り込まれた schedule Pulse は復帰 queue に積まれるが、
復帰実行では PLAYBOOK_PARAMS 由来の Playbook 引数と事前スペルが落ちた状態で走る。

### 影響

- 引数必須の Playbook を持つスケジュールが、割り込み復帰時だけ引数なしで実行される
  (静かな挙動差 — 失敗ではなく「別の入力での実行」になるのが厄介)。
- pre_spells に依存する運用 (発火前のコンテキスト仕込み) が復帰時だけ抜ける。

### 修正方針 (案)

`_queue_for_resumption` のコピーに `args` / `pre_spells` を加える (1 行×2)。
`ExecutionRequest` の他フィールドにも同種の引き継ぎ漏れがないか、dataclass の
全フィールドと突き合わせて棚卸しする (「コピー箇所は定義とズレる」型の再発防止
として、`dataclasses.replace` ベースへの書き換えも検討)。
