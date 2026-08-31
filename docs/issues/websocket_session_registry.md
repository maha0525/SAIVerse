# Issue: 物理 Vessel SDK 共通基盤化 (WS セッション管理 + ストリーミング音声)

**ステータス**: 🔲 未着手
**優先度**: low (2 例目が出るまで保留)
**作成日**: 2026-05-12 (2026-05-12 更新: ストリーミング音声 / SDK 化に拡張)
**関連**:
- `expansion_data/saiverse-stackchan-addon/vessel_manager.py` (現状の実装)
- `expansion_data/saiverse-stackchan-addon/api_routes.py:273-387` (vessel_endpoint)
- `expansion_data/saiverse-stackchan-addon/audio_stream_bridge.py` (TTS chunk 転送)
- `expansion_data/saiverse-stackchan-addon/firmware/src/main.cpp` (libhelix + playRaw)
- `feedback_generic_foundation_first.md` (汎用基盤先行原則)

## 背景

Stack-chan addon の Phase 2 統合中に、物理 vessel (= ペルソナの身体になる外部デバイス) を SAIVerse に繋ぐ際の **汎用的な罠** が複数見つかった。Stack-chan 固有ではなく、別フォームファクタの vessel / 物理連携アドオン / Discord Gateway 等で再発する性質のもの。

### 罠 1: WebSocket セッション管理

- **TCP half-open**: クライアント (ESP32) は接続継続と認識、サーバ (Starlette) は close 判定。Wi-Fi / NAT 経路で TCP RST が到達しないと発生。`enableHeartbeat` を入れても library 実装差で穴が残る。
- **Session 上書き identity 喪失**: 同じ ID で reconnect が来たとき、新 session が dict を上書き → 古い task の `finally: unregister(id)` が **新 session を誤削除**。結果「session 登録なし + WS は alive」のゾンビ状態 → 発話 skip。

### 罠 2: ストリーミング音声 (PCM データ寿命)

ESP32 側で MP3 デコーダ (libhelix 等) の callback で受け取る PCM は **デコーダ内部の固定 buffer 参照** で、次の frame で上書きされる。一方 M5Unified の `Speaker.playRaw` は **data を内部コピーせず ポインタだけ保存** する (Speaker_Class.cpp:1029、hpp の `@attention` も明示)。**ユーザーが両者の寿命管理をしないと再生中に PCM が壊れる** (= 「喉が枯れた」ガビガビ音)。

これは libhelix + M5Unified に限らず、ストリーミングデコーダ + DMA 出力の組み合わせ全般で発生する。Stack-chan 以外の vessel (例: ESP32 + 別 codec、Raspberry Pi + ALSA、別マイコン + I2S DAC) でも対応必要。

### 罠 3: ライブラリ仕様の誤読

- `playRaw(data, len, rate, stereo, repeat, channel, stop)` の **第5引数 `repeat` を音量と誤読** (255 = 255 回繰り返し)
- libhelix の callback `len` を **byte 数と誤読** (実は sample 数 = `info.outputSamps`)

これは「物理 vessel SDK のサンプル実装 / テンプレート」があれば防げる。

### 罠 4: シリアルログのキャプチャ漏れ

ESP32 側の `Serial.println("[ws] disconnected")` 等の重要な状態遷移が **手動で `pio device monitor` してる時にしか取れない**。一般ユーザーがトラブルシューティングする際にも問題になるので、SAIVerse の logs dir に統合される仕組みが欲しい → 別 issue `stackchan_serial_log_integration.md` に切り出し。

### 再発見込みの経路

- Discord Gateway (`SAIVERSE_GATEWAY_WS_URL`)
- Phase 4 構想の恒常入力経路 (カメラ / X 等の物理デバイス接続)
- 将来の物理連携アドオン (Stack-chan 以外の vessel 系: より高機能なロボット、専用ハードウェア等)

「3 例目を待たず、2 例目が出たら抽象化」の方針。

## 解決案候補

サーバ側 (Python) とファーム側 (C++) で別建て。

### サーバ側: WS セッション管理 + 音声ストリーム送信

#### A. `WebSocketSessionRegistry[KeyT, SessionT]` 汎用クラスを `saiverse/` 配下に作る

- `register(session) -> Optional[SessionT]`: 既存 session があれば return (呼び出し側で古い WS を close)
- `unregister(key, session=None)`: identity check 付きで安全に削除
- `get(key)`, `list_sessions()`
- 内部に `last_seen_at` テーブルを持ち、`prune_stale(timeout_s)` で TCP half-open 検知

#### B. ベースクラス `BaseWebSocketEndpoint` を提供

- `vessel_endpoint` 的な hello → 認証 → メッセージループの定型を base class に
- 子クラスは `verify_handshake` / `on_message` / `serialize_event` だけ書く
- session registry は base class 内蔵

#### C. `AudioStreamForwarder` ヘルパー

- `audio_stream_bridge.py` を一般化して `voice-tts の audio_stream → 任意の vessel WS` のブリッジ部分を共通化
- subscribe / threadsafe send / abort on disconnect の定型をライブラリ化
- vessel 側はバイナリ受信ハンドラだけ書けば済む

### ファーム側: 物理 Vessel SDK (C++/Arduino)

将来複数の vessel ファーム実装 (Stack-chan v1 / v2 / 別フォームファクタ) が出る場合、共通の Arduino library を作って `lib_deps = saiverse-vessel-sdk` で取れる形にする。

#### D. `SaiverseVessel` クラス (Arduino library)

- `begin(serverUrl, vesselId, token)`: WS 接続 + hello + welcome 待ち
- `loop()`: WS の loop + heartbeat
- `onMessage(callback)`: TEXT メッセージ受信 callback
- `playAudioStream(callback)`: バイナリ MP3 chunks 受信 → 内部 ringbuf → 別 core でデコード + I2S 再生
  - **PCM rotation buffer 内蔵** (libhelix の固定 buffer 上書き問題を SDK 側で吸収)
  - `playRaw` の `len` (sample 数) / `repeat` の誤用も SDK の API でガード
- `setIdentity(building_id)`: 画面表示や状態管理

これがあれば、新しい vessel ハードウェアを作る人は `SaiverseVessel vessel(...); vessel.begin(); vessel.playAudioStream();` だけで物理身体化できる。

#### E. リファレンス実装 + サンプルファーム

`expansion_data/saiverse-stackchan-addon/firmware/` を **参考実装** として位置づけ、他の vessel addon は port-and-adapt する。SDK 化が時期尚早なら、この firmware ディレクトリを **テンプレート** として git clone できる構造に整える。

## 設計上の論点

- **key の型**: `str` で十分か、Generic で `KeyT` まで持っていくか (現状 vessel_id は str)
- **last_seen 更新の責務**: registry が自動更新 (`get` 呼び出しごと) するか、呼び出し側が明示するか
- **stale 検知の trigger**: 別タスクで定期 prune するか、register 時に既存 check するか
- **server_hooks との連携**: audio_stream_bridge のように「session に対して push する」経路を直接サポートするか

## 関連リソース

- 現状の vessel_manager.py 修正 (2026-05-12): identity-aware unregister + register が old session を return
- TCP half-open 検知改善: WebSocketsClient (ESP32 側) の `enableHeartbeat` だけでは不十分だった事例

## ログ

- 2026-05-12: issue 起案。stackchan_addon Phase 2 統合テスト中に TCP half-open + session 上書き bug を踏み、修正後に「これは汎用化案件」と判断。2 例目 (Discord gateway / 別 vessel 等) が出たら本格着手。
- 2026-05-12 (更新): Phase 2 統合中にさらに「PCM data 寿命管理」「ライブラリ仕様誤読 (sample 数 / repeat 引数)」「シリアルログキャプチャ漏れ」が判明。スコープを「**物理 Vessel SDK 共通基盤化**」に拡張。ファーム側 SDK (Arduino library 化 or テンプレート構造) も解決案候補に追加。シリアルログ統合は別 issue (`stackchan_serial_log_integration.md`) に切り出し。
