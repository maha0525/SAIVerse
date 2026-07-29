# Issue: 起動時のタイマー再確立が、無応答の期限を再起動のたびに 30 分延長する

**ステータス**: 🔲 未着手（まはー裁定: issue 化。2026-07-29）
**優先度**: medium（会話中の再起動時のみ・実害は「会話が閉じるのが遅れる」）
**作成日**: 2026-07-29
**関連**: `saiverse/track_manager.py` `_schedule_wait_response_timeout`（base_time のフォールバック）/ `saiverse/saiverse_manager.py` `_rearm_wait_response_timeout_on_load` / [life.md](../intent/life.md) §7.3

## 症状

`_schedule_wait_response_timeout` は、保存済みの最終メッセージ時刻が現在より過去なら **必ず `now` を基準に置き換える**。

```python
if base_time is None or base_time < now:
    base = now
```

この規則は起動時の再確立にも同じく適用される。そのため最終発言から 29 分後に再起動すると、残り 1 分ではなく**新たに 30 分**待つ。再起動を繰り返せば、開いている会話は期限を超えても閉じない。

## なぜこの規則があるか（消してはいけない理由）

長く放置された Track をペルソナが自分で activate した瞬間、素直に計算すると「過去 + 30 分 = まだ過去」で即タイムアウトし、「即発火 → 即判断 → 再 activate」のタイトループに落ちる（2026-05-10 の修正、`_schedule_wait_response_timeout` の docstring に経緯あり）。**新規 activate では `now` 基準が正しい。**

問題は、新規 activate と起動時の復旧が同じ経路を通っていること。復旧は「既にある期限を復元する」操作であって、「新しく呼びかけを始める」操作ではない。

## 検討中の方向（未裁定）

新規 activate と起動復旧を別モードにし、復旧時は `last_message_time + timeout` を本来の期限として使う。期限を既に超えていた場合は即時に発火条件を再評価し、**開いている会話が確認できたときだけ**終了処理へ進める。

注意: 期限超過での即時発火は、2026-07-29 に修正した空撃ち（終了済み会話への post_conversation）と隣接する。開いている会話の確認を必ず先に置くこと。

## 隣接して発生し、同日に閉じた件（2026-07-29）

判定不能時の読み取り再試行（`_schedule_rearm_retry`、30/120/300 秒）が、待っている間にユーザー発話で
張られたタイマーを同じキー（`wait_response_timeout:{track_id}`）で上書きし、**期限を最大 300 秒
後退させる**競合があった。当初これを「同じ家族の穴」として本 issue へ先送りしたが、**それは誤った
仕分けだった** — 本 issue は案 Y 以前からある `base_time` の丸めの話で、あちらは 2026-07-29 の
再試行機構が新規に持ち込んだ競合。別物を同じ箱に入れて先送りにしていた。

`_wait_response_timer_already_armed` による歯止め（有効な予約が既にあるなら再確立しない）で同日に
閉じ、回帰テストも追加済み。**本 issue とは独立**。

## 発見経緯

2026-07-29、起動時 wait_response 再確立の空撃ち修正に対する Codex 攻撃レビューで [medium] として指摘。同レビューの [high] 2 件のうち、判定不能時の再試行は同日修正済み、会話の出来事の孤児化は
[open_conversation_orphaned_by_track_displacement](open_conversation_orphaned_by_track_displacement.md) へ分離した。**本 issue は孤児化の方の裁定（タイマーを Track に紐づけるか出来事に紐づけるか）に影響されるため、そちらの後に着手するのが順序として自然。**
