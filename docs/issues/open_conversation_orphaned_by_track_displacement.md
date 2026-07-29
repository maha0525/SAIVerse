# Issue: 別 Track に押し出された会話の出来事が孤児になり、永久に閉じない

**ステータス**: 🔲 未着手（まはー裁定: issue 化。2026-07-29）
**優先度**: high（予定された行動が失われる実害あり・発生条件は限定的）
**作成日**: 2026-07-29
**関連**: `saiverse/track_manager.py`（`activate` / `create` の displaced 処理）/ `saiverse/saiverse_manager.py`（`_rearm_wait_response_timeout_on_load`）/ `saiverse/day_plan.py`（`is_in_user_conversation`）/ [life.md](../intent/life.md) §7.3

## 症状

会話中に別の Track が activate されると、対ユーザー会話 Track は pending へ押し出され、その wait_response タイマーは `_cancel_wait_response_timeout` でキャンセルされる。**しかし会話の出来事 (kind='conversation' の Episode) は閉じられない。**

閉じるのは `autonomy_wiring.handle_conversation_end` だけで、その入口は wait_response タイムアウトの発火。タイマーが消えた時点で、その会話を閉じる者がいなくなる。

## 実害

「いま会話中か」の正典は開いている会話の出来事（`day_plan.is_in_user_conversation`、life.md §7 案 Y）。孤児になった出来事は、

1. **コマの繰り下げが止まらない** — day_plan は「ユーザー会話中」としてコマを 10 分後へ繰り下げる。上限 3 回で `skipped` になり、**予定されていた行動がそのまま失われる**
2. **判断点の状況テキストが「ユーザーと会話中です」のまま** — `judgment_points.build_on_event_situation_text`。2026-07-29 に「running Track の種別」から「開いている会話の出来事」へ修正したが、**孤児が残る限りこちらでも嘘は残る**。しかも症状の向きが変わった: 修正前は「終了済みの会話を会話中と言う」、修正後は「孤児が開いている間、**実際に取り組んでいる別 Track を隠して**会話中と言う」。Codex 再レビューの指摘どおり、Episode と Track の寿命関係が未確定なままでは、開いている出来事だけを見る判定も安全ではない。ただし判定を `day_plan.is_in_user_conversation` へ一本化したこと自体は正しい（実装を 2 つに割らない）ので、**本 issue の裁定でまとめて閉じる**
3. 回復は「メタ判断が会話 Track を再び activate する」に依存する。ユーザーが発話しても、別 Track が running なら alert 経路に入るため直接には戻らない

## 発見経緯

2026-07-29、起動時 wait_response 再確立の空撃ち修正（life.md §7.3 表の追加行）に対する Codex 攻撃レビューで指摘。**この欠陥は当該修正が作ったものではなく、それ以前から存在する**（修正前も `get_running()` 起点で同じ経路を通る）。修正は起点を `get_running()` に置いたままなので、この孤児を解消しない。

## 検討中の方向（未裁定）

起動時の復旧を `get_running()` から切り離し、**開いている会話の出来事そのものを列挙して**、その `origin_ref` に対応する Track へタイマーを張る。あわせて次の不変条件をどちらかに固定する必要がある:

- **A**: Track が押し出されたら会話の出来事も閉じる（＝会話は running Track に従属する）
- **B**: 出来事は Track の押し出しと独立に生き、タイマーは Track ではなく出来事に紐づく（＝会話は Track と別の寿命を持つ）

A は「別 Track に移った瞬間に会話が終わったことになる」ため、ユーザーがまだ話している最中の切り替えで会話が切れる。B は Track と出来事の寿命が分離するので、タイマーのキーとキャンセル契機を出来事側へ移す設計変更を伴う。**どちらが life.md の「いま」の意味論に合うかの裁定が先**。

## 再現の当て（未実施）

開いている会話と running Track が食い違う状態を作る統合テストを追加する:

1. 対ユーザー会話 Track を running にし、会話の出来事を開く
2. 別の autonomous Track を activate（会話 Track は pending へ、タイマーは cancel）
3. 再起動相当（`_on_persona_registered`）を通す
4. 会話の出来事が開いたままで、タイマーがどこにも無いことを確認
