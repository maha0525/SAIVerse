# outbox 配達・知覚 push の門の同族 4 点 (2026-09-07 走査の残り)

**起票**: 2026-09-07 (部屋の様子 §11 のレビュー一巡目で「配達の冪等・検証・未 ready」の型を 3 件直した後、同じ理由が当てはまる隣を全域走査した結果)
**状態**: 未着手 — いずれも現時点で実害の報告なし。該当箇所を触る作業のついでに直すのが釣り合う

欠陥の種類を一文にすると: **配達・push の境界で「壊れた入力を成功扱いにする」「同じ配達を二度実行する」「未 ready を黙って成功にする」のいずれかが残っている箇所**。2026-09-07 に `perception.room_state` で同じ 3 型を直した (docs/intent/room_state_packages.md §11-3-1)。

1. **`move.post_addon_hooks` に冪等キーがない** (`saiverse/execution_ledger_wiring.py`) — 再配送のたびに `persona_exited/entered_building` の addon hook が再発火する。実害は addon の副作用次第。移動の再実行の族なので、[entry_delivery_retry_duplicates_room_perception.md](entry_delivery_retry_duplicates_room_perception.md) の「刺激の永続 ID」の裁定に合流させるのが筋。
2. **`perception.push` handler の `salient` が `bool(...)` で型化けする** (`saiverse/execution_ledger_wiring.py` の `_make_perception_push_handler`) — `"false"` が True に化ける型。現在の積み手は実 bool を渡すので潜在的。厳格化する場合は先に積み手の全数調査が要る (None 等を渡す積み手がいると配達失敗へ挙動が変わる)。
3. **`saiverse/day_plan.py` `_record_move_failure` が未 ready を黙殺** — 移動失敗の通知が静かに消える。成功記帳を伴わない best-effort なので実害小。WARN を足すだけでも見えるようになる。
4. **`api/routes/people/core_memory.py` `_notify_persona_correction` が未 ready を WARN なしで silent return** — コア記憶訂正の本人通知が消える。訂正本体は成立しているが、本人が訂正を知らされない形は自己像の尊厳の設計に絡むので、優先度は 3 より上。
