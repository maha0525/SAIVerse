# Issue: ｽﾀｯｸﾁｬﾝ発話中断機構

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-17
**関連**: `expansion_data/saiverse-stackchan-addon/`、`expansion_data/saiverse-voice-tts/`、`docs/intent/stackchan_vessel.md`、`docs/issues/stackchan_mcp_upstream_pr_strategy.md`

## 背景

ｽﾀｯｸﾁｬﾝが長文を発話している最中、 ユーザーが「もういい」「止めて」と思っても中断する手段がない。 ペルソナの自律発話で文脈が変わってしまった時や、 単純に騒がしい場面で物理ボタン代わりに止めたい場面で困る。

現状、 発話経路は以下のとおり中断点がない:

1. **voice-tts addon** (`expansion_data/saiverse-voice-tts/tools/speak/`): TTS 合成 + audio stream 配信 + (Vessel 経由なら) PCM を gateway に POST。 中断 API 不在 (`cancel` / `abort` / `interrupt` で grep してもヒットなし、 唯一あるのは `audio_stream.py:255` の docstring 内の "mid-response aborts" 言及のみ)。
2. **stackchan-mcp gateway**: `POST /pcm` で受け取った PCM を WS 経由で device に送り続ける。 中断 API なし。
3. **ｽﾀｯｸﾁｬﾝ firmware**: device 上で受信した PCM を audio buffer に積んで再生。 buffer flush の MCP tool なし。

つまり「すでに合成済みの音声」「合成中の音声」「device の audio buffer に積まれた音声」 の 3 段階すべてを止める仕組みがいる。

## スコープ

addon 管理 UI (= `expansion_data/saiverse-stackchan-addon/ui/Panel.tsx` のデバイス操作セクション) に「発話を止める」 ボタンを 1 つ追加することがゴール。 1 クリックで上記 3 段階すべて停止する。

## 実装範囲

### 1. voice-tts addon 側 — playback / synthesis の停止 API

新規 endpoint:

- `POST /api/addon/saiverse-voice-tts/playback/stop`

実装内容:

- `playback_worker.py` の TTS ジョブキューを drain + 現在合成中のジョブを cancel
- 配信中の audio_stream を `None` 送信で終端 (= browser 側 `<audio>` を自然終了させる)
- 並行して進行中の `POST /pcm` (gateway 宛て) があれば aiohttp request を cancel

### 2. stackchan-mcp gateway 側 — 新規 MCP tool `stop_audio`

新規 tool 追加 (= 手元 fork `maha0525/stackchan-mcp` に commit、 upstream PR 候補):

- `stop_audio`: WS で device に「audio buffer flush + 現在再生中の音声停止」 コマンドを送る
- gateway 内部の PCM relay (= `capture_server.py` の `POST /pcm` ハンドラ) も中断シグナルを送ったタイミングで break させる

ESP32 firmware 側 (= `temp/stackchan-mcp-firmware/`) にも対応 MCP method `self.audio_speaker.stop` 等を実装する必要がある (= audio task の DMA buffer を flush)。 firmware 側変更は OTA で配布。

### 3. addon api_routes.py — 統合エンドポイント

新規 endpoint:

- `POST /api/addon/saiverse-stackchan-addon/device/speech/stop`

実装内容:

- 内部で (1) voice-tts の `playback/stop` を叩く + (2) `stackchan-mcp` の `stop_audio` MCP tool を叩く、 を並列実行
- 片方失敗してももう片方は続行 (= 中断は best effort)

### 4. Panel.tsx — UI

「デバイス操作」 セクションに「発話を止める」 ボタン (赤系強調) を追加。 押下で `POST /device/speech/stop` を叩く。

### 5. firmware 側 — タッチ長押しによる物理停止 trigger

addon 管理 UI のボタンは「PC を開いてる時」 にしか押せない。 ｽﾀｯｸﾁｬﾝ単体で動かしている時 (= 食卓に置いて会話、 等) でも止められるよう、 **firmware の touch driver で長押し検出時に発話停止 path を発火**:

- `StackChanBoard` の touch event 判定ロジックに「~1.5 秒以上の hold」 を `LONG_PRESS` event として追加
- 検出時に device 側で audio buffer を flush + (= 上記 (2) の `self.audio_speaker.stop` と同等) MCP 経由で `speech_interrupt_requested` notification を gateway に通知
- gateway は notification を受けて voice-tts の停止 API も叩く (= 合成中のジョブも止める)

これで PC が遠い / 触りたくない場面でも、 ｽﾀｯｸﾁｬﾝの頭を 1.5 秒触れば止まる。 上記 (1)〜(4) の経路と同じエンドポイントを叩く形で実装すれば冪等性も保たれる。

**留意点**: 現在頻発している false STROKE event (= [`stackchan_touch_false_stroke_events.md`](stackchan_touch_false_stroke_events.md)) が解消されてからでないと、 勝手に発話停止が走るリスクあり。 false stroke 対応の後に着手する順序が望ましい。

## upstream PR 影響

`stop_audio` MCP tool + firmware 側 `self.audio_speaker.stop` の追加は upstream PR 候補として `docs/issues/stackchan_mcp_upstream_pr_strategy.md` に Series F として追補する必要あり。 着手時点で同 doc を更新する。

## 不変条件

- 中断ボタンは複数回連打されても害がないこと (= idempotent、 すでに無音なら no-op)
- 中断中に新規 TTS ジョブが enqueue されたら正常に再生されること (= 中断フラグが残り続けて永久無音にならない)
- gateway / device 接続なしでも voice-tts 側だけは停止できること (= 503 を返さず部分成功)

## 関連メモリ

- `feedback_user_experience_first.md` — 「ユーザーが詰まらない」 視点で、 現状の「物理電源切るしかない」 状態は明らかに改善対象
- `project_voice_tts_remote_layout.md` — voice-tts は Nature109 fork が正規、 改修は origin push で OK
