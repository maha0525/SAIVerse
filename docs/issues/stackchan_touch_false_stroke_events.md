# Issue: stackchan-mcp の touch driver が誤検知 STROKE event を間欠的に発火

**ステータス**: 🔲 未着手 (= 調査 + upstream PR 候補) — **現在最頻発、 次の着手対象**
**優先度**: 🚨 high (2026-05-21 再評価) — 当初は低-中 (Phase 5' 着手時想定) だったが、 頻度が増しているため最優先対応に変更
**作成日**: 2026-05-19
**再評価日**: 2026-05-21
**関連**: `docs/intent/stackchan_vessel.md` §F (= タッチ知覚の設計、 Phase 5' で正式実装)、 `temp/stackchan-mcp/firmware/main/boards/stackchan/stackchan.cc` (= touch driver 実装)、 `docs/issues/stackchan_mcp_upstream_pr_strategy.md` (= 修正は upstream PR 候補)

## 観測

2026-05-19 の Phase 2' 検証中、 stack-chan device に物理的に触れていないのに STROKE touch event が間欠的に発火することを観測。 serial log で正常な「撫で」 操作と並べると、 raw 値で明確に区別できることが判明。

### 6 event の log

| 種別 | 時刻 | event | zones | raw |
|---|---|---|---|---|
| 誤検知 | 14:37:56 | STROKE | 000 | `0x1130` (= 4400) |
| 誤検知 | 15:02:23 | STROKE | 000 | `0x1E78` (= 7800) |
| 誤検知 | 15:41:00 | STROKE | 000 | `0x157C` (= 5500) |
| 正常 (撫で) | 15:46:22.041 | **TAP** | 000 | `0x00` |
| 正常 (撫で) | 15:46:22.840 | STROKE | 000 | `0x1F3` (= 499) |
| 正常 (撫で) | 15:46:38.341 | STROKE | 000 | `0x190` (= 400) |

source log: `~/.saiverse/user_data/logs/20260519_134542/stackchan_serial.log`

誤検知 3 件の発生間隔: 24 分 27 秒 / 38 分 37 秒 → 間欠的 (= 一定周期じゃない)、 静電容量 sensor のノイズ / 温度変化 / 接地条件等に起因する可能性。

## 観測された差分と仮説

### 1. raw 値が誤検知 signature 候補

- **誤検知 (3 件)**: `0x1130` / `0x1E78` / `0x157C` = **4400 〜 7800**
- **正常 (2 件)**: `0x1F3` / `0x190` = **400 〜 499**
- 約 10 倍の差、 threshold filter で切り分け可能と推定

**注意**: 観測サンプル数が少ない (= 誤検知 3 件、 正常 2 件)、 「raw が大きい正当な強いタッチ」 が存在し得るか確認が要る。 firmware の touch driver コードで raw 値の意味を確認 (= 触られた強さの直接表現か、 別の何かか) してから threshold 値を決める。

### 2. `zones=000` は識別シグナルにならない

6 件全てで `zones=000`。 期待としては「head zone hit」 等が出るはず (= stackchan board は Si12T sensor で頭部タッチ判定する設計とされる)、 しかし全件で 000 が出てる。 firmware の zone 判定が:

- 実装途中で未完成 (= TODO のまま)
- stackchan board の 1 sensor 構成では zone 判定が原理的に常に 0 (= zone 単位の分離は別 board 用の API?)
- 別の理由

のどれかの可能性。 これは別個に **zone 判定機構の調査** が必要 (= 下記「解決案候補 (3)」)。

### 3. `duration=lums` format bug

6 件全てで `duration=lums` という同じ文字列が出力されている。 これは firmware 側で `%lums` (= `%lu` で unsigned long + 「ms」 単位接尾辞を意図) と書いた format string が、 実際には `%l u m s` のような解釈で「lums」 という literal 文字列が直接出力されてる可能性。 つまり **実際の duration 値が読めない format bug**。

memory `feedback_esp_idf_nano_printf_no_zu.md` と類似の現象 (= ESP-IDF nano-printf の format string 制約)。 nano-printf は `%zu` を出すと panic するが、 `%lums` のような連結 specifier も解釈失敗 → そのまま文字列出力する可能性。

該当箇所は `temp/stackchan-mcp/firmware/main/boards/stackchan/stackchan.cc` 内の touch event ログ出力 (= `ESP_LOGI(TAG, "touch event: ...")` 系)、 grep で要特定。

## 解決案候補

### (1) firmware 側に raw 値 threshold filter を追加

`StackChanBoard` の touch event 判定ロジック内で:

```cpp
if (raw_value > kFalseStrokeRawThreshold) {  // 例: 0x1000 (4096)
    ESP_LOGD(TAG, "touch event: STROKE rejected as likely false positive (raw=0x%X)", raw_value);
    return;
}
```

threshold 値は経験的に **0x500 〜 0x1000 の間** で要調整。 正常 raw 最大 (= 0x1F3 = 499) と誤検知 raw 最小 (= 0x1130 = 4400) の間に大きな gap があるので、 中間値 (= 0x800 〜 0xC00) で十分。

**長所**: 単純、 firmware だけで完結、 upstream PR にしやすい。
**短所**: 「raw が大きい正当な強いタッチ」 が誤って弾かれる可能性 (= 観測サンプル少なく不明)。 解決前に raw 値の意味を firmware code で確認すべき。

### (2) `duration=lums` format bug の修正

該当の `ESP_LOGI` を grep で見つけて、 format string を確認 + 修正:

```cpp
// 修正前 (推測): ESP_LOGI(TAG, "... duration=%lums raw=0x%X", duration_ms, raw);
// 修正後: ESP_LOGI(TAG, "... duration=%lu ms raw=0x%X", duration_ms, raw);
```

別 PR として独立可能 (= 純粋なログ修正、 動作影響なし)。 加えて `duration_ms` の実値が読めるようになれば「短時間 STROKE が誤検知の signature」 等の追加分析が可能になる。

### (3) zone 判定機構の調査 + upstream への確認

`StackChanBoard` の touch driver で zone 判定の実装を確認:

- `zones` field がどう計算されてるか (= sensor reading のどこを参照するか)
- stackchan board の物理 sensor 構成 (= Si12T か別 chip か、 単一 sensor か複数 sensor か)
- `zones=000` が正常な「単一 sensor 構成での読み」 か、 未実装 TODO の placeholder か

upstream に issue 起票 or 直接コード読みで確認。 もし「stackchan board は単一 sensor で zone 細分化されてない」 なら、 ログから zones の出力を消す or zones 概念を再設計する余地あり。

## 投稿戦略

修正実施したら upstream PR 候補。 `docs/issues/stackchan_mcp_upstream_pr_strategy.md` の Series 系列に **PR-I (= touch driver false positive filter + format bug fix)** として追加予定:

- 解決案 (1) と (2) を 1 PR にまとめる ((1) は機能修正、 (2) はログ修正、 関連箇所が近いため)
- 解決案 (3) は調査結果次第で別 PR、 もしくは PR-I に含める

Phase 5' (= タッチ知覚の実装) 着手前に本 issue を解決しておくと、 Phase 5' の検証で「ペルソナが触られたと誤認する」 ノイズが減る。

## 調査 TODO

- [ ] `firmware/main/boards/stackchan/stackchan.cc` の touch event log 出力箇所を grep で特定 + format string 確認 (= `duration=lums` の真因)
- [ ] 同ファイルで raw 値の取得経路を確認 (= raw が「触られた強さ」 直接表現か、 別の何か = sensor I2C reading 等の生値か)
- [ ] stackchan board の物理 sensor 構成を確認 (= 単一 Si12T sensor か、 別 chip か、 zone 判定の物理基盤)
- [ ] 誤検知 raw 値の頻度を長時間 capture で追加観測 (= 0x1000+ が誤検知の必要十分条件か、 中間値 (0x500〜0x1000) の event があるか)
- [ ] 解決案 (1) の threshold を実装、 ローカル fork でビルド + flash + 観測 (= まはー の手元で 24 時間 capture して誤検知 0 件確認)

## 関連

- `docs/intent/stackchan_vessel.md` §F (= タッチ知覚の設計、 Phase 5' 着手時)
- `temp/stackchan-mcp/firmware/main/boards/stackchan/stackchan.cc` (= touch driver 実装)
- `docs/issues/stackchan_mcp_upstream_pr_strategy.md` (= 修正は PR-I 候補)
- `docs/issues/stackchan_avatar_psram_peak.md` (= 副次として stroke reset 抑制を観測していたが、 false stroke が頻発する限り stroke reset 解消の再評価が要る)
- `docs/issues/stackchan_speech_interrupt.md` (= タッチ長押しで発話停止を実装したい。 false stroke を解消してからの順序)
- memory `feedback_esp_idf_nano_printf_no_zu.md` (= ESP-IDF nano-printf format 制約、 類似現象の前例)
- memory `project_esp32s3_usb_cdc_reset_capture.md` (= USB CDC re-enumerate の制約)

## ログ

- 2026-05-19: issue 起案 (= Phase 2' 検証中に間欠 false STROKE を 3 件観測、 raw 値の signature が見えてきた)
- 2026-05-21: **頻度が増し、 最頻発の不具合に**。 触れていないのに STROKE event が連発する状態。 同時に PC 側から **USB デバイス接続解除の通知音** が鳴っており、 stroke event 発火 → device 側で USB-CDC re-enumerate (= 実質 reset / disconnect) が走っている疑い。
  - false stroke 単独の問題ではなく、 false stroke を契機に device が落ちている可能性が高い → ペルソナ稼働中の vessel 体感が大きく劣化
  - 優先度を low-medium → high に引き上げ、 次の着手対象に
  - 着手順序: 本 issue → (副次解消の再評価 = avatar_psram_peak.md) → speech_interrupt.md のタッチ長押し追加
