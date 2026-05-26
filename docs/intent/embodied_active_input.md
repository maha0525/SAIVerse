# Intent: 身体性の能動入力レイヤー (Embodied Active Input)

**ステータス**: v0.1 ドラフト (2026-05-25)

## これは何か

ペルソナの身体 (Vessel: スタックチャン / 将来は他形態) に対して、ユーザーが**明示的なボタン/キー操作で能動的に指示を送る**ための入力レイヤー。第一実装として **BLE Bluetooth リモコン** (安価な BLE HID デバイス: シャッターリモコン / 小型キーボード / BLE ゲームパッド等) を扱う。

`embodied_passive_input.md` (受動入力 = センサー値の常時注入・閾値発火) と**対**になる。あちらは「身体が勝手に感じ取る」値、本書は「ユーザーが意図して押す」操作を扱う。

まはー の元々の枠組み名は「ｽﾀｯｸﾁｬﾝ Bluetooth リモコン機能」。本書ではそれを passive_input と対になる能動入力レイヤーとして位置づけ、将来の能動入力デバイス (BLE 以外のリモコン、CardKB の無線版相当など) も同じ枠に乗せられる抽象で書く。命名の抽象度はレビューで見直し可。

## これは何でないか

- **受動センサー入力ではない** (`embodied_passive_input.md`)。温湿度・照度のような「感じ取る」値は扱わない。
- **有線拡張モジュールではない** (`stackchan_extension_modules.md`)。CardKB 等の I2C 有線入力は汎用 I2C 口で済むが、本書の BLE HID は無線でファーム改修 (HID ホスト追加) が必須という別軸。
- **出力 (ジェスチャー/表情) ではない** (`embodied_expression.md`)。本書が叩く先は出力系デバイスツールだが、入力 → 機能発火の経路そのものが主題。
- **ペルソナの自律行動ではない**。ユーザー起点の決定的な操作であり、LLM の判断を介さない (後述・不変条件3)。

## なぜ必要か

現状、ｽﾀｯｸﾁｬﾝで入力に使えるのは LCD のタッチパネルと body 3 ゾーンタッチ程度で、**入力インターフェースが貧弱**。一方ｽﾀｯｸﾁｬﾝは PC から離れた場所に置かれるケースがあり、「ｽﾀｯｸﾁｬﾝ付近で操作を完結させたい」需要がある (例: 音量操作)。アドオン管理 UI からも制御できるが、それは PC 操作であって近接完結ではない。

近接で入力を増やす手段は「本体に入力装置を増設する」か「無線入力を受ける」かの二択。M5Stack のボタン/ユニット増設は本体がゴテゴテするため、無線を採る。

### なぜ BLE か (検討経緯, 2026-05-25)

- **Switch Joy-Con は不可**: 旧 Joy-Con は Bluetooth Classic (BR/EDR)。ｽﾀｯｸﾁｬﾝの CoreS3 = ESP32-S3 は **BLE only** で Classic 非対応なので、無線の規格レベルで繋がらない。
- **IR (赤外線) は却下**: CoreS3 基板には IR 送受信 (IRM56384) が内蔵されているが、家庭の TV リモコンを入力に使うと **TV 等を誤操作する**。誤操作を避けるには専用リモコンを別途用意するしかなく、それは「家にある物で済む」入手性の利点を失う。
- **内蔵センサー (IMU/近接/マイク) も却下**: 叩く/傾けるジェスチャ入力は手軽だが、将来ｽﾀｯｸﾁｬﾝに足/車輪を付けると通常動作中に誤検知する。光も同様。マイクは音声入力と被り、サーボフィードバックはペルソナ自身の出力なので入力に使わない。
- **BLE の本質的優位**: 明示的なペアリングで 1 対 1 接続するため**他機器を誤操作しない**。今使っていない BLE HID 機器があれば流用できる。弱点は「TV リモコンほど誰の家にもある」とは言えない点 (BLE HID に限られる)。

## 設計の骨子

### レイヤ分担

```
[ファーム: stackchan-mcp 系]               [SAIVerse 本体]
BLE HID ホスト (リモコンを受ける)
   ↓ 物理イベントを構造化のまま
input_event を WS で push   ──────→   物理イベント受信
   (既存 SendText 経路)                    ↓ マッピング表 (ユーザー設定, DB+UI)
                                        {tool_name, args} を引く
                                            ↓
                                        デバイス MCP ツールを直接実行
                                        (LLM 非経由)
                                            ↓ gateway 経由で device へ
[ファーム]  ←──── 音量/LED 等が変化 ←────
```

論理アクションを直接「機能」に結ぶのではなく、ファームは物理イベントを**横流し**し、SAIVerse がマッピングして既存のデバイス MCP ツールを叩く。

### 物理イベントの粒度

ファームが上げるのは生 HID バイト列ではなく、**構造化された物理イベント** (例: `keyboard: key=VolumeUp, action=down` / `mouse: wheel=+1` / `gamepad: button=A, action=up`)。生レポートを流すと SAIVerse 側で HID ディスクリプタ解釈が必要になるため、デバイス種別 + 構造化状態まではファームの責務とする。論理アクション化はしない。

down/up は**生のまま**流す。「クリック/長押し/連打」の判定も SAIVerse 側に寄せ、ファームで半論理化しない。

### マッピング = ボタン → {tool, args} の直接バインド

機能割り当ては、固定の論理アクション集合 (VOLUME_UP 等) を自前で持たず、**既存デバイス MCP ツールに直接バインド**する。ツール名がそのまま論理アクションの役割を兼ねる。

- マッピング表は SAIVerse 側の DB に持ち、UI (ゲームのキーコンフィグ風) で編集する。
- `args` は 2 モード: **固定値** (キー → `{"volume": 70}`) と、連続値デバイス用の**入力値差し込み** (マウスホイール → `{"delta": <wheel_value>}`)。
- デバイスの表現力に応じて割り当てる。ボタン多いデバイスは多数のツールを、1 ボタン機器は 1 ツールだけ持てばよい。

### LLM 非経由の直接実行

ボタン → ツールは決定的マッピングなので、LLM の往復・コスト・非決定性を一切噛ませない。

- ネイティブ/MCP ツールは共に `tools/__init__.py` の `TOOL_REGISTRY` に登録され、`TOOL_REGISTRY[name](**args)` で名前+引数で直接呼べる。
- デバイス制御 (set_volume 等) は既存の `saiverse-stackchan-addon/api_routes.py:_call_device_mcp_tool` が `conn.call_tool(tool_name, args)` を **persona_id=None** で叩いており、`building_ids` ゲート (TOOL_REGISTRY ラッパー側にのみ存在) を迂回する。リモコンもこの**ペルソナ非依存のデバイス MCP 経路を再利用**する。

### 機能割り当て先 = 既存デバイス MCP ツール (棚卸し済, 2026-05-25)

`saiverse-stackchan-addon/mcp_servers.json` の `spell_tools` に出力系が 15 個 visible。リモコン割り当て候補:

| 分類 | ツール |
|---|---|
| 音 | `set_volume` |
| 光 | `set_led` / `set_all_leds` / `set_leds` / `clear_leds` |
| 画面 | `set_brightness` |
| 表情/口/目 | `set_avatar` / `set_mouth` / `set_mouth_sequence` / `set_blink` |
| 首 | `move_head` |

現状 UI に配線されているのは `set_volume` と `clear_leds` の 2 つのみ。残り 13 個は MCP 経由で叩けるのに手動操作の口がなく、リモコン割り当て先として活用できる。

### ファーム側スコープ (現役ファーム = `temp/stackchan-mcp`, xiaozhi-esp32 ベース)

1. **運用フェーズで BLE HID ホストを init** — BluFi が去った後の BLE コントローラを単独使用。
2. **HID 入力 → `input_event` JSON → WS push** — `websocket_protocol.cc:SendText` 経路に新メッセージ型を追加。
3. **設定モード再突入時に HID ホストを stop** — BluFi に BLE を譲る (`device_state_machine` に排他遷移を追加)。

> 旧 `saiverse-stackchan-addon/firmware/src/main.cpp` (PlatformIO/Arduino, audio+WS+touch のみ) は**現在未使用の旧実装**。BLE HID は現役の stackchan-mcp 系ファームに足す。

## 不変条件

1. **マッピングは SAIVerse 側に置く**: ボタン → {tool, args} の表は DB に持ち、UI で動的に変更できる。ファームに焼き込まない (= キーコンフィグ変更でファーム再書き込みを要求しない)。
2. **ファームは論理化しない**: ファームの責務は「構造化された物理イベントの横流し」まで。down/up は生で流し、クリック/長押し/連打の判定や機能割り当ては SAIVerse 側で行う。
3. **ボタン → ツールは LLM 非経由の直接実行**: 決定的マッピングに LLM を噛ませない。即応性・ゼロコスト・決定性を保つ。
4. **デバイス制御はペルソナ非依存経路を使う**: 既存 UI と同じ `conn.call_tool` (persona_id=None) を再利用し、`building_ids` ゲートを迂回する。リモコン操作にペルソナのアクティブ性を要求しない。
5. **BluFi と BLE HID ホストはフェーズ排他**: 同時共存させない。設定モード = BluFi、運用モード = HID ホスト。BluFi は Wi-Fi 接続後に BLE コントローラごと deinit する (`blufi.cpp` で確認済) ため、運用中は HID ホストが BLE を単独使用できる。設定モード再突入時は HID を stop する。
6. **切断中の入力は破棄する**: WebSocket 切断中のボタン押下はバッファせず捨てる。古い入力を後から流して誤動作させない。
7. **対応は BLE HID デバイスのみ**: Bluetooth Classic (BR/EDR) は ESP32-S3 が非対応なので扱わない。BLE イヤホン/スピーカー (A2DP/AVRCP) も HID ではないため対象外。

## 段階実装プラン

- **Phase A (ファーム疎通)**: stackchan-mcp 系ファームに BLE HID ホストを追加 (BluFi とフェーズ排他)。1 デバイス (安価な BLE シャッターリモコン or 小型 BLE キーボード) で接続 → `input_event` を WS push できるところまで。
- **Phase B (SAIVerse 受信 → 既存ツール)**: SAIVerse 側で `input_event` を受信し、マッピング表 (DB) を引いてデバイス MCP ツールを直接実行。まず `set_volume` / `clear_leds` に配線して end-to-end を通す。
- **Phase C (マッピング UI)**: ゲームのキーコンフィグ風 UI でボタン → {tool, args} を編集可能に。残り 13 個の制御ツールも割り当て対象に。
- **Phase D (連続値 / 複数デバイス)**: マウスホイール/スティックの値差し込み (`args` への入力値注入)、複数デバイス種別の同時対応。
- **Phase E (将来: ペルソナへの働きかけ)**: 「話しかけて」「こっち見て」のような働きかけも「ペルソナに入力を渡すツール」を作れば同じ枠に乗る。vessel 制御と能動的働きかけを "ツールを叩く" に統一。

## upstream PR 戦略との整合

BLE HID ホスト + `input_event` push は、特定アプリに依らない**汎用の能動入力口**として stackchan-mcp 本家への PR 候補になりうる (`stackchan_extension_modules.md` の汎用口戦略、`stackchan_mcp_upstream_pr_strategy.md` のロードマップと整合)。本家保守姿勢を見ながら投入判断する。

## 将来のツール追加 (本書スコープ完了後・別口)

本書の Phase A〜E (既存デバイスツールへのリモコン割り当て) が完了した後の**別口タスク**として、リモコンから叩けると特にユーザビリティが上がる**モードチェンジ系ツール**を stackchan-mcp に新規追加し、upstream PR を投げる。現状のデバイスツール (棚卸し済 15 個) には無い操作群:

- **Listening モードへの移行** — 音声入力受付を開始する
- **Listening 中止 (送信せず終了)** — 録音をキャンセルし、何も送らない
- **Listening 完了 (送信して終了)** — 録音を確定し、音声を送信する
- **Speaking 中止** — TTS 再生を割り込み停止する (`docs/issues/stackchan_speech_interrupt.md` 関連)
- **(可能なら) Wi-Fi 設定モードへの移行** — `EnterWifiConfigMode()` のツール化

これらが揃うと「手元のリモコンで Listening 開始 → 話す → 完了送信」「喋りすぎを Speaking 中止」のような対話制御が近接で完結する。

> **注意 (Wi-Fi 設定モード移行)**: 不変条件 5 (BluFi と HID ホストはフェーズ排他) により、Wi-Fi 設定モードに入った瞬間 BLE HID ホストが停止し、**リモコン自体が切断される**。設定モード移行ボタンは「押したら以降の設定はスマホ (BluFi) 側で行う」片道操作になる。この性質を許容できる範囲で提供する。

これらは MCP ツール (= ファーム側実装 + `mcp_servers.json` 登録) として追加し、`stackchan_mcp_upstream_pr_strategy.md` のロードマップに別 PR として載せる。

## 関連 doc

- `docs/intent/embodied_passive_input.md` — 対になる受動入力レイヤー (センサー値注入・閾値発火)。
- `docs/intent/embodied_expression.md` — 出力側 (ジェスチャー/表情)。本書の入力が叩く先の一部。
- `docs/intent/stackchan_extension_modules.md` — 有線拡張モジュール (CardKB 等の入力含む)。本書は無線 BLE HID という別軸。
- `docs/intent/stackchan_vessel.md` — Vessel = 身体メタファー、gateway 統合、building_ids visibility (A-3-c)。
- `docs/issues/stackchan_mcp_upstream_pr_strategy.md` — upstream PR 戦略。BLE HID 口 PR はここに追加する形。

## オープン課題

- **BLE HID ホストの実装手段**: Bluepad32 (BLE gamepad/mouse/keyboard 対応、ESP32-S3 可) を使うか、ESP-IDF の BLE HID Host API を直書きするか。xiaozhi-esp32 の NimBLE 構成との相性を実装着手時に確認。
- **BluFi deinit との init 順序**: BluFi の deinit は非同期タスク (`blufi.cpp` の `xTaskCreate("blufi_deinit", ...)`)。HID ホスト init がそれと競合しない順序保証をどう取るか。
- **対応デバイスの実機検証**: どの安価 BLE リモコンが BLE HID として正しく見えるか (店頭の「Bluetooth」品に Classic が混じる罠は Joy-Con と同じ)。「BLE / Bluetooth Low Energy」明記品を選ぶ前提を実機で確認。
- **レイテンシ**: ボタン → WS 往復 → SAIVerse 判定 → WS でデバイスコマンド → ファーム実行、の往復遅延。LAN なら数十 ms 想定だが、将来「緊急停止」のような即応必須操作が出たら、それだけファーム側ローカルショートカットを残す逃げ道を検討。
- **マッピング保存単位**: vessel 単位 / persona 単位 / building 単位のどれで持つか (Vessel Building は capacity=1 単一 persona 前提)。
- **常駐 RAM**: HID ホスト常駐分と avatar 描画の PSRAM ピーク (`stackchan_avatar_psram_peak`) の兼ね合い。BluFi 分とは排他なので純増は HID 分のみ。実測で確認。

## 決定事項記録

### 2026-05-25 確定 (起草インタビュー)

- **入力方式は BLE HID に決定**: Joy-Con (Classic, S3 非対応) / IR (誤操作) / 内蔵センサー (可動部で誤検知) を却下した上での選択。明示的ペアリングで誤操作しない点が決め手。
- **マッピングは SAIVerse 側**: 動的キーコンフィグのため。ファームは物理イベント横流しに徹する。
- **ボタン → デバイス MCP ツール直接バインド**: 固定の論理アクション集合を持たず、ツール名で代替。`args` は固定値 + 入力値差し込みの 2 モード。
- **LLM 非経由の直接実行**: 既存の `_call_device_mcp_tool` (persona_id=None, gate 迂回) 経路を再利用。
- **BluFi とはフェーズ排他**: 共存設計は不要 (BluFi は Wi-Fi 接続後にコントローラごと deinit するとコードで確認)。
- **現役ファーム = stackchan-mcp 系**: 旧 `saiverse-stackchan-addon/firmware/` は未使用。BLE HID は stackchan-mcp 系ファームに追加し、upstream PR 候補とする。
- **将来のモードチェンジ系ツール追加**: Listening 移行/中止/完了、Speaking 中止、(可能なら) Wi-Fi 設定モード移行を、本書スコープ完了後の別口 PR として stackchan-mcp に追加する。Wi-Fi 設定モード移行は HID 切断を伴う片道操作になる点に留意。
