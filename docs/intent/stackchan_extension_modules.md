# Stackchan 拡張モジュール対応

ｽﾀｯｸﾁｬﾝ (M5Stack ベース) に温湿度センサー・IMU・LLM Module 等の拡張モジュールを **ユーザーがファーム書き換えなしで気軽に追加できる体験** を実現するための設計指針。

`docs/intent/stackchan_vessel.md` (身体メタファー + gateway 統合) の延長線、`docs/issues/stackchan_mcp_upstream_pr_strategy.md` (upstream PR 戦略) と並走する案件。

## これは何か

M5Stack の強みは拡張モジュール群 (Unit / Module 規格、I2C / UART / Grove 接続)。ｽﾀｯｸﾁｬﾝに以下のような拡張を後付けすることで、 ユーザーが「自分のスタッチャン」を作れるようになる:

- 温湿度センサー (ENV III)
- IMU (姿勢・加速度)
- CardKB (キーボード入力)
- LLM Module (オンデバイス推論 NPU)
- その他 M5Stack の各種 Unit / Module

本書はこれらをサポートする 3 段階のユーザー体験モデルと、そのための実装方針を記述する。

## これは何でないか

- **拡張モジュールごとの具体実装手順書ではない**。各 Unit 対応は本書を踏まえて別 issue / addon で扱う
- **stackchan-mcp 本体への深い介入計画ではない**。本家 (kisaragi-mochi/stackchan-mcp) は SAIVerse とは独立したプロジェクトで、こちらが過剰に介入するのは越権。upstream PR は汎用口に絞る
- **LLM Module の即時実装計画ではない**。本書では将来検討項目として記録するに留める

## なぜ必要か

現状、ｽﾀｯｸﾁｬﾝに拡張モジュールを取り付けて使うにはファームウェアをユーザー側で書き換える必要がある。SAIVerse のユーザー層 (AITuber 運用者・創作者中心) は組み込み開発の経験を前提にできないため、ハードルが高すぎて事実上選択肢にならない。

ファーム書き換えなしで Unit を追加できる選択肢を整備することで:

- 「ENV III 買って繋いだら、ペルソナが温湿度を気にしてくれる」程度の体験が addon インストールだけで完結する
- 練度の高いユーザーは自前ファームで深く触れる
- ナチュレ見守り構想 (memory 参照) に必要なセンサー類が現実的に乗ってくる

の 3 つが同時に達成できる。

## ユーザー体験の 3 段階 (C 案で確定)

ユーザーの組み込み経験に応じて 3 つの選択肢を提示する:

| レベル | 想定ユーザー | 操作 | 例 |
|---|---|---|---|
| **1** (初心者) | 組み込み経験なし | 本家ファーム焼き直し (プリセット込み) | ENV III プリセット入りファームを `saiverse stackchan flash` で焼く |
| **2** (中級) | Python は書ける | SAIVerse addon でドライバ書く (汎用口経由) | `i2c_read/write` MCP tool を呼ぶ Python ドライバを addon に置く |
| **3** (上級) | C++ / ESP-IDF 触れる | ファームに Unit ドライバ追加 (プリセットを見本に) | 新 Unit を本家ファーム fork に追加 |

3 段階を **すべて成立させる** ことが本書の主目的。レベル 2 を用意することで、ファーム改修なしで拡張できる道がユーザーに開く。

## 現状

### ファーム側 (stackchan-mcp)

調査日: 2026-05-19。`temp/stackchan-mcp` (fork: maha0525/stackchan-mcp, branch: dev/integration)、ESP-IDF + ESP32-S3、xiaozhi-esp32 v2.2.6 ベース。

- **I2C**: `I2cDevice` 基盤クラスで `ReadReg() / WriteReg() / ReadRegs() / transmit_receive` ラッパー整備済み。ブート時に `I2cDetect()` で 128 アドレス自動スキャン実行
  - 既存搭載 (= 全て internal bus、 GPIO 12/11): AXP2101 (電源) / AW9523 (GPIO 拡張) / FT6336 (タッチ) / PY32 (LED・サーボ電源) / Si12T (ヘッドタッチ) / AW88298 (audio codec) / IMU
  - **Grove Port A I2C bus (GPIO 2/1) は xiaozhi-esp32 全 board で未 init** (2026-05-19 PR ② 再設計時に発見、 後述 A 項参照)
- **UART**: SERVO_UART_NUM (UART1) はサーボ専用 (Feetech SCS0009)。`uart_diag` MCP tool で raw byte 送受信できる診断口あり (visible=false)
- **GPIO**: `gpio_test` MCP tool で HIGH/LOW 切り替えできる診断口あり (visible=false)
- **既存 MCP tool**: `self.robot.*` `self.display.*` `self.touch.*` `self.led.*` 等、本体機能特化のみ
- **拡張モジュール対応コードは現状なし** (ENV III / IMU / CardKB 等の専用実装は入っていない)

**判断材料**: I2C / UART / GPIO の low-level driver は既に ESP-IDF 経由で抽象化されており、 汎用 read/write 用 MCP tool を `AddTool()` で追加するのは既存設計の自然な延長線。

### SAIVerse 側 (saiverse-stackchan-addon)

- `expansion_data/saiverse-stackchan-addon/` (独立 Git リポジトリ、Phase 1-2 完了、2026-05-19 現在)
- gateway は subprocess として SAIVerse の MCP client が起動、stdio で接続
- 新 MCP tool を SAIVerse 側で使えるようにするには `mcp_servers.json` の `spell_tools` 配列に追加するだけ (実装は stackchan-mcp gateway 側、 SAIVerse 側は登録のみ)
- 拡張モジュール対応の議論は intent doc / issues にまだ載っていない (本書が初出)

## 実装方針

### A. 汎用口の upstream PR (本家への提案、レベル 2-3 の基盤)

stackchan-mcp 本家に **汎用 I2C / UART / GPIO read/write tool** を追加する PR を投げる。これがレベル 2 (SAIVerse addon でドライバ) とレベル 3 (ファーム改修) 両方の基盤になる。

**重要 (2026-05-19 PR ② 実装時に判明)**: 「Port A 用 bus init + その上で汎用 tool」 を **1 PR にまとめて投入** する必要がある。 当初は「汎用 tool だけ追加すれば既存 `i2c_bus_` (= internal bus = `AUDIO_CODEC_I2C_*`、 GPIO 12/11) を経由して外部 Unit に届く」 と想定したが、 実際には:

- `i2c_bus_` は internal device 共通 bus (PMU / IO ext / touch / IMU / 音声 codec 等が共有) で、 Port A (GPIO 2/1) には繋がってない
- xiaozhi-esp32 系全 board (`stackchan` / `m5stack-core-s3` / `atom-*` / `esp-box-*` / 他 100+) のコードを grep した結果、 **Port A 専用 bus を init している board は存在しない**

つまり「ファーム書き換えなしで Port A の Unit が叩ける」 体験を成立させるには、 汎用口 PR の中で Port A bus init 自体を新規追加する必要がある。 これは board 固有の改修なので、 board ごとに 1 PR (= stackchan board 用、 m5stack-core-s3 board 用、 ... ) になるか、 もしくは「Port A 用 bus init helper を共通化」 する大規模改修になる。

→ I2C 汎用 tool 案件 (PR ②) では stackchan board のみを対象とし、 他 board のメンテナーが見本として取り込む流れに任せる。 詳細は `docs/issues/stackchan_mcp_i2c_generic_tools.md` 参照。

最小セット (I2C):

- `i2c_read(addr, n_bytes)` / `i2c_write(addr, bytes)` / `i2c_write_read(addr, write_bytes, n_bytes)` / `i2c_scan()`
- いずれも **Port A 専用 bus** に対して動作 (internal device 共通 bus には触らせない — 物理 bus 分離による safety)
- PR ② で実装完了、 dev/integration で動作確認済 (ENV III の SHT30 + QMP6988 を Port A で検出 / chip ID 読み取り成功)

最小セット (UART / GPIO):
- `uart_open(uart_num, baud, tx_pin, rx_pin)` / `uart_read(uart_num, nbytes, timeout)` / `uart_write(uart_num, bytes)`
- `gpio_set_mode(pin, mode)` / `gpio_set_level(pin, value)` / `gpio_get_level(pin)`

これらは `docs/issues/stackchan_mcp_upstream_pr_strategy.md` の PR ロードマップに追加する形で投げる。本家 (kisaragi-mochi) の保守姿勢を見ながら、 必要なら PR description で「他 Unit 開発者の足場として」 のフレーミングを強調する。

### B. 人気 Unit プリセット tool (本家への見本、レベル 1 の基盤)

汎用口とは別 PR で、 人気 Unit の専用 tool を 1 Unit ずつ投げる。これがレベル 1 (本家ファームに焼き込み) の中身になる。

候補 (優先順未確定):

- ENV III (温湿度・気圧) — I2C 0x76 (BMP280) + 0x44 (SHT30)
- IMU Unit — I2C
- CardKB — I2C

本家が受け入れるかは反応次第。 受け入れられなかった場合でも、レベル 2 (SAIVerse addon ドライバ) で同じことができるのでブロッカーにはならない。

### C. SAIVerse 側 Unit ドライバ addon (レベル 2 の中身)

ファーム書き換え不要で Unit 対応できる選択肢として整備する。

- `expansion_data/saiverse-stackchan-env3/` のような Unit ごとの独立 addon
- 内部で汎用 I2C MCP tool (A で追加するもの) を呼ぶ Python ドライバを実装
- ペルソナ向けには高レベル tool (`get_temperature_humidity()` 等) を公開
- `mcp_servers.json` の `spell_tools` で Vessel Building 内のみ visible にする

本家プリセット (レベル 1) と並列で成立する。 ユーザーは両方インストールしても矛盾しない (どちらか先に呼ばれた方が動く程度の話)。

## 守るべき不変条件

### 1. ファーム改修なしで Unit 追加できる選択肢を必ず残す

汎用 I2C / UART / GPIO 口は upstream に必ず提案し、 採用させる (採用されなければ fork で持つ)。これがレベル 2 の前提条件であり、 本書全体の存在理由でもある。

### 2. 本家リポジトリへの介入は最小限に留める

汎用口は提案 (= 他 Unit 開発者の足場、 本家にも価値がある)、 個別 Unit プリセットは別 PR で「見本」として提示するに留める。 本家 (kisaragi-mochi) の方針に逆らって全 Unit プリセットを焼き込む方向に押し進めない。

### 3. SAIVerse addon 側の Unit ドライバ実装は本家プリセットと並列で成立する

レベル 1 (本家ファーム焼き直し) とレベル 2 (SAIVerse addon ドライバ) はユーザーの選択肢として両方存在し、 どちらも動く状態を保つ。 片方に集約しない (= 練度に応じて選べる前提を維持)。

### 4. upstream PR 戦略との整合性を保つ

`docs/issues/stackchan_mcp_upstream_pr_strategy.md` に記載された既存 PR ロードマップに本書の汎用口 PR を追加する形で進める。 既存 PR の merge 状況を見ながら投入タイミングを判断する。

## LLM Module (将来検討項目)

ENV III 等のセンサー系とは別軸の特殊ケース。 本書では設計検討の経緯と判断のみ記録し、 実装着手はしない。

### 仕様

- **M5 Module-LLM**: 内部に AX630C (NPU 付き SoC) を持ち、 オンデバイスで軽量 LLM が動く「箱」
- UART (シリアル) コマンドプロトコルで叩く設計
- 入力テキスト → モジュール内で推論 → 出力テキストが返る
- 一部モデル (LLM630 系) はカメラ入力も受け付ける

つまり「センサー」ではなく「外部 LLM プロバイダ + ローカル NPU」が物理的に生えている形。

### 候補モデル (2026-05-19 調査時点)

AXERA-TECH (AX630C の SoC ベンダ Axera 公式) が Hugging Face に AX630C 最適化済みモデルを公開している:

| モデル | 更新日 | 特徴 |
|---|---|---|
| `Qwen3-VL-2B-Instruct-GPTQ-Int4-AX630C` | 2025/12/22 | 最新。 max_token 2047 / context 1024、 グループ化 prefill (~1152 token) |
| `Qwen3-VL-2B-Instruct-GPTQ-Int4-AX630C-P256-CTX384` | 2025/11/18 | 中間。 prefill 256 / context 384 制約版 (推定) |
| `Qwen3-VL-2B-Instruct-GPTQ-Int4-AX630C-P320-CTX448` | 2025/04/10 | 一番古い。 vision encoder 320×320 patch / context 短め (推定) |

3 つともベースは Qwen3-VL-2B-Instruct + GPTQ Int4、 推論パフォーマンスは TTFT 313ms (画像) / 14 token/s / NPU メモリ 2GB / Flash 2.7GB。 P / CTX サフィックスの正確な意味はモデルカードに明記されておらず、 ファイル名規約から prefill / context length と推測されるが、 vision encoder 解像度 (320 / 384) を指す可能性も残る。

新規採用するなら最新の無印版が筋。

### 設計上の置き場所候補

3 つの選択肢が議論された。1 を基盤レイヤとして実装すれば、 2 と 3 はその上に乗る:

1. **MCP tool として `llm_module_chat(prompt)` を出す** (基盤レイヤ)
   - ファームが UART で叩いて結果を返すだけのシンプルな tool
   - SAIVerse 側は普通の tool として呼ぶ
2. **SAIVerse の `llm_clients/` に新プロトコル (`stackchan_llm_module`) を足して LLM プロバイダとして扱う**
   - `LIGHTWEIGHT_MODEL` 経路に指定可能になり、 ペルソナの軽量思考が物理的にスタッチャンの中で回る (「身体に宿る軽量意識」)
   - ストリーミング応答や思考遅延の扱いに工夫が要る
3. **常時知覚 / イベント発火源として独立** (オフライン身体反射)
   - カメラに人が映ってるか・話しかけられたか等の判定をオンデバイスで実行
   - 重要イベントだけ phenomena システム経由で SAIVerse に上げる
   - ウェイクワード不要の「目が合ったら話しかけてくれる」 「カメラに映ったら反応してくれる」体験が成立
   - WiFi 切れても動く知覚層 (ナチュレ見守り構想と合流可能)

### 性能制約と用途の現実性

候補モデルの性能 (TTFT 313ms / 14 token/s / context 1024) を踏まえると:

- **3 (常時知覚 / イベント発火)** は十分使える。 「人が映ってる/映ってない」 判定なら 1 秒以内に答えが返る
- **2 (LIGHTWEIGHT_MODEL 経路)** は厳しい。 14 token/s ではまともな思考速度にならず、 context 1024 は単発応答か 2-3 往復が限界。 「短い反射的応答だけそっちで出す」 ような限定用途に絞るなら成立
- **1 (基盤 tool)** は 2 / 3 のどちらに進むにせよ必要

### 判断 (2026-05-19)

- **v0.7 着手範囲外**。性能制約と実装コストから、 拡張モジュール対応全体としてはセンサー系を先行
- LLM Module は後々の検討項目として残置
- 実装する時は 1 (基盤 MCP tool) から着手し、 3 (常時知覚) を主用途として組む方針を本書時点で記録しておく

## 関連ドキュメント

- `docs/intent/stackchan_vessel.md` — Vessel Building、 ペルソナの身体メタファー、 stackchan-mcp 統合 (本書の前提)
- `docs/intent/stackchan_avatar_pipeline.md` — アバター表現
- `docs/intent/mcp_addon_integration.md` — MCP × Addon 統合の枠組み
- `docs/intent/multimodal_input_pipeline.md` — MediaBuffer / カメラ画像経路 (LLM Module の視覚処理を 3 経路で組む際に参考)
- `docs/issues/stackchan_mcp_upstream_pr_strategy.md` — upstream PR 戦略 (本書の汎用口 PR はこれに追加)
- `docs/issues/stackchan_mcp_i2c_generic_tools.md` — 本書の第一弾 (I2C 汎用 tool 追加) の設計確定事項・実装計画・検証シナリオ
- stackchan-mcp / M5Stack 関連:
  - `https://github.com/kisaragi-mochi/stackchan-mcp` — 本家 (upstream)
  - `https://github.com/maha0525/stackchan-mcp` — 我々の fork
  - `https://docs.m5stack.com/` — M5Stack 公式 Unit / Module ドキュメント
  - `https://huggingface.co/AXERA-TECH` — LLM Module 用 AX630C 最適化モデル群

## 決定事項記録

実装着手前のインタビューで確定した設計判断。

### 2026-05-19 確定

- **C 案採用**: 汎用口 (本家 PR) + 人気 Unit プリセット (本家別 PR) + SAIVerse addon ドライバ の 3 段階モデル
- **着手順序**: センサー系 (汎用 I2C / UART / GPIO 口) を先行、 LLM Module は後々検討項目として残置
- **upstream への姿勢**: 汎用口は他 Unit 開発者の足場として本家に提案、 個別 Unit プリセットは見本として別 PR で提示、 本家保守姿勢次第で判断
- **SAIVerse addon 側ドライバ**: ファーム書き換え不要選択肢として並列整備 (本家プリセットと両立)
- **LLM Module の用途想定**: 常時知覚 + イベント発火 (3) が主用途、 LIGHTWEIGHT_MODEL 経路 (2) は性能制約で当面見送り
- **LLM Module の実装着手**: v0.7 範囲外、 センサー系完了後に再検討

### 2026-05-19 追加 (PR ② 実装中に判明)

- **Port A bus init 不在の発見**: xiaozhi-esp32 系全 board に Port A I2C bus init が無いことが判明 (詳細は本書「ファーム側 (stackchan-mcp)」 / 「実装方針 A」 / `docs/issues/stackchan_mcp_i2c_generic_tools.md`)
- **PR ② スコープ拡張**: 当初「汎用 I2C tool 追加」 → 「**Port A bus init + 汎用 I2C tool**」 を 1 PR に統合 (board 固有改修)
- **物理 bus 分離による safety**: 旧設計の protection list (案 C、 runtime check で internal IC への write block) は廃止、 Port A 専用 bus に切り替えることで internal IC を物理的に到達不可にする (案 C'、 security by construction)。 「不変条件で安全保証 ＞ runtime check で安全保証」 の判断
- **board 単位での横展開戦略**: stackchan board のみ我々が PR を出し、 m5stack-core-s3 / atom-* / 他 100+ board は各 board メンテナーが見本として取り込む流れに任せる (= 本家への過剰介入を避ける不変条件 2 と整合)
