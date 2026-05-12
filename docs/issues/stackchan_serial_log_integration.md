# Issue: Stack-chan シリアルログを SAIVerse logs/ に統合

**ステータス**: 🔲 未着手
**優先度**: medium (Phase 2 のトラブルシューティング体験に直結)
**作成日**: 2026-05-12
**関連**:
- `expansion_data/saiverse-stackchan-addon/firmware/src/main.cpp` (`Serial.println` / `Serial.printf`)
- `~/.saiverse/user_data/logs/<session>/backend.log` (現状のセッションログ dir)
- 親 issue: `websocket_session_registry.md` (物理 Vessel SDK 化)

## 背景

Stack-chan ファームの `Serial.println("[ws] disconnected")` / `Serial.printf("[debug] uptime=...")` 等の状態遷移ログは、現状 **手動で `pio device monitor` してる時にしかキャプチャされない**。

これが Phase 2 統合のデバッグで深刻な問題だった:

- サーバ側 `backend.log` には「20:40:24 device disconnected」とだけ書かれる
- 切断の **原因が device 側で何だったか** (WStype_ERROR? heartbeat 失敗? heap 不足?) は serial 出力にしかない
- 私 (エア) が `python -c "...serial..."` を実行した 25-30 秒の窓内に切断が起きないと、ログを取り逃す
- まはーが「声出ない」と言った時には既に disconnect 完了、シリアル巻き戻し不可

一般ユーザー視点でも同じ問題:

- アドオン入れたら音が出ない → サポート求めようにも serial log が手元にない
- backend.log だけで原因特定できないケースが多発する見込み (Wi-Fi / 電源 / Wi-Fi 干渉 / heap 不足 等)

## 期待挙動

SAIVerse のセッションログ dir (`~/.saiverse/user_data/logs/<YYYYMMDD_HHMMSS>/`) に **`stackchan_serial.log`** が自動的に書き出される。`backend.log` / `llm_io.log` / `sea_trace.log` の隣に並ぶ形。

これで:
- `backend.log` の 20:40:24 disconnect ログを見たら、`stackchan_serial.log` の同時刻を見れば device 側の理由が分かる
- 一般ユーザーもまるごと zip して送れば再現に必要な情報が揃う

## 解決案候補

### A. アドオン内で serial monitor を起動 (推奨)

- アドオンの起動時 hook (もしくは vessel WS 接続時) に背景 thread で serial port を開く
- 出力先は `saiverse.logging_config` から セッション log dir を取得して `stackchan_serial.log` に追記
- COM port は addon 設定で指定 (Windows: `COM3`, Linux: `/dev/ttyUSB0`, macOS: `/dev/cu.usbserial-*`)
- デフォルトは `null` (= disable)、設定したら有効化

#### 注意点

- **書き込み (`pio run -t upload`) 中は serial port が占有される** → 監視タスクは書き込み前に `close()` する必要がある
- `/api/addon/.../monitor/stop` 的なエンドポイントを用意するか、書き込み開始時に自動 stop
- COM port が無効になっていてもアドオン全体が落ちないようにエラーハンドリング

### B. ESP32 → Wi-Fi 経由のリモート log

- ESP32 側で UDP / TCP で log を SAIVerse に送る
- 利点: USB ケーブル不要、起動後すぐにリモート log 開始
- 欠点: Wi-Fi 接続前のブート ログ (一番欲しい部分) が取れない
- → A の補完として将来検討。第一段階は USB シリアル経由が現実的

### C. SAIVerse 本体の logging_config に統合

- `saiverse/logging_config.py` に「外部デバイスからの log を受け取って同 dir に書く」汎用機構を作る
- アドオン側は path だけ受け取って好きに書く
- 将来 別 vessel addon でも同じ仕組みを使える (= 親 issue の物理 Vessel SDK の一部)

## 当面の凌ぎ

実装まで `temp/stackchan_serial_capture.py` 的な背景常駐スクリプトで `~/.saiverse/user_data/logs/<最新>/stackchan_serial.log` に出力する。手動運用。

## ログ

- 2026-05-12: issue 起案。Phase 2 統合デバッグでシリアルログ取り逃しが致命的に効いたので可視化。親 issue `websocket_session_registry.md` から切り出し。
