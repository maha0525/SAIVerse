# Issue: test_slots_fire_on_real_dispatch_thread がフルスイート実行で間欠 fail

**ステータス**: 🔲 未着手（観測記録・様子見）
**優先度**: low
**作成日**: 2026-07-13
**関連**: `tests/test_autonomy_wiring.py::test_slots_fire_on_real_dispatch_thread`

## 事実

- 2026-07-13、ライフ Phase 4 完了後の全体スイート実行（`pytest tests -q --ignore=tests/test_avatar_pipeline.py`、6 分弱）で 1 回 fail した
- 直後の単独実行・ファイル単位実行（50 件）はいずれも passed
- 同日のライフ Phase 1〜4 の各サブエージェントによる全体実行（計 4 回、同じ変更込み）ではすべて passed（fail は既知の avatar_pipeline / addon_config_mcp_reconnect のみ）
- テスト自身に間欠の既往記録がある：本文コメント「共有 in-memory SQLite の癖で load_day_plan が一瞬 None を返すことがある（2026-07-07 に間欠観測）」— 当時の対策（締切後の再読）は入っている
- テストは実時刻・実 dispatch スレッドの統合テスト（deadline 20 秒のポーリング）

## 推測（未確定）

フルスイート並走の高負荷時に 20 秒 deadline を割る、既往症と同系のタイミング flaky の可能性が高い。ライフ実装（Phase 2 で発火経路に `consume_life_pulse` 呼び出しが追加されたが、lives 未宣言のこのテストでは no-op）が原因である積極的な証拠は無い——ただし排除もできていない（fail 時のどの assert で落ちたかのログは未取得）。

## 次に fail したら

1. fail 時の assert 位置とメッセージ（`slot did not fire ... (status=...)` か `day plan unreadable`）を記録する
2. 頻度が上がるようなら deadline 延長 or `pytest-repeat` での負荷再現を検討
