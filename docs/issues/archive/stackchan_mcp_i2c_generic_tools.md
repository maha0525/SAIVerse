# Issue: stackchan-mcp に I2C 汎用 tool を追加 (拡張モジュール対応の第一弾)

**ステータス**: 🟢 実装完了 / 実機動作確認済 (upstream PR は別途)
**優先度**: medium
**作成日**: 2026-05-19
**最終更新**: 2026-05-19
**関連**:
- `docs/intent/stackchan_extension_modules.md` (拡張モジュール対応の設計指針、 本 issue はその第一弾)
- `docs/issues/stackchan_mcp_upstream_pr_strategy.md` (本家 PR 戦略、 本 issue の PR ①/② を追加投入)
- fork: `https://github.com/maha0525/stackchan-mcp`
  - `feature/mcp-property-array-type` (PR ① 用、 upstream/main 派生)
  - `feature/mcp-i2c-generic-tools` (PR ② 用、 PR ① の上に積み)
  - `dev/integration` (実機テスト用統合 branch)

## 背景

`docs/intent/stackchan_extension_modules.md` で 「ｽﾀｯｸﾁｬﾝに拡張モジュール (ENV III 等の I2C Unit) をユーザーがファーム書き換えなしで追加できる体験」 のために C 案 (汎用口 + 人気 Unit プリセット + SAIVerse addon ドライバ) を採用することが確定した。

本 issue ではこの第一弾として、 stackchan-mcp 本家への汎用 I2C tool 追加 PR の設計・実装・検証を扱う。

まはーが手元に以下の I2C Unit を所持しており、 ENV III を最初の動作確認対象とした:

- ENV III (温湿度・気圧、 SHT30 0x44 + QMP6988 0x70)
- 超音波測距 (RCWL-9620)
- 水分測定センサ付き給水ポンプ
- TVOC/eCO2 ガスセンサ (SGP30)
- Port A I2C 拡張ハブ v2.1

## 重大な発見 (2026-05-19): 本家には Port A bus init 自体が無かった

PR ② の初期実装 (旧 commit `024234b` / `57f0533`) では既存 `i2c_bus_` (= `AUDIO_CODEC_I2C_*`、 GPIO 12/11) を使って I2C tool を実装した。 これは「audio codec 用 bus」 と認識していたためで、 「外部 device 用」 として位置付けていた。

しかし実機テスト時に i2c.scan で内部 IC (0x21/0x23/0x34/0x38/0x40/0x41/0x50/0x51/0x58/0x68/0x69/0x6F) しか見えず、 Port A に接続した ENV III (0x44/0x70) が一切応答しない問題が発覚。 ESPHome の M5Stack CoreS3 ページで裏取りした結果:

- `AUDIO_CODEC_I2C_*` (GPIO 12/11) は **internal device 共通 bus** で、 PMIC (AXP2101) / IO ext (AW9523) / touch (FT6336) / IMU (BMI270) / audio codec (AW88298) / 他 が共有
- **Grove Port A I2C は GPIO 2 (SDA) / GPIO 1 (SCL)** で、 internal とは独立した別 controller

xiaozhi-esp32 系の全 board (stackchan / m5stack-core-s3 / atom-* / 等) のコードを grep した結果、 **Port A I2C bus init を実装している board は存在しなかった**。 つまり「Port A bus 経由で外部 Unit を叩く」 という機能自体が本家には無い。

→ PR ② のスコープを「I2C tool 追加」 から「**Port A I2C bus init を追加 + その上で汎用 I2C tool を expose**」 に拡張して再設計した。

## 設計確定事項 (再設計版)

### スコープ

- **I2C 系のみ** (UART / GPIO は別 PR で後追い投入)
- センサー系を先行する方針 (拡張モジュール対応全体としてセンサー先行、 LLM Module は将来検討)
- **Port A 専用** (= internal bus は触らない)

### バス設計: Port A 専用 master bus

ESP32-S3 は I2C controller を 2 系統持つ (`i2c_new_master_bus()` を別 port で 2 回呼べる)。 stackchan board は既存の internal bus を I2C_NUM_1 で使っているので、 **I2C_NUM_0 を Port A 用に新規 init** する。

`firmware/main/boards/stackchan/config.h`:
```c
// Internal I2C bus pins (AXP2101 / AW9523 / FT6336 / PY32 / Si12T /
// audio codec / IMU を共有)。 self.i2c.* は触らせない。
#define AUDIO_CODEC_I2C_SDA_PIN  GPIO_NUM_12
#define AUDIO_CODEC_I2C_SCL_PIN  GPIO_NUM_11

// External I2C bus pins for Grove Port A (M5Stack HY2.0-4P connector).
// Independent from the internal bus above。
#define PORT_A_I2C_SDA_PIN       GPIO_NUM_2
#define PORT_A_I2C_SCL_PIN       GPIO_NUM_1
```

`StackChanBoard` に `port_a_i2c_bus_` member 追加 + `InitializePortAI2c()` メソッド (controller 0、 GPIO 2/1) で別 bus 生成。 constructor で `InitializeI2c()` (internal) → `InitializePortAI2c()` (external) の順で初期化。

### tool 仕様 (案 Y 派生: Port A 専用 4 関数)

```
i2c_write(addr, bytes)
  例: i2c_write(0x44, [0x2C, 0x06])         # SHT30 高精度測定コマンド送信

i2c_read(addr, n_bytes)
  例: i2c_read(0x44, 6)                     # SHT30 結果 6 バイト読み取り

i2c_write_read(addr, write_bytes, n_bytes)
  例: i2c_write_read(0x70, [0xD1], 1)       # QMP6988 chip ID レジスタ読み取り

i2c_scan()
  例: → { "ok": true, "addresses": [0x44, 0x70] }
```

**設計判断**:
- 案 X (レジスタ中心 5 関数) ではなく案 Y (生バイト中心 4 関数) を採用。「レジスタ」 は I2C プロトコル本質ではなく chip ごとの慣習で、 ラッパー関数は SAIVerse addon 側で Python で書けば十分。
- データ形式は int 配列。 PR ① の `kPropertyTypeArray` でファーム側がネイティブに array 引数を受けられる。

### データ形式: int 配列

バイト列は `[0x2C, 0x06]` のような整数配列で表現する (= PR ① の `kPropertyTypeArray` を活用)。

### 安全性: 物理 bus 分離 (案 C')

旧設計 (案 C) は「内部 IC アドレスを protection list で write block + `allow_protected=true` で override」 だったが、 再設計では:

- Port A 専用 bus に切り替え → **internal IC アドレスは別 controller 上、 物理的に到達不可**
- protection list / `allow_protected` parameter は不要 (= 削除)
- 「不変条件で安全を保証 ＞ runtime check で安全を保証」 を選択

これは「security by construction」 の方が「security by convention」 より強い、 という設計哲学に沿う。 future-safe (= 将来 hub 経由で internal IC と被るアドレスを持つ Unit を繋いでも、 bus が分離している前提なので protection 不要)。

### scan 範囲とタイムアウト

- 範囲: 0x08..0x77 (I2C reserved range 0x00-0x07 / 0x78-0x7F 除外)
- 各 probe timeout: 200 ms (boot 時 `I2cDetect()` と同じ、 PaHub / RCWL-9620 等の slower device もカバー)
- 全 scan で約 24 秒 (= 120 address × 200ms 上限)、 ただし ACK する device は即座に返る

### 戻り値の形式

stackchan-mcp の既存 tool 慣習に揃える:

```json
// i2c_read 成功
{ "ok": true, "bytes": [0x12, 0x34] }
// i2c_read 失敗 (デバイス無し等)
{ "ok": false, "error": "ESP_ERR_TIMEOUT" }

// i2c_write 成功
{ "ok": true }
// i2c_write 失敗
{ "ok": false, "error": "ESP_ERR_TIMEOUT" }

// i2c_write_read 成功
{ "ok": true, "bytes": [0x12, 0x34] }
// i2c_write_read 失敗
{ "ok": false, "error": "ESP_ERR_TIMEOUT" }

// i2c_scan
{ "ok": true, "addresses": [0x44, 0x70] }
```

エラー種別:
- `ESP_ERR_TIMEOUT`: NACK (デバイス無し / 応答なし) / バス占有でタイムアウト
- `ESP_ERR_INVALID_ARG`: アドレスが 7-bit 範囲外、 n_bytes が負数等
- `ESP_FAIL`: 一般的バスエラー

(旧設計の `PROTECTED_ADDRESS` エラーは削除 — 物理 bus 分離で発火経路自体が無くなった)

## 実装結果

### PR ①: `PropertyType` に Array 型追加 (基盤改修)

**branch**: `feature/mcp-property-array-type`
**HEAD**: `ed4e62c feat(firmware/mcp_server): add Array property type for tools with list arguments`

stackchan-mcp ファームの MCP server 側に Array 型サポートを追加。

**変更点** (`firmware/main/mcp_server.h` / `mcp_server.cc`):
- `PropertyType` enum に `kPropertyTypeArray` 追加 + `PropertyElementType` enum 新設 (Integer / String)
- `Property::value_` variant に `std::vector<int>` / `std::vector<std::string>` 追加
- `to_json` で JSON Schema `{"type":"array","items":{...}}` 出力 (per-element min/max)
- `DoToolCall` で JSON array → `std::vector<int> / std::vector<std::string>` パース + element-type 検証

**既存 tool への影響**: なし (純粋に追加機能)

**build 検証**: ESP-IDF v5.5.4 で `xiaozhi.bin` 0x30beb0 bytes、 警告なし。

### PR ②: Port A I2C bus init + 汎用 I2C tool (PR ① の Array 型を活用)

**branch**: `feature/mcp-i2c-generic-tools` (PR ① の上に積み)
**HEAD**:
- `6fc715d feat(gateway/stackchan_mcp): expose generic Port A I2C bus tools`
- `42154df feat(firmware/stackchan): add Port A I2C bus and expose generic I2C tools`

**変更点 (firmware)** (`firmware/main/boards/stackchan/{config.h, stackchan.cc}`):
- `PORT_A_I2C_SDA_PIN` (GPIO 2) / `PORT_A_I2C_SCL_PIN` (GPIO 1) を `config.h` に追加
- `StackChanBoard::port_a_i2c_bus_` member 追加 (internal `i2c_bus_` と並存)
- `InitializePortAI2c()` 新設 (I2C controller 0 で別 bus 生成)
- `RegisterMcpTools()` に `self.i2c.{scan,read,write,write_read}` 4 個追加 (handle は `port_a_i2c_bus_`)
- scan は 0x08..0x77 / probe timeout 200ms
- protection list / `allow_protected` / on-board IC skip は **未実装** (物理 bus 分離で不要)

**変更点 (gateway)** (`gateway/stackchan_mcp/stdio_server.py`):
- Tool 定義 `i2c_scan` / `i2c_read` / `i2c_write` / `i2c_write_read` 4 個追加 (description で「Grove Port A」 を明示)
- dispatch mapping で device 側 `self.i2c.*` tool に relay

**build 検証**: ESP-IDF v5.5.4 で `xiaozhi.bin` 0x30e1b0 bytes、 警告なし。

## 検証シナリオ (実機確認結果、 2026-05-19)

ENV III (SHT30 + QMP6988) を Port A に接続して COM3 経由 flash + admin tool-call (`POST /api/mcp/tool-call`) で検証:

### 1. スキャン
```bash
# i2c_scan
# 実測: { "ok": true, "addresses": [68, 112] }
# → 0x44 (SHT30) + 0x70 (QMP6988) が見える ✓
```

### 2. QMP6988 chip ID 読み取り (write_read パターン)
```bash
# i2c_write_read(addr=0x70, write_bytes=[0xD1], n_bytes=1)
# 実測: { "ok": true, "bytes": [92] } = 0x5C
# → QMP6988 chip ID と一致 ✓
```

### 3. SHT30 status read (write + read 個別検証)
```bash
# i2c_write(addr=0x44, bytes=[0xF3, 0x2D])
# 実測: { "ok": true }  → write 成功 ✓
# i2c_read(addr=0x44, n_bytes=3)
# 実測: { "ok": true, "bytes": [0x80, 0x10, 0xE1] }
# → status value + CRC が読み取れた ✓
```

### 4. 物理 bus 分離の確認 (= 旧 protection list の役割を bus 設計で代替)
```
i2c_scan で internal IC (0x34/0x38/0x58/0x68/0x6F 等) が一切返らない。
旧設計では runtime check で write block していたが、 再設計では
内部 IC が tool 経由で到達不可 (物理的に別 controller) のため安全。
```

### 5. SHT30 温湿度測定 (未実施だが上記から成功必須)
```python
i2c_write(addr=0x44, bytes=[0x2C, 0x06])  # 高精度測定コマンド
# 15ms 待機
i2c_read(addr=0x44, n_bytes=6)
# T_msb, T_lsb, T_crc, H_msb, H_lsb, H_crc を取得 → Python で温湿度算出
```

→ 4 tool 全て動作確認完了。 PR ② としての upstream PR 投入可能状態に到達。

## 関連リソース

- ENV III 公式: `https://docs.m5stack.com/en/unit/envIII` (SHT30 + QMP6988、 Grove Port A)
- M5Stack CoreS3 GPIO 仕様: `https://docs.m5stack.com/en/core/CoreS3` (Port A = GPIO 2/1)
- ESP-IDF v5.5 I2C master driver: `https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/api-reference/peripherals/i2c.html`
- SHT30 データシート: I2C 0x44、 command + read 形式
- QMP6988 データシート: I2C 0x70、 register read 形式
- stackchan-mcp の既存 MCP server: `temp/stackchan-mcp/firmware/main/mcp_server.h`
- stackchan board の I2C 初期化: `temp/stackchan-mcp/firmware/main/boards/stackchan/stackchan.cc`

## ログ

- **2026-05-19 (午前)**: 設計議論完了、 案 C (protection list) ベースで PR ①/② 設計確定。 PR ① / PR ② 初期版 (旧 commit `024234b` / `57f0533`) を実装、 dev/integration に merge。 関連 intent doc `docs/intent/stackchan_extension_modules.md` 作成。
- **2026-05-19 (午後 — 旧設計の問題発覚)**: 実機テストで i2c.scan が internal IC のみ返し、 Port A の ENV III (0x44/0x70) が見えない。 ENSPHome 公式ドキュメントで「`AUDIO_CODEC_I2C_*` = internal device 共通 bus、 Port A = GPIO 2/1 独立 bus」 と判明。 xiaozhi-esp32 系全 board に Port A bus init 自体が無いことも確認。
- **2026-05-19 (夜 — 再設計実装)**: PR ② を「Port A bus init + 汎用 I2C tool」 にスコープ拡張。
  - `feature/mcp-i2c-generic-tools` を `ed4e62c` (PR ①) に reset
  - firmware (`42154df`) + gateway (`6fc715d`) を新 2 commit で push
  - `dev/integration` も `6b4fa53` に reset → 新 feature を merge (`3ecbd5b`) → force push
- **2026-05-19 (夜 — 実機動作確認)**: ENV III を Port A に接続して admin tool-call で検証。 4 tool 全て期待通り動作。 PR ② upstream PR 投入待ち。
