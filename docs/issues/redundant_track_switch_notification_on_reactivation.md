# Issue: 同一 Track への復帰で「Track 切替通知」が毎回注入される

**ステータス**: 🟣 追補修正済・実機検証待ち (2026-07-16 — life.md §7 案 Y Phase 1 の実機追跡で、既存 running Track の直接応答経路が一回限りの wait_response タイマーを再装填していない回帰を確認・修正。二度目の会話 Episode が timeout で閉じる回帰テストを追加)
**優先度**: medium（表面化しており早め対応をまはー希望）
**作成日**: 2026-07-08
**上位**: この症状の根は「Track が進行状態管理から目的の指し示しに変質したのに、時間経過での自動 pause が古い意味論を引きずっている」こと。設計の親は [autonomous_v2_post_live_gaps.md](autonomous_v2_post_live_gaps.md) の**束C（Track の意味論の再整理）** → 解決設計は **[life.md](../intent/life.md) §7（案 Y）**: wait_response タイムアウトから pause を抜き「いま」の読み出しを開いているエピソードへ一本化することで、同一 Track 再 activate という事象ごと消滅させる（通知の出し分け修正は不要になる）。
**先行症状止め**: life.md 実装を待たず「同一 Track 復帰では通知を出さない」最小ガード（下記 修正方向 第一候補）を先行投入してよい（まはー早め希望）。ただし life.md landed 時に撤去する使い捨てと明記する。
**関連**: `saiverse/track_manager.py` `activate`、`saiverse/track_handlers/user_conversation_handler.py` `on_track_activated` / `_inject_track_context`、`docs/intent/persona_cognition/pulse_dispatch.md` §5

## 症状

sophie_city_a の SAIMemory に、**同じ対ユーザー会話 Track (track:2「対 まはー会話」) 宛ての `## Track 切替通知`** が何度も積もっている。ペルソナは別の Track (自律等) に移ったわけではなく、ずっと同じ会話 Track に居るのに、「切替通知」が繰り返し入る。

実測 (2026-07-08): 該当 Track 宛ての切替通知は全履歴で 7 件。すべて「30 分以上空けて会話に戻ったタイミング」と 1 対 1 で一致。タイトループではなく、**会話を再開するたびに 1 件ずつ積もる**構造。

## 原因

### 2026-07-16 実機追跡で判明した追補回帰

life.md §7 案 Y Phase 1 (コミット `6257b6a`) で wait_response タイムアウトから
Track の pause を撤去した結果、同一 Track の再 activate と通知重複は設計どおり
消滅した。一方、タイムアウト予約は一度発火すると消費される一回限りの予約であり、
タイムアウト後も Track が running のままなので、次のユーザー発話は activate を
通らず `on_user_utterance` の直接応答経路へ入る。この経路は新しい conversation
Episode を開いていたが、タイムアウトを再装填していなかった。

air_city_a では 2026-07-15 13:36 開始の Episode 39 が閉じないまま残り、
2026-07-16 の日計画は「会話中」と判定され続けた。その結果、00:30 以外の全コマが
3 回延期後に `deferral_limit` でスキップされた。

修正は、既存 running Track の同期メインライン応答完了後に
`ensure_wait_response_timeout(persona_id)` を呼び、一回限りの予約を毎回再装填する。
応答本体が失敗した場合でも開いた Episode を放置しないよう `finally` で実行し、
再装填自体の失敗は二重応答を避けるためログへ記録して吸収する。

### 発火点

`TrackManager.activate()` → `on_track_activated` フック → `UserConversationHandler._inject_track_context()` が SAIMemory に `## Track 切替通知` を注入する (`track_manager.py:596-599`、`user_conversation_handler.py:368-372`)。

`activate()` は running になった瞬間に**無条件で**フックを呼ぶ。「別 Track からの切替」と「同じ Track への復帰」を区別していないため、同一 Track の再 activate でも通知が入る。

### なぜ同一 Track が繰り返し activate されるのか

**wait_response の自動 pause (デフォルト 30 分、`SAIVerseManager._DEFAULT_WAIT_RESPONSE_TIMEOUT_MINUTES=30`)** が running→pending に落とすため。サイクル:

1. ユーザー発話 → track は `pending` → `on_user_utterance` が衝突なしと判定 → `activate` → **切替通知注入** → 応答 (`user_conversation_handler.py:535-546`)
2. 最終メッセージから 30 分で wait_response timeout → `paused` (running→pending)
3. 次のユーザー発話 → また `pending` → `activate` → **切替通知 (2 回目)**

ログ実例:

```
07-08 16:27:04  activated track:2 → on_track_activated → 切替通知注入 + wait_response timeout 予約(30分)
07-08 16:57:39  wait_response timeout reached → paused (running→pending)
07-08 19:53:33  activated track:2 → 切替通知注入(また) + timeout 予約
07-08 20:25:08  wait_response timeout reached → paused
```

## 論点

- **意味論の歪み**: ペルソナは別 Track へ移っていない。「まはー待ち」で pending に落ちて戻ってきただけ。それを `activate` が「切替 (switch)」として扱うのがおかしい。これは切替ではなく**会話の再開**。「切替」という語自体も誤解を生む。
- **コンテキスト汚染**: 同じ Track に対する同文の切替通知が SAIMemory に積み上がる。ペルソナの認知履歴としてノイズ。

## 修正方向 (未確定・議論用)

第一候補: **`_inject_track_context` を「実際に別 Track から切り替わった時だけ」に絞る**。

- `activate()` に「直前まで running だった Track (displaced)」の情報を渡す
- displaced が別 Track → 従来通り「切替通知」
- displaced 無し = 同一 Track への復帰 → 通知スキップ、または文面を「会話再開」に差し替え

wait_response の pause 自体は idle 判定に必要なので残す。触るのは通知の出し分けのみで済む想定。

補足: 通知経路は `on_track_activated` フックに統一されており (pulse_dispatch.md §5)、`activate` は user_conversation 以外 (autonomous 等) でも同じフックを通る。ガードを入れるなら「同一 Track 復帰の抑止」を activate/フック側の共通ロジックに置くか、Handler 個別に置くかも設計判断。

## 関連リソース

- `docs/intent/persona_cognition/pulse_dispatch.md` §5 (track_activated observer への通知経路統一)
- `saiverse/track_manager.py` `activate` (displaced_track_ids のロジックあり — 復帰判定の材料になりうる)
- `saiverse/track_handlers/user_conversation_handler.py` `on_track_activated` / `_inject_track_context` / `on_user_utterance`
- 関連 issue: `docs/issues/user_utterance_forced_response_on_running_conflict.md` (同じ `on_user_utterance` 経路の別論点)
