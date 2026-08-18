# Issue: デバッグの「完全手動モード（全タイマー停止）」は v1 の亡霊——退役 or 実態縮退の裁定済み掃除

**ステータス**: 💤 v3 待ち凍結（掃除対象であることはまはー裁定済み 2026-07-14。実施時期は自律行動 v3 の運転設計と一体 — 下の「現況」参照。2026-08-18 実態確認）
**優先度**: low（実害なし・表示と実態の乖離）
**作成日**: 2026-07-14
**関連**: `frontend/src/components/DebugPanel.tsx`（完全手動モードトグル）/ `api/routes/people/debug.py` / `saiverse/saiverse_manager.py` L295・L1625（現役の読み手）/ `docs/intent/persona_cognition/debug_controller.md`

## 経緯

ライフ v0.5 改修B（v1 亡霊の掃除、2026-07-14）で、まはーの掃除指示にあった「タイマー停止」の正体がこれと判明。改修B の実装時は debug_controller.md の intent がある現役デバッグ機能と判断して残したが、**まはーの裁定は掃除対象**。

## 実態（2026-07-14 確認）

- UI 文言「完全手動モード: OFF 全タイマー停止して手動へ」——「全タイマー」は v1（SubLineScheduler の 30 秒 Pulse・50 分 tick が主駆動だった頃）の言葉
- v2 でこのモードが実際に止めるのは **wait_response タイムアウトの予約抑止**（`saiverse_manager._wait_response_timeout_provider` が対象ペルソナで None を返す）にほぼ縮んでいる
- 機能の読み手は現役（消すなら読み手ごと）

## 対応方向（次セッションで裁定）

1. **丸ごと退役**: v2 のデバッグは自律行動トグル（`AUTONOMY_ENABLED`）とライフ設定で足りるなら、モードごと削除（UI・API・provider 分岐・debug_controller.md 改訂）
2. **実態縮退**: 「会話の応答待ちタイマーを止める」という現役の一機能に名前と文言を合わせて残す

まはーのデバッグ実需（まだ使う場面があるか）を聞いてから決める。

> **2026-07-14 追記**: 同日に `ACTIVITY_STATE`（Stop/Sleep/Idle/Active）が**解体**され、`AUTONOMY_ENABLED`（真偽値・既定 ON）1 本になった（[landscape §9](../overview/landscape.md)）。案 1 の「ACTIVITY_STATE で足りるなら」は自律トグルに読み替え済み。この解体は本 issue の追い風になる — 4 値のうち 3 値が名前だけの飾りだったのと同じ構図（**UI に名前があるのに実態が伴わない**）が、この「完全手動モード」にもある。

## 現況（2026-08-18 確認）

**閉じられない。現物は手つかずで残っている。** v0.3.0 の門 §6 では「掃除裁定済み・退役が素直と明記済み」として archive 候補に並んでいたが、裁定が下りているのは「掃除する」ところまでで、退役か縮退かの選択も、実際の掃除も行われていない。

- UI のボタン文言は今も「全タイマー停止して手動へ」のまま（`frontend/src/components/DebugPanel.tsx:145`）。
- 読み手も現役で、`api/routes/people/debug.py` のトグルと状態表示、`saiverse/execution_ledger_wiring.py` の「行動を生む仕事だけ止めて掃除は止めない」二分、`tests/test_wait_response_timeout_gate.py` と `tests/test_schedule_reconciliation.py` の回帰がこのフラグにぶら下がっている。
- 同じ機能を扱う監査側の W9（柱 7）は、2026-08-16 の棚卸しで **v3 待ち凍結**になった（[audit_remediation_plan.md](../overview/audit_remediation_plan.md) の W9）。凍結の理由は、自律行動の運転そのものが [autonomous_behavior_v3](../intent/autonomous_behavior_v3.md) で再設計中であり、v2 の配線に gate を新設しても土台ごと入れ替わるため。A10 の finding は未解決のままだと明記されている。

**したがって本 issue も v3 待ちとして扱う。** 何が自動で発火するのかが v3 で決まらないと、「全タイマーを止める」の正しい実態が定義できないため。ただし**この結びつけは W9 の凍結理由を本 issue に当てはめた私（Claude）の判断であって、まはーの裁定ではない** — v3 の運転設計に入る時点で、掃除の形（退役か縮退か）と併せて裁定を仰ぐ。
