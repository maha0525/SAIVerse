# Issue: Stack-chan (ESP32-S3) が稼働中に自発的に再起動 / 電源 OFF する

**ステータス**: 🔍 未調査 (事象記録のみ、 本格切り分けはこれから)
**優先度**: medium-high (= 物理身体が突然死ぬと Vessel 機能が成立しない。 ただし再現条件が未特定で再現待ち)
**作成日**: 2026-06-29
**関連**:
- デバイス: Stack-chan (ESP32-S3、 MAC `44:1b:f6:df:58:e0`)
- ファーム: fork `maha0525/stackchan-mcp` @ `integrate/all-fixes-2026-06-24` を本セッションで build & flash
- gateway: `saiverse-stackchan-addon__stackchan` (stackchan-mcp gateway, uvx subprocess)
- ログ: `~/.saiverse/user_data/logs/20260629_222519/backend.log` (本体側), 同ディレクトリの `mcp_subprocess_saiverse-stackchan-addon__stackchan.log` (gateway 側 WS 接続イベント)
- 電源系の前提: AXP2101 PMIC 給電 (memory `project_stackchan_power_topology`)

## 観測

### 今回の事象 (2026-06-29 の超音波測距ユニット対応セッション中)

超音波測距ユニット (RCWL-9620) の動作確認中、 Stack-chan が稼働中に**突然電源 OFF** した。 ユーザーは物理的に触れていない (= 自発的な電源断)。

タイムライン (`backend.log`, session 20260629_222519):
- `22:25` SAIVerse 再起動 (sonic.py 100kHz 反映のため)
- `22:26:15` stackchan MCP subprocess 起動 (WS_PORT 18765)
- `22:28:11` device ready (session=`bc837452-...`)、 顔リセット等正常動作
- `22:28〜22:30` 超音波測距が正常動作 (`get_sonic_distance` が実値を返す。 read@100kHz で安定取得できていた)
- `22:30:28` `_fetch_device_session_id: result is empty/None` (接続が怪しくなり始める)
- `22:30:58` 以降ずっと `No ESP32 device connected. Please check the device.`
- その後ユーザーが確認 → **Stack-chan 本体の電源が落ちていた** (再起動して復帰したのではなく OFF のまま)

### 既往 (今回が初めてではない)

ユーザー報告: **この超音波対応とは無関係に、 他の場面でも「急に再起動する現象」が以前から起きていた**。 つまり今回の電源 OFF は単発ではなく、 Stack-chan の自発再起動 / 電源断という再発性の問題の一例と見られる。

### 追加事象 (2026-06-30、 LED/カメラ修正の実機確認セッション中)

LED スペル欠落とカメラ画像未達の SAIVerse 側バグ修正を実機確認していた最中、 同じ自発電源断が再発。 ユーザーが現認して「勝手に電源落ちてた」 と確認 (= 今回も **OFF のまま**、 手動再起動で復帰)。

タイムライン (`~/.saiverse/user_data/logs/20260630_004606/`, gateway `mcp_subprocess_saiverse-stackchan-addon__stackchan.log`):
- `00:47:13` device ready (再起動後に正常接続、 35 tools)
- `00:47〜00:51:40` 約 4.5 分間正常動作 (LED 点灯 `set_all_leds → ok`、 自動 see のカメラ画像取得も成功)
- `00:51:31` gateway 側 `avatar_loader: MCP call failed` (heartbeat が落ち始める)
- `00:51:40` `ESP32 disconnected` → 以降 `No ESP32 device connected`、 4 分以上自動再接続せず (= OFF のまま)
- `00:57:31` ユーザーの手動再起動で device ready 復帰
- その後も `01:17:34` に **接続 4 秒後の即切断** など不安定な切断/再接続を反復

**重要な切り分け材料**: 今回の `00:51:40` 切断の直前は、 超音波 ping もサーボ駆動も無い **通常の会話ターン処理中** (軽負荷) だった。 前回 (2026-06-29) は超音波測距中だったが、 今回は重い電流負荷が無い状況で落ちている。 → 「超音波/サーボの瞬間電流スパイク」 単独説 (仮説 1) では今回の事象を説明できない。 電源マージン全般 / ファーム / PMIC 側など、 負荷非依存の原因を疑う必要がある。

## 切り分けの前提 (重要)

- **本セッションの I2C / 超音波対応 (firmware の `scl_speed_hz` 追加 + sonic.py) は本件と無関係**。 あれは I2C 通信レイヤの修正で完成済み。 本件は「デバイス本体が落ちる」 物理 / ファーム / 電源レイヤの別問題。
- 「再起動」 と「電源 OFF」 は区別が必要。 ESP32 のブラウンアウト検出やパニックは通常 **reset (再起動)** を起こす。 今回ユーザーが見たのは **OFF のまま** (自動復帰せず) で、 これは PMIC のラッチ OFF / 過電流・過熱保護 / バッテリ枯渇に近い挙動。 既往の「再起動」 と今回の「OFF」 が同一原因かは未確定。

## 仮説 (いずれも未検証。 断定しないこと)

1. **電源 / ブラウンアウト系**: 超音波 ping (RCWL-9620 は送信時に瞬間的な電流スパイク)、 サーボ (8Servos)、 本体処理の重なりで瞬間的に電力が不足し、 AXP2101 が保護動作した。 ただし既往の再起動は超音波無しでも起きているはずなので、 超音波単独要因では説明がつかない (電源マージン全般の問題の可能性)。 **2026-06-30 の事象は超音波/サーボ非稼働の軽負荷時に発生しており、 瞬間電流スパイク説とは整合しない** → 負荷依存トリガーは主因ではない可能性が高い。
2. **ファーム由来のクラッシュ**: `integrate/all-fixes-2026-06-24` は統合ブランチで、 稀な不安定があり得る。
3. **WiFi / ネットワーク**: WS 切断は起こすが、 デバイス本体の電源 OFF までは通常起こさない → 本件 (OFF のまま) の主因としては弱い。

## 診断プラン (デバイス復帰後に実施)

このファームブランチは **coredump-to-flash 有効** (commit `610249e` "feat(firmware/esp32s3): enable coredump-to-flash for panic backtrace retention") + active keepalive (#239)。 これを活かす:

1. **reset 理由の取得**: USB (COM3) 接続で電源投入し、 起動シリアルログを取る。 `esp_reset_reason()` / ブラウンアウト検出メッセージ (`Brownout detector was triggered`) / panic backtrace のいずれが出るかで brownout vs panic vs clean boot を判別。
   - ESP32-S3 USB CDC は reset 直後の boot ログを取り逃しやすい (memory `project_esp32s3_usb_cdc_reset_capture`)。 pyserial 常時オープン or 物理 UART で確実に捕捉する。
2. **coredump 吸い出し**: パニックなら flash に coredump が残る。 `idf.py -C temp/stackchan-mcp/firmware coredump-info` / `coredump-dbg` (USB 経由) で backtrace を取得。
3. **電源系の確認** (ユーザー領域の物理事実):
   - バッテリ駆動か USB/AC 給電か (バッテリなら枯渇 / 電圧降下を疑う)
   - 落ちた直後の本体温度 (過熱保護の可能性)
   - 給電を強化 (安定化 5V / 別 USB) して再発するか
4. **再発の捕捉**: 再発性があるので、 gateway 側 WS 切断タイムスタンプ (`mcp_subprocess_*.log`) と device boot ログを継続記録し、 次回発生時の前後文脈 (直前の操作 = サーボ / 超音波 / カメラ等の電流負荷) を残す。

## 未確定事項 (調査開始時に埋める)

- [ ] 既往の「再起動」 と今回の「OFF」 は同一原因か
- [ ] reset 理由 (brownout / panic / WDT / power-loss)
- [ ] 給電方式 (battery / USB) と再発の相関
- [ ] 特定の操作 (サーボ駆動・超音波 ping・カメラ撮影) との時間相関
