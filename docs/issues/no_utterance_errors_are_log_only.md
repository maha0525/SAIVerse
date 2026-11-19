# エラーで発言が生まれなかった回が、ログにしか残らない

**ステータス**: 設計中 — 洗い出しは完了した (2026-11-19)。設計は [no_utterance_error_notice.md](../intent/no_utterance_error_notice.md) で進めている。
**出自**: 2026-11-19 の朝、エリス (`eris_city_a`) の 9:00 のスケジュールが発火したが、何も喋らなかった。8:17 に画面のモデル選択で全ペルソナが一時的に `gpt-4o-2024-11-20` に切り替わっていて、9:00 にエリスの文脈 159,608 トークンが gpt-4o の上限 128,000 トークンを超え、OpenAI が 400 (`context_length_exceeded`) を返した。空の下書き行は取り下げられ、ScheduleManager は `outcome=error` をログに書いて翌日の分を登録した。部屋の記録にもエリスの記憶にも何も残らず、起きたことを知っていたのは `backend.log` だけだった。

## まはーが決めた方向 (2026-11-19)

- 「エラーの結果、発言が生まれなかった」ことを、その部屋の記録にシステム発言として残す。ユーザーはチャット画面で見られ、ペルソナは次に喋るときに読める。
- 対象は今回のエラーだけでなく、結果として発言が生まれなかったエラーのすべて。
- ペルソナには「エラーで発言に失敗した」ことだけを見せ、詳細 (原因・数値) はユーザーにだけ見せる。理由: [reply_stop_exit.md](../intent/reply_stop_exit.md) の「通告の内容」の節が決めているとおり、エラーの中身 (特に安全性フィルターの理由) をペルソナの記憶に書くと、誤った自己像を植え付ける。
  - 実現の当て (メティス案、未設計): 部屋の記録の一行の本文 (`role: "host"`、取り込みでペルソナの記憶へ届く) には理由を含まない一文を書き、詳細は同じ行の `metadata` に置く。`metadata` はペルソナのコンテキストに載らず、画面だけが読める (`_interrupted` の印と同じ性質)。

## 洗い出しの結果

以下は `f42de3b8` (develop) のコードを読んだ結果で、実機では動かしていない。【推測】と書いたもの以外はコードで確かめた。

### 発話を起こす経路

いま生きているのは次の 6 つ。

1. ユーザーのチャット — `/api/chat/send` → `manager/runtime.py` の `handle_user_input_stream` → `backend_worker`。アドオンの音声入力 (stackchan `audio_input_relay.py:287`、godot `voice_input.py:100`、skyrim `bridge.py:1238`) も同じ関数を呼ぶ。
2. やり直しと続き — `/api/chat/retry`、`/api/chat/continue` → `_stream_persona_pulse` (`manager/runtime.py:1041-1185`)。
3. Discord ゲートウェイの非ストリーム経路 (`manager/gateway.py:67` → `manager/runtime.py:602-694`)。`discord_gateway/saiverse_adapter.py:111` は `gateway_handle_human_message(message)` と 1 引数で呼ぶが、定義 (`manager/gateway.py:60-62`) は 2 引数なので、【推測】この経路はいま動いていない。
4. スケジュール — `saiverse/schedule_manager.py` `_execute_schedule` → `saiverse/pulse_dispatcher.py` `dispatch_schedule_fire` → `PulseController.submit` → `run_meta_user`。`event_callback` は None なので、画面への一時的な知らせは原理的に出ない。
5. 外部イベントの現象 `inject_persona_event` (on_event 判断を含む) — `builtin_data/phenomena/inject_persona_event.py` → `saiverse/autonomy_wiring.py` `handle_external_event` → `dispatch_phenomenon_event`。
6. 台帳の回収経路が拾い直す on_event の応対 — `saiverse/autonomy_wiring.py` `_dispatch_recovered_response_with_retry`。

発話しないと確かめたもの: 判断点の Playbook (judge ノードが `speak: False`)、時間割のコマ (`run_work_session`、`speak=False`)、watchdog、AutonomyManager の tick、召喚。`run_sea_auto` と `submit_auto` は呼び出し元が無い。

### いま何が見えるか

- **スケジュールと外部イベントの経路は、ほぼ全部の失敗がログだけになる。** モデルが無い、関所 (`BeatGateClosedError`) が閉じている、窓の床が用意できない、Playbook が見つからない、Playbook 内の例外 (LLMError の全種、今回の文脈長超過を含む)、再試行しても空の応答、実行時にペルソナが居ない、終了処理中の受付拒否。
- **外部イベントの失敗は、ログ上で成功と記録される。** `inject_persona_event` は失敗時に文字列 `"error: pulse submission failed"` を返すが、`phenomena/manager.py:184` は戻り値を見ずに `completed successfully` を INFO で出す。
- **ユーザーのチャットでは、ブラウザが付いている間だけ一時的な知らせ (NDJSON のイベント) が出る。** 保存されないので再読込で消え、ペルソナは失敗を知らない。ブラウザが付いていない回 (アドオンの音声入力、画面を閉じた後) はログだけになる。
- **部屋に複数のペルソナがいて、1 人目が LLMError で失敗すると、2 人目以降は走らない。** `manager/runtime.py:858-935` で、LLMError を捕まえる try が for ループの外側にあるため。1 人目のエラーの知らせで「何か届いた」扱いになり、2 人目以降については画面にもログにも何も出ない。
- **ペルソナが忙しくて待機列に入った実行は、失敗しても誰も見ていない。** 待機列から繰り上がった実行は別のデーモンスレッドで `_execute_unlocked` を呼び (`sea/pulse_controller.py:769-772`)、LLMError はそこで再送出される (`:649-652`) ので、スレッドの未処理例外になる。リポジトリに `excepthook` の設定は無いので、【推測】トレースバックは stderr に出るだけで `backend.log` に入らない。スケジュールの台帳には、受付の時点で `accepted` として前進済みになる。
- **再試行しても応答が空だった回は、意図した沈黙と区別できない。** ストリーム経路は例外を出さずに締めの Beat の取り下げを通って返る (`sea/runtime_llm.py:6972-6978`、`:7288-7329`)。

### エラーではない (記録しない) もの

- 判断が note_only / ignore などを選んだ回。経路の名前 (`judged:<reaction>`) で区別できる。
- 文字が出る前にユーザーが止めた回。2026-08-26 の裁定 (`sea/pulse_controller.py:623-635` のコメント) どおり記録しない。ユーザーは自分で止めたことを知っている。
- 新しいユーザー発言が古い要求を置き換えた回。次の Pulse でペルソナは両方の発言を読む。

### 先例

- 途中まで喋ってから止まった回は、`sea/reply_stop_exit.py` の `settle_reply_stop` が、部屋の記録に `role: "host"` の中断の通告を置いている。書き込みは `sea/runtime_llm.py` の `_record_interruption_notice` (`add_to_building_only`)。取り込み (`builtin_data/tools/get_building_messages.py:341-379`) がこの行を role=system のシステム通知 (`SYSTEM_NOTICE_KEY: "building_event"`) にしてペルソナの記憶へ配り、画面は host 行をシステム通知として描く。
- ただし `settle_reply_stop` は、この返事の中で何かが保存された回だけを扱う (`sea/reply_stop_exit.py:272-276`)。一言も生まれなかった回には何もしない。
- 2026-08-26 に「一言も出ないうちに止められた回」の記録を撤去した理由は、「返事が生まれなかった回には昔から何も書いておらず、そこだけ機構の声を足すと不揃いになる」だった。今回の方向は、エラーの回についてこの前提を変える。

### 確認を一箇所に置くなら

一番多くを覆うのは `PulseController._execute_unlocked` (`sea/pulse_controller.py:589-675`)。ユーザー・スケジュール・外部イベント・回収の応対・待機列から繰り上がった実行・中断からの再開が全部ここを通り、`run_meta_user` 全体を囲んでいる。判断点は別の口 (`_submit_meta_lane`) を通るので自然に外れる。「この実行の中で発言が保存されたか」は、既存の `last_saved_utterance(persona_id).saved_at` (`sea/runtime_emitters.py`) と実行開始の時刻を比べれば判定できる。

ここで覆えないもの:

- ここに届く前に終わる出口 — 終了処理中の受付拒否 (`sea/pulse_controller.py:420-427` ほか)、待機列のあふれ (`:543-549`)、終了処理での列の破棄、ユーザー経路の前段 (`saiverse/user_conversation.py:702-728`)、ループの打ち切りで走らなかった 2 人目以降。
- スケジュールと外部イベントの前段 (ペルソナが居ない・現在地が無い回)。こちらは書き込む先の部屋がそもそも決まらない。
- 例外を出さずに発言なしで終わった回 (空の応答、Playbook が見つからない、実行時にペルソナが居ない)。今のままでは意図した沈黙と区別できない。

## 確かめていないこと

- Discord ゲートウェイの全体 (引数の数が合っていないことしか見ていない)。
- 待機列のスレッドの未処理例外が `backend.log` に入るかどうか。
- `playbook_not_found` のイベント (キーが `error_code` ではなく `code`、`content` が無い) を画面がどう描くか。
- やり直しの門番 `_has_assistant_reply_after` (`database/building_messages.py`) が、host の行を「返事があった」と数えないか。
- 終了処理で取り消されたスケジュールが `accepted` として前進して失われること (分類のコードからの推論)。
- スペルだけの返事や、心内文だけで正規化すると空になる返事の扱い (意図した沈黙との境目)。
- 作業セッションの中から `tell` などのスペルで建物へ書く場合。
- 非ストリームの経路で `EmptyResponseError` が起きたときの扱い。
