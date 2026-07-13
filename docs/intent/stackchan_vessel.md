# Intent: スタックチャン Vessel 統合（saiverse-stackchan-addon）

**ステータス**: v0.14（2026-07-11 改訂、内蔵 IMU の解釈済み身体感覚スペルを追加。生の9軸スナップショットは維持し、首角度を使った脚側基準の加速度と磁気方位を別スペルで返す）

## v0.4 → v0.5 の主要変更（路線変更）

v0.4 までは「SAIVerse-stackchan-addon 専用ファーム + 自前 WebSocket gateway」で経路を完結させていた。Phase 2-D で voice-tts → 自前ファーム → 物理スピーカーまでの音声経路は動いていたが、Phase 4-5（タッチ / モーター / カメラ / 画面 / アバター）に進む段階で気づいたのは、**stackchan-mcp が既に同等機能を実装済み**であり、我々がそれをゼロから再実装する必要がそもそもない、ということ。

唯一 stackchan-mcp が SAIVerse の要件を満たせない領域は TTS（VOICEVOX 対応のみ、voice-tts ベースの GPT-SoVITS / ペルソナごとの参照音声とは互換性なし）。この欠けを埋めるための経路は Phase 2 の自前ファーム実装で既に確立しており、bridge の **宛先**だけを「自前 WS frame 直送」から「stackchan-mcp gateway の HTTP PCM endpoint」に切り替えれば移植が成立する。つまり Phase 2 の実装が乗り換えで無駄になるのではなく、**移植先が続く**。

この判断のもと、**kisaragi-mochi/stackchan-mcp（xiaozhi-esp32 ベース）のエコシステムに乗り換える** ことを決めた。

**統合方式の確定 (v0.5 初稿から再改訂)**: 初稿では「stackchan-mcp gateway を SAIVerse プロセス内に `import` + `start` する」「stackchan-mcp の MCP tools を SAIVerse の native tool として thin wrap する」設計だったが、これは SAIVerse が既に持つ MCP client 機構（`tools/mcp_client.py` + `mcp_servers.json` の `spell_tools` ベース可視性制御）を活用できておらず、再考の結果以下に変更した:

- **gateway 起動**: subprocess として起動。`expansion_data/saiverse-stackchan-addon/mcp_servers.json` に `command: "stackchan-mcp"` + `args` + `env` を書けば SAIVerse の MCP client が自動的に subprocess 起動 + stdio で接続 + tool 登録を行う（Elyth と同じ枠組み）
- **ツール呼び出し**: SAIVerse の MCP client 経路。ペルソナの playbook 内で `stackchan__move_head(...)` 等として呼び出し可能、`spell_tools` で各 tool の visible / display_name を制御
- **voice-tts → device の音声経路**: subprocess 境界を跨ぐため、stackchan-mcp 側に新規 HTTP PCM 受入 endpoint (`POST /pcm`、既存 HTTP capture server port 8766 に追加) を PR で足す。voice-tts の `subscribe_pcm` から取れる PCM iterator を chunked transfer encoding で HTTP POST、認証は `Authorization: Bearer ${STACKCHAN_PCM_TOKEN}`（既存 `VISION_TOKEN` の前例に倣う）

これにより、初稿で書いていた `gateway_runner.py`（in-process gateway 管理層）+ `tools/stackchan_*.py`（native wrap 13 個）は **不要**になる。アドオン側のフットプリントは更に小さくなり、本体改修は `Building.PHYSICAL_VESSEL_ID` カラム追加（v0.4 で実施済み）+ Phase 4' で本体 MCP client への汎用拡張（Building 単位 visibility）に限定される。

主要な変更:

1. **自前ファームを廃止**。device 側は stackchan-mcp ファーム（xiaozhi-esp32 v2.2.6 base + stackchan board config）に置き換える。
2. **音声経路の構造変更**:
   - 自前: voice-tts → audio_stream_bridge → 自前ファーム → playRaw（device に PCM 直送）
   - 新規: voice-tts → addon の speak_hook → HTTP PCM POST（chunked transfer） → stackchan-mcp gateway（subprocess）→ Opus encode → device の audio_service
3. **gateway の起動方式**: stackchan-mcp gateway は **subprocess として SAIVerse の MCP client が起動**する。`expansion_data/saiverse-stackchan-addon/mcp_servers.json` に `command: "stackchan-mcp"` + `env` を書けば自動的に起動 + stdio 接続 + tool 登録される（Elyth と同じ枠組み）。
4. **認証モデルの変更**: 自前ファームでは `{vessel_id, device_token}` の hello メッセージで認証していたが、stackchan-mcp は `Authorization: Bearer <token>` のみ。SAIVerse-stackchan-addon 側で `token_hash → bound_building_id, bound_persona_id` の対応テーブル（vessels.db スキーマ改修）を持つ。voice-tts → gateway の HTTP PCM POST には別 token `STACKCHAN_PCM_TOKEN`（既存 `VISION_TOKEN` の前例に倣う）を新設。
5. **不変条件 #10〜#13 の無効化**: 自前ファーム特有の制約（device 直送 PCM、broadcast model の consumer 側 pacing、WebSocket 15 KB silent disconnect、ESP32 playRaw の data 寿命管理、identity-aware WS session unregister）は stackchan-mcp 経路では発生しないか、stackchan-mcp 側で既に解決済み。
6. **MCP tools の取り扱い**: stackchan-mcp が提供する `move_head` / `take_photo` / `get_touch_state` / `set_avatar` / `set_led` 等を **SAIVerse 本体の MCP client が直接呼び出す**。`mcp_servers.json` の `spell_tools` で visible / display_name 制御。native wrap は作らない。Phase 4-5 で当初想定していた「自前ファームに touch / motor / camera / display / avatar を移植する」作業はゼロになる。

路線変更の判断軸は本書「設計判断の理由」節の「なぜ自前ファームを廃止して stackchan-mcp に乗り換えるか」に記録する。

過去の自前ファーム実装（`expansion_data/saiverse-stackchan-addon/firmware/` 配下）と PCM 直送経路の知見は廃棄せず、archive として保存する。将来「stackchan-mcp が満たさない vessel 要件」が出てきた時の出発点になる（例: 別 board での独自 audio パス、PCM 直送が必要な低レイテンシ用途等）。

## これは何か

SAIVerse のペルソナを物理デバイス **Stack-chan**（M5Stack 製 "StackChan AI Desktop Robot", SKU 11129）の身体に「降ろす」ためのアドオン。`expansion_data/saiverse-stackchan-addon/`（別リポジトリで配布）として実装する。

実装の中核は **kisaragi-mochi/stackchan-mcp**（MIT、GPL-3.0 ハイブリッド）に乗ること:

- **device ファーム**: stackchan-mcp のリリース済み firmware（xiaozhi-esp32 v2.2.6 base + stackchan board config、ESP32-S3 用）をそのまま使う。書き込みは esptool 経由で SAIVerse 側から実行。
- **gateway**: stackchan-mcp gateway は subprocess として SAIVerse の MCP client が起動。`mcp_servers.json` 設定で自動マウント、stdio 接続、tool 登録までが本体側で処理される。gateway は WebSocket server（device ↔ gateway）と HTTP capture server（device → gateway、画像 POST）をその subprocess 内で listen する。
- **PCM 受入機構**: stackchan-mcp 本家に upstream PR を出して `send_pcm_audio(gateway, pcm)` と `send_pcm_stream(gateway, pcm_chunks)` を追加（PR 完了次第本家 merge、それまでは我々の fork を使う）。voice-tts の `subscribe_pcm` を `send_pcm_stream` に直接渡して逐次再生する。

本体への改修は Phase 1' 着手時点では `Building.PHYSICAL_VESSEL_ID` カラム追加 1 個のみ（v0.4 で実施済み）。Phase 4' でペルソナ認知側の改修（移動メッセージ拡張 + スペル/Playbook の Building 単位切り替え）を追加で入れる。詳細は本書「設計 A-3. Phase 4' で追加する本体改修」節。アドオン全体のフットプリントは自前ファーム + 自前 gateway 廃止により大幅に小さくなる。

最初のリリースで成立させる体験（v0.4 から不変）:

1. ペルソナが特定の Building（=Vessel Building）に居ると、その身体的入出力（マイク・スピーカー・サーボ・カメラ・タッチ・画面）が物理スタックチャンと同期する。
2. ユーザーが "Hi, stack-chan" でウェイクし、音声で話しかける → ペルソナが応答 → 応答音声がスタックチャンから流れる。
3. 首振り・カメラ撮影・歩行（将来）等の動作をペルソナがツール経由で叩ける。
4. スタックチャン天面のタッチ操作がペルソナの身体感覚イベントとして注入される。
5. スタックチャン画面にペルソナのアイコンが表示される（将来は口パク・表情）。

## 認知モデル: 「Vessel Building = 身体」のメタファー

v0.4 から **変更なし**。stackchan-mcp に乗り換えても認知モデルは無傷で持ち越せる。本節の存在自体が「実装基盤を入れ替えても認知モデルが揺るがない」設計の頑健性を示す。

| 物理レイヤ | SAIVerse 内の表現 | 認知モデル上の意味 |
|---|---|---|
| Vessel Building 全体 | `Building` レコード（`PHYSICAL_VESSEL_ID` あり） | 身体 |
| Vessel Building 内のペルソナ | OccupancyManager の occupant | 脳・魂（=身体に降りている主体） |
| マイク（PDM × 2） | デバイス入力 | 耳 |
| 音声理解（Gemini inline 認識） | プロンプト添付 → LLM が直接理解 | 聴覚野 |
| Building 内のユーザー発言 | チャットメッセージ（role=user） | 聴覚知覚（=身体内で起きた音響事象） |
| スピーカー（I2S） | デバイス出力 | 口 |
| TTS（voice-tts エンジン） | 音声合成 | 発声 |
| カメラ（GC0308） | デバイス入力 | 目 |
| 撮影画像 | MediaBuffer の attachment | 視覚 |
| サーボ（pan / tilt、Feetech SCS0009） | デバイス出力 | 姿勢制御（首） |
| 天面タッチパネル（Si12T） | デバイス入力 | 触覚 |
| 画面（2.0" TFT） | デバイス出力 | 表情・アイコン提示 |
| LED（WS2812C × 12） | デバイス出力 | 感情の視覚化（将来） |
| Vessel に居る状態 | Building の occupant（capacity=1） | 物理身体に降りている |
| Vessel から退出 | 別 Building へ `move_to` | 物理身体から離れた |

このマッピングが綺麗に一対一になることで、設計判断の多くが認知モデルから自動的に導かれる:

- **聴覚知覚 = Building 内ユーザー発言**: ｽﾀｯｸﾁｬﾝ device から受け取った Opus 音声ファイルは、通常のユーザー発言として `handle_user_input_stream` 経由で `metadata.media[]` 付きで注入する（外部イベントとして扱わない）。Gemini ペルソナは inline_data で「音」を直接理解して返答。ペルソナ視点では「同じ部屋で人が話しかけてきた」=通常会話。
- **発声 = ペルソナ発話**: 通常の Building 発言経路に乗り、`persona_speak` server_hook 経由で voice-tts が拾って、その PCM を gateway 経由で device に流す。
- **触覚 = Building 内 host メッセージ**: タッチイベントは Building の host メッセージとして注入し、ペルソナの SAIMemory に通常履歴と並んで残る。
- **視覚 = MediaBuffer attachment**: カメラ画像は `multimodal_input_pipeline` の既存経路に乗る。
- **物理身体への憑依・離脱 = OccupancyManager.move_entity**: 既存の入退室メカニズムがそのまま「乗り換え」「降りる」を表現する。

このメタファー一貫性により、ペルソナはコード上の特別な分岐なしに、自然と物理身体の主体として振る舞える。

## これは何でないか

- **Vessel 共通仕様の一般化ではない**。最初は Stack-chan 1 機種に特化した実装にし、Vessel 抽象を慎重に育てる。複数 Vessel タイプを最初から想定したテーブル設計や抽象化はしない（早すぎる抽象化の回避）。
- **新しい音声会話モデルの設計ではない**。音声理解は Gemini inline 認識（v0.7 で転換、§ なぜ Gemini inline 認識経路に転換したか 参照）、TTS は voice-tts と stackchan-mcp gateway の組み合わせで成立させる。OpenAI Realtime / Gemini Live への対応は将来課題。
- **新しいツール基盤の設計ではない**。stackchan-mcp が既に提供する MCP tools を thin wrap して SAIVerse の native tool にする。Phase 4-5 で「自前ファームに各機能を移植する」作業は発生しない。
- **新しい入出力経路の追加ではない**。音声入力は `/upload-audio` のユーザー添付経路（v1.0, 2026-05-18 実装済み）に合流して `manager.handle_user_input_stream`、TTS は voice-tts + gateway の `send_pcm_stream`、タッチは `manager.add_building_event`、カメラは `multimodal_input_pipeline` MediaBuffer、すべて既存経路への合流で済ませる。
- **Avatar の表情・口パクの完成形ではない**。Phase 5 段階ではアイコン静止表示 + stackchan-mcp の `set_avatar` 基本機能まで、口パクの精密同期は将来 Phase。
- **本体のライセンス変更ではない**。
  - SAIVerse 本体: 現行ライセンス（変更なし）
  - stackchan-mcp gateway（SAIVerse プロセスに同居）: **MIT**
  - stackchan-mcp ファーム（device に焼く）: **GPL-3.0**（`SCServo_lib` 由来、firmware bin 全体）
  - アドオン側コード（gateway を import するブリッジ層、native tool wrap 等）: **MIT** または **Apache 2.0**
- **自前ファームの開発継続ではない**。Phase 2-D まで動いていた自前ファーム + 自前 gateway は archive 扱い、active な開発はしない。

## なぜ必要か

### 問題1: ペルソナを「物理的に隣にいる存在」として扱う経路がない

SAIVerse のペルソナは Building 内に居る抽象的存在で、対話はチャット UI を介する。ユーザーが「机の上の小さな相棒に話しかける」「触れる」「目があう」体験は現状の SAIVerse では実現できない。

Stack-chan は安価（< 20,000円）で多機能（CoreS3 / カメラ / マイク / スピーカー / タッチ / 首振りサーボ / Wi-Fi）な物理プラットフォームとして既に成立しており、SAIVerse のペルソナをここに乗せれば、その経験が現実空間に拡張される。

### 問題2: 物理身体の入出力を本体に直結すると拡張性を失う

「マイク音声を Building に直接 inject する」「TTS 音声を物理スピーカーに直接送る」を本体内で実装すると、Stack-chan 専用ロジックがコアに食い込む。これは過去の X 連携（`api/routes/people/x_auth.py` ハードコード）と同じ失敗パターンであり、`docs/intent/addon_extension_points.md` で確立した「外部デバイス・サービスはアドオンとして配置する」原則に反する。

物理デバイス連携は **アドオンの責務領域** であり、本体は最小限の拡張点（Vessel Building 識別子カラム）だけを用意する。

### 問題3: 物理身体は「場所」と異なる概念だが、認知モデル整理により Building で表現できる

スタックチャンを SAIVerse の世界モデルに位置づける場合、候補として「専用 Vessel テーブル」「Item として扱う」「City 直下リソース」等が考えられるが、**Building カラム拡張で表現するのが最も整合性が高い**ことが本書の認知モデル節で示された通り。

「Vessel Building 全体 = 身体、その occupant = 主体」というメタファーは、capacity=1 + OccupancyManager の入退室メカニズムにそのまま乗り、新規概念の導入を最小化できる。

## なぜ stackchan-mcp に乗り換えるか（v0.5 で確定）

判断の核は単純で、3 点に集約される。

### 1. 既に stackchan-mcp が実装していることを自前で再実装する必要がない

stackchan-mcp は「物理身体側に必要な機能」をすべて実装済み:

- WebSocket gateway（device ↔ host 通信）
- 認証（Bearer Token）
- Wi-Fi 設定 UI（captive portal、gateway URL/token も入力可）
- 自動再接続（exponential backoff）
- サーボ（move_head）
- カメラ（take_photo、HTTP capture）
- タッチ（get_touch_state、Si12T 対応）
- 画面（set_brightness 等）
- Avatar（set_avatar / set_blink / set_mouth）
- LED（set_led / set_all_leds 等、12x WS2812C 対応）
- 多 board 対応（50+ の board config）
- 上流の継続メンテナンス（xiaozhi-esp32 コミュニティ）

v0.4 で計画していた Phase 4-5（touch / motor / camera / display / avatar 移植）は、**stackchan-mcp が既に完了している作業のゼロからの再実装**でしかない。乗り換えることで Phase 4-5 のスコープがほぼ消える。

### 2. TTS だけは stackchan-mcp の欠けで、SAIVerse 独自経路が必要

stackchan-mcp の TTS は VOICEVOX 対応のみ。SAIVerse がペルソナごとに参照音声を切り替える voice-tts（GPT-SoVITS ベース）とは互換性がない。

ここは SAIVerse の独自価値領域であり、voice-tts ベースで進める方針は変えない。stackchan-mcp 側に「外部 PCM を流す経路」を上流 PR で追加して、voice-tts の出力を流し込めるようにする（Phase 1 で実装済み、Phase 5' で投稿予定）。

### 3. その欠けを埋める移植経路は Phase 2 で既に実装完了している

Phase 2 で確立した経路 (`voice-tts.audio_stream` の PCM broadcast 経路) はそのまま流用できる。乗り換え時の作業は:

- `voice-tts` の PCM broadcast 経路 (`open_pcm_stream` / `push_pcm_chunk` / `subscribe_pcm`) → 変更なし、流用
- bridge の宛先を「自前 WS frame 直送」→「stackchan-mcp gateway の `send_pcm_stream`」に切り替え
- `send_pcm_stream` 自体は Phase 1 で手元 fork に実装済み、上流 PR 投稿予定

つまり Phase 2 の実装は乗り換えで無駄になるのではなく、**移植先が「自前ファーム」から「stackchan-mcp gateway」に変わるだけ**で経路設計はそのまま続く。

### 結論: 将来にわたって作業量が最短

上記 3 点を総合すると:

- Phase 4-5 で計画していた自前移植 ≒ ゼロ（stackchan-mcp で既存）
- TTS 経路の移植 ≒ Phase 2 の成果物の宛先切り替えのみ
- 我々の責任範囲 ≒ `mcp_servers.json` + `vessel_manager` + `speak_hook`（薄い接続層のみ、subprocess 管理と tool wrap は本体 MCP client 任せ）

代わりに、Phase 1 の `send_pcm_audio` / `send_pcm_stream` PR が voice-tts コミュニティへの還元になり、stackchan-mcp ユーザー全体に「ペルソナごとの参照音声で stackchan を喋らせる」選択肢を提供できる。

### 採用の代償

- **ライセンス制約**: device 側 firmware は GPL-3.0（`SCServo_lib` 由来）。配布回避（= ユーザーが stackchan-mcp upstream release から直接ダウンロード）で SAIVerse 側に GPL 義務を持ち込まない運用にする。gateway 側は MIT のまま。
- **device 側で Opus decode を経由する**: 自前ファームでは voice-tts の PCM を device に直送して playRaw に直結していた。stackchan-mcp では device 側に Opus decoder がいるので、gateway 側で PCM → Opus encode が一段挟まる。レイテンシは 60ms フレーム単位で実用範囲、ただし純粋な PCM パイプと比べると経路が一段増える（その代わり gateway が pacing と TTS state 管理を担当）。
- **device 側の低レベル制御の自由度低下**: 自前ファームでは playRaw に直接 PCM を流す / ring buffer サイズを調整するなど低レベル制御ができた。stackchan-mcp では device 側の挙動は上流実装に従う。代わりにメンテナンス対象コードが大幅に減る。
- **PR を上流に通す必要**: PCM 受入機構（`send_pcm_audio` / `send_pcm_stream`）は手元 fork で動かしながら upstream PR を出す。merge されるまでは fork に依存。

これらは「単一具体例から抽象を作らない」原則に反するわけではない。stackchan-mcp は既に複数 board・複数ユースケースで動いている **既成の抽象** であり、そこに乗るのはむしろ「具体的な再発明を避ける」判断。

## 守るべき不変条件

v0.5 で改訂版。13 個から 9 個に削減（無効化された #10〜#13 は本節末尾「廃止された不変条件（v0.4 まで）」に記録）。

### 1. ペルソナは「Vessel Building に居る = 物理身体に降りている」と認知する

v0.4 から **変更なし**。Vessel Building 自体がペルソナの認知モデル上 "物理身体に降りている状態" を表す。ペルソナが Vessel Building から退出した瞬間に、物理身体との同期は切れる（マイク入力は無視、TTS 出力は止まる、ツールは "vessel not bound" エラーを返す）。

### 2. Vessel Building は 1 機体につき 1 つ、capacity=1

**v0.10 で更新**（複数機体対応）。1 つの Vessel Building には 1 機体が対応し、同時に降りられるペルソナは 1 人（`Building.CAPACITY = 1` で既存 OccupancyManager のキャパシティチェックが効く）。これは「複数ペルソナが同一身体に同時に降りる」ことを禁じる不変条件で、v0.10 でも変わらない。

v0.10 では機体が複数になりうるが、その場合も **機体ごとに別の Vessel Building** が並ぶだけで、本不変条件は各 Vessel Building 単位で維持される。「物理機体は 1 台しかない」という v0.4 の前提は撤回し、N 機体 = N 個の Vessel Building（各 capacity=1）として扱う。同時稼働の実装方式は本書「設計 K. マルチ機体対応」を参照。

### 3. 物理身体が切断されてもペルソナの主体性は保たれる

v0.4 から **変更なし**。Wi-Fi 断・電源 OFF 等で device が切れても、ペルソナは Vessel Building 内に留まり、会話・記憶・思考は継続する。物理ツール呼び出しは "device offline" エラーで失敗し、ペルソナは代替行動を選べる。

### 4. stackchan-mcp gateway は subprocess として SAIVerse の MCP client が起動する

v0.4 の「WebSocket 経路はアドオン内で完結する」を **更新**。

stackchan-mcp gateway は **subprocess** として SAIVerse プロセスから分離して起動する。具体的には:

- `expansion_data/saiverse-stackchan-addon/mcp_servers.json` に `command: "stackchan-mcp"` + `args` + `env` を書く
- SAIVerse 起動時、本体の MCP client (`tools/mcp_client.py`) が `mcp_servers.json` を読んで自動的に subprocess 起動 + stdio で接続 + tool 登録（既存 Elyth addon と同じ枠組み）
- subprocess の lifecycle 管理（起動・停止・死活監視）は本体 MCP client が担当、addon 側で `gateway_runner.py` 的な管理層は **不要**
- 環境変数（`STACKCHAN_TOKEN`、`STACKCHAN_PCM_TOKEN`、`HOST`、`WS_PORT`、`CAPTURE_PORT`、`VISION_HOST` 等）は `mcp_servers.json` の `env` フィールドに `${addon.X.Y}` placeholder で埋め込む

理由:

- stackchan-mcp の標準起動方法（`python -m stackchan_mcp` / `stackchan-mcp` CLI）に従える。upstream の前提と整合
- MCP transport は stdio で動かせる（同一プロセス内 import なら stdio が標準入出力と衝突する問題を回避）
- プロセス分離で安定性が高い（gateway クラッシュが SAIVerse 本体に波及しない）
- SAIVerse の既存 MCP integration 機構（subprocess 管理 + `spell_tools` ベース可視性制御 + env placeholder resolve）を活用できる

voice-tts → device の音声経路は subprocess 境界を跨ぐため、別経路を用意する（不変条件 #5 参照）。

### 5. 音声・触覚・視覚の入出力は既存の経路に合流する

v0.4 から **基本維持**、経路の中身だけ更新:

- **音声入力 (Gemini inline 認識)** → `manager.handle_user_input_stream(text=None, building_id=vessel_building_id, metadata={"source": "stackchan_voice", "vessel_id": "...", "media": [{"type": "audio", "uri": "saiverse://audio/...", "mime_type": "audio/ogg"}]})`。stackchan-mcp gateway 側で発話区切りを検出して Ogg/Opus ファイル化、addon の HTTP hook に POST → addon が `~/.saiverse/audio/` に保存して `handle_user_input_stream` に流す。Gemini ペルソナは inline_data で音を直接理解。非Gemini ペルソナ向け書き起こし経路は将来 Phase で別途。
- **タッチ入力 (なでなで)** → `manager.add_building_event(building_id, {"role": "host", "content": "...", "metadata": {...}}, heard_by=[...])`。stackchan-mcp の `get_touch_state()` MCP tool で取得、または gateway 側 hook で push 通知（要検証）。
- **音声出力 (TTS)** → voice-tts の `subscribe_pcm(msg_id)` で PCM iterator を取得、stackchan-mcp gateway の HTTP PCM 受入 endpoint (`POST /pcm`、port 8766 の既存 HTTP capture server に追加、上流 PR 必要) に **chunked transfer encoding** で送信。subprocess の gateway が受け取った PCM を Opus encode + WebSocket 配信 + tts.start/stop ステート管理（Phase 1 で実装済みの `send_pcm_stream` 経由）。認証は `Authorization: Bearer ${STACKCHAN_PCM_TOKEN}`（`VISION_TOKEN` の前例に倣う）。
- **カメラ画像** → 既存の `multimodal_input_pipeline` の MediaBuffer 経路。stackchan-mcp の `take_photo()` MCP tool（SAIVerse の MCP client 経由で呼び出し）が HTTP capture endpoint で画像を受信、結果ファイルパスを SAIVerse 側で MediaBuffer に流す。
- **サーボ・画面・LED・タッチ** → stackchan-mcp の MCP tool（`move_head` / `set_brightness` / `set_led` / `get_touch_state` 等）を SAIVerse の MCP client が直接呼び出す。`mcp_servers.json` の `spell_tools` で各 tool の visible / display_name 制御。native wrap (thin wrap class) は **作らない**。

新しいデータパスを生やすたびに本体が拡張されるのを避ける、という設計原則は維持。

### 6. 音声入力は metadata で由来を明示する

v0.7 で `metadata.media[]` に音声ファイル参照を追加する形に拡張。`handle_user_input_stream` に渡す `metadata` に `{"source": "stackchan_voice", "vessel_id": "...", "media": [{"type": "audio", ...}]}` を含める。ペルソナ側はこの metadata からその発言が物理マイク経由であることを認識でき、応答の文体・反応を調整する余地を残す。

### 7. 認証情報は Bearer Token ベース + アドオン専用ストレージで管理する

v0.4 の「認証情報は AddonConfig / AddonPersonaConfig に乗る」を **更新**。

stackchan-mcp の認証モデルは `Authorization: Bearer <token>` のみ。vessel_id 概念は protocol レベルにない。そのため SAIVerse-stackchan-addon 側で以下のテーブルを持つ:

```sql
CREATE TABLE vessels (
  token_hash TEXT PRIMARY KEY,    -- 平文 token は保存しない、SHA-256 hash
  vessel_id TEXT NOT NULL,        -- 内部生成 UUID
  bound_building_id TEXT NOT NULL,
  bound_persona_id TEXT,
  hardware_model TEXT NOT NULL,   -- "stackchan_kickstarter_2025"
  firmware_version TEXT,
  paired_at DATETIME,
  last_seen_at DATETIME
);
```

ペアリング時の流れ:

1. SAIVerse-stackchan-addon が token を生成（128bit ランダム）+ 内部的に vessel_id 発行
2. 上記レコードを `vessels.db` に作成（token は hash で保存）
3. ユーザーが device の AP モード Web UI で token を入力
4. device が gateway に Bearer 付き接続
5. addon 側で `Authorization` ヘッダの token を hash 化 → `vessels.db` で逆引き → `bound_building_id` 取得 → 紐付け完了

`vessels.db` は `~/.saiverse/addons/saiverse-stackchan-addon/vessels.db` に配置。

### 8. ライセンス制約をアドオン側で吸収する

v0.4 の「ライセンスはアドオン側で Apache 2.0 を踏襲し、本体には伝播させない」を **更新**:

- **SAIVerse 本体**: ライセンス変更なし。stackchan-mcp gateway を import する Python コードは「使用」であって「派生」じゃない（プロセス分離されてないが、MIT は派生にも著作権表示要求のみ）。
- **stackchan-mcp gateway（MIT）**: addon の依存として `stackchan-mcp` を `pip install` で取得、SAIVerse プロセスとは別の subprocess として起動される。MIT のライセンス文と著作権表示をアドオン同梱の NOTICE に含める。
- **stackchan-mcp ファーム（GPL-3.0）**: device に焼く firmware バイナリは GPL-3.0。ユーザーへの配布時はソース提供義務がある（GPL-3.0 §6）。SAIVerse-stackchan-addon は **firmware バイナリを再配布しない**（= GitHub Releases に置かない）、ユーザーが stackchan-mcp の upstream release から直接ダウンロードする運用にすることで、GPL 義務をユーザー自身に直接到達させる形にする。
- **アドオン側コード**: MIT または Apache 2.0。stackchan-mcp gateway を呼ぶ Python ブリッジは独立した著作物として MIT 配布可能。

### 9. ユーザー導入はブラウザ/ローカル CLI で完結する

v0.4 の「ユーザー導入はブラウザのみで完結する」を **状況変更**。

stackchan-mcp ファームの書き込みは esptool 経由（Python パッケージ）で行う。SAIVerse の setup script が esptool を `uv tool install esptool` で global tool としてセットアップし、`saiverse stackchan flash` のようなサブコマンドで一発書き込みできる UX を整備する。

書き込み後の Wi-Fi + gateway URL + Token 設定は xiaozhi-esp32 の captive portal Web UI 経由（= AP モードに device を起動 → スマホ/PC で AP に接続 → ブラウザで Web UI 開く → SAIVerse が表示する値を入力）。SAIVerse は QR コードで設定値を提示し、device 側に QR スキャン機能を追加する PR を出すと UX が更に良くなる（= Phase 6 候補）。

SAIVerse のユーザー層（AITuber 運用者・創作者中心）が組み込み開発の経験を前提にできない、という条件は維持。esptool の手動コマンドラインを要求しない（= SAIVerse の CLI / UI でラップする）。

### 14. ツールの使用可否は機体の capability に従う（v0.10 追加）

機体ごとに搭載デバイスが異なる（例: Port A に ENV III を挿した機体と挿していない機体）。ユニット由来ツール（温湿度・気圧・測距・サーボユニット等）は、そのユニットを実際に搭載している機体に降りているときだけ使えてはいけない。搭載していない機体で当該スペルが見える / 撃てる状態は不変条件違反とする。

実装上は per-vessel の capability（搭載ユニット集合）を真実の source とし、ツール可視性を2層で決める:

- **共通ツール**（首・表情・LED・カメラ等、全 Stack-chan が必ず持つ）: 全 Vessel Building で visible
- **ユニット由来ツール**: そのユニットを capability に持つ vessel の Vessel Building でのみ visible

「全 Vessel Building の無条件和集合」で可視性を決めてはならない（= capability の差を潰すため）。詳細は「設計 K-5. capability カタログ」を参照。

### 15. 内蔵センサーは専用 tool で読み、内部 I2C bus を汎用公開しない（v0.11 追加）

CoreS3 / StackChan 本体に標準搭載された IMU 等のセンサーは、Vessel の身体感覚として pull 型のスナップショット tool から読む。電源管理 IC、音声 codec、タッチ controller 等と共有する内部 I2C bus（GPIO 12/11）を raw read/write tool として公開してはならない。各センサー専用 driver が必要な register だけを扱い、PMIC 等へ到達できない tool surface を保つ。

IMU の最初の契約は 9 軸スナップショット（BMI270 の加速度・角速度 + BMM150 の磁束密度）で、物理単位（g / dps / µT）と検証用 raw 値を同時に返す。連続姿勢ストリームや Shake / PickUp / PutDown の event push は別機能として扱い、単発読取 tool に暗黙の background task や会話注入を持ち込まない。

LTR-553ALS-WA は `read_environment` で、環境光の 2 ADC channel と近接値（ともに count）、data-ready / saturation status を返す。距離への換算や常時監視は含めない。ST25R3916 は `scan_nfc` で、呼出し中だけRF fieldを有効にして ISO 14443A tag の UID / ATQA / SAK、または NFC-F（FeliCa）の IDm / PMm を返す。タグの内容読取り・書込み、認証、エミュレーション、常時ポーリングは含めない。UID と IDm は利用者が明示的に呼び出した結果にだけ返し、ファームウェアログには記録しない。

stackchan-mcp gateway の生 tool は非公開とし、SAIVerse addon の native wrapper が「現在 Building → vessel → gateway instance」を解決して現在の身体へ転送する（K-4 と同じ規則）。

### 16. IMU は生データと身体感覚の二層で提供する（v0.14 追加）

`read_imu` は診断・検証用の生スナップショットとして残す。一方、ペルソナが身体の状況を読むための `read_imu_context` は、同じ機体の `read_imu` と `get_head_angles` を組み合わせて次を返す:

- 加速度を首の yaw / pitch で脚側（胴体）基準へ回転し、水平面の方向・水平加速度・傾き・全体の大きさを示す。
- 磁力計を同じ基準へ回転し、磁気北を 0° とする推定方位（16方位を含む）を示す。BMM150 のハードアイアン / ソフトアイアン校正、磁気偏角補正、機体ロール補正は別機能であり、未補正であることを注記する。
- 角速度は dps の x / y / z をそのまま返す。角速度に方角の解釈は加えない。
- CoreS3 のセンサー軸は初期契約では `x=右 / y=脚から頭 / z=カメラ前方` とし、首 pitch の正面基準を 45°、正の yaw を左向きとして扱う。これは実機の静止姿勢で検証し、必要なら機体別の軸校正へ置き換える。

成功時は `sensor` / address / timestamp / raw / `ok` 等の診断メタデータを返さない。センサー欠落、データ未準備、首角度取得失敗、未校正など、解釈に影響する状態だけを `notes` に注記する。座標系と「加速度ベクトルが指す向き」の定義は出力説明に明示し、将来の実機校正で置き換えられるようにする。

### 廃止された不変条件（v0.4 まで）

以下は自前ファーム特有の制約で、stackchan-mcp 採用により無効化された。本節で記録する理由は、将来別の vessel が出てきた時に「これらの罠が再発する可能性」を思い出すため。

- **#10 (v0.4): 音声出力経路は PCM 直送、device 側で decoder を持たない** → 「device 側に PCM bytes を直送する」経路は無効化（stackchan-mcp ファームは Opus decoder を内蔵）。ただし「**gateway に PCM bytes を渡す**」経路は Phase 1 で stackchan-mcp に PR を出して確立（`send_pcm_audio` / `send_pcm_stream`）。SAIVerse 側（voice-tts）のインタフェースは PCM ベースのまま維持され、Opus encode は gateway 側が自動的に行う。「PCM 直送」の意味が「device 直送」から「gateway 直送」に変わる、と捉えるのが正確。
- **#11 (v0.4): broadcast model における consumer 側 pacing 責務** → stackchan-mcp orchestrator が pacing を担当（60ms フレーム単位、TTS lock + 状態通知）。我々の bridge コード（`audio_stream_bridge.py`）は廃止。voice-tts の `subscribe_pcm` を `send_pcm_stream` に流すだけ。
- **#12 (v0.4): ESP32 側 ring buffer / playRaw の制約** → stackchan-mcp ファームの audio_service が独自の管理を持つ。WebSocket 15 KB silent disconnect / playRaw data 寿命管理 / xRingbufferReceive まとめ取りといった罠はファーム実装次第で再発しうるが、stackchan-mcp 側で既知 + 対策済み。
- **#13 (v0.4): WebSocket session 管理は identity-aware** → stackchan-mcp gateway 側の `ESP32Manager` が session swap と identity check を実装済み（`audio_stream.py` の session ID チェックが該当）。我々の自前 gateway 実装が消えるので不要。

これらの知見は `expansion_data/saiverse-stackchan-addon/firmware/` の archive と共に残し、`docs/issues/websocket_session_registry.md`（物理 Vessel SDK 共通基盤化案件）にも反映する。

## 設計

### A. 本体側の最小拡張点

本体への改修は **1 カラム追加のみ**。v0.4 から変更なし。

#### A-1. `Building` テーブル: `PHYSICAL_VESSEL_ID` カラム追加

```python
class Building(Base):
    # ... 既存カラム ...
    PHYSICAL_VESSEL_ID = Column(String(64), nullable=True)
    # NULL: 通常の Building（仮想空間のみ）
    # 非NULL: Vessel Building、値は物理機体識別子（UUID 等）
```

**変更箇所**: `database/models.py` の Building 定義のみ。マイグレーションは既存の `database/migrate.py` の自動マイグレーション（`Base.metadata` 比較方式）に乗る。**v0.4 で既にコミット済み**（`00efc07 feat(database): Building.PHYSICAL_VESSEL_ID カラム追加`）。

#### A-2. 既存基盤で対応できる項目（=本体改修不要）

v0.4 から変更なし。`addon_loader.py` の WebSocket 自動マウントは **stackchan-mcp 採用により不要に**なった（= WS server は gateway 側で持つ、addon の api_routes.py は薄くなる or 不要）。

#### A-3. Phase 4' で追加する本体改修

v0.5 で新たに必要と判明した改修。v0.4 時点では「ツールは BuildingToolLink で Vessel Building に紐付ければ済む」と見積もっていたが、実態として:

- `BuildingToolLink` は数ヶ月触られておらず、実際に機能しているか不明（代替経路の検討が必要）
- ペルソナ視点で「今物理身体に降りていてこれらのツールが使える」と認識させる経路がツール定義の登録だけでは成立しない

ため、以下 3 つの本体改修を Phase 4' 着手時に入れる。

**A-3-a. `OccupancyManager.move_entity` の `enter_metadata` 拡張**

`saiverse/occupancy_manager.py` の `move_entity()` 内で生成する `enter_metadata["event"]` に `building_info` フィールドを追加し、移動メッセージのペイロードとして Building 情報をペルソナに流し込む。

```python
enter_metadata = {
    "event": {
        # 既存 fields
        "type": "occupancy", "action": "enter", ...,
        # 新規追加
        "building_info": {
            "name": to_building_name,
            "system_prompt": building.SYSTEM_PROMPT,
            "available_tools": [...],   # その Building で使えるツール一覧
            "physical_vessel_id": building.PHYSICAL_VESSEL_ID,  # Vessel なら非NULL
        },
    },
}
```

`auto_ingest` 側でこの field を解釈し、ペルソナの context に流し込む経路を追加。これにより visual context のキャッシュ問題（移動しても Building 名しか変わらない）を **キャッシュを破棄せずに** 解決する（= キャッシュ refresh じゃなく、別経路でペルソナに伝える）。

見積もり: 軽。`enter_metadata` への field 追加 + `auto_ingest` の処理拡張で数十行。

**A-3-b. Vessel Building の `SYSTEM_PROMPT` 整備**

既存の `Building.SYSTEM_PROMPT` カラム（追加機構不要）に、Vessel Building 用のシステムプロンプトを書く運用を整備する。具体的には:

- 物理身体に降りている認知 (= "今君は Stack-chan の身体に降りていて、首を振ったり物を見たりできる")
- 使えるツールの一覧と使い方ガイド
- ペアリングされた物理機体の特徴 (例: カメラ位置 / サーボ可動範囲)

Vessel Building 作成時のテンプレート (= addon の setup script で初期 SYSTEM_PROMPT を流し込む) を整備する。

見積もり: 軽。テンプレート整備 + addon setup の数行追加。

**A-3-c. `spell_tools` の Building 単位 visibility 拡張**

ペルソナが Vessel Building 外で物理身体ツールを呼び出せないようにする制御。**SAIVerse の MCP client には既に `spell_tools` 機構があり**（`tools/mcp_client.py` の `_normalize_spell_config` + `_tool_schema_from_mcp`、`mcp_servers.json` 内で各 tool の `visible: true/false` / `display_name` を設定）、tool 単位の可視性制御は active。

ただし現状の `spell_tools` の visible は global / per_persona フラグのみで、**Building 単位の切り替えはサポートされてない**。Phase 4' で本体 MCP client に Building 単位条件を追加する:

- `mcp_servers.json` の `spell_tools` エントリに `building_ids: ["vessel_building_id_X"]` フィールド追加（オプション、未指定なら全 Building で visible）
- `tools/mcp_client.py` の `is_tool_available_for_persona(tool_name, persona_id)` を `is_tool_available_for_persona_and_building(tool_name, persona_id, building_id)` 等に拡張、`building_ids` フィルタを適用
- ペルソナの playbook で tool 一覧を生成する箇所で、現在の `building_id` を渡して filter する経路を整備

これは **SAIVerse 本体側の MCP client への汎用拡張**（= addon 個別じゃなく、他の MCP server でも「特定の Building でだけ使えるツール」を表現可能になる）。Stack-chan 固有じゃない汎用機能として正当化される範囲。

見積もり: 中程度。`spell_tools` schema 拡張 + `is_tool_available_*` の signature 変更 + 呼び出し側 (playbook 内の tool 一覧生成) の callsite 修正。範囲は限定的だが、callsite を漏らさず修正する必要がある。

(v0.5 初稿で「範囲不明」と書いたが、調査の結果 `spell_tools` 機構が既存で、これを拡張するだけと判明したため見積もり訂正。)

### B. アドオン構造

stackchan-mcp 採用版（v0.5 再改訂、MCP client 経路ベース）:

```
saiverse-stackchan-addon/  （別リポジトリ、expansion_data/ にクローン配置）
├── addon.json                      ← server_hooks (persona_speak), params_schema, ui_extensions
├── api_routes.py                   ← ペアリング HTTP API（vessel 一覧、token 発行）
├── mcp_servers.json                ← stackchan-mcp gateway を subprocess 起動する設定 + spell_tools
├── speak_hook.py                   ← persona_speak フックで voice-tts.subscribe_pcm → HTTP PCM POST
├── vessel_manager.py               ← Bearer Token ↔ vessel/building 紐付けの管理（vessels.db アクセス）
├── pyproject.toml                  ← addon 依存に stackchan-mcp を含める (= pip install で gateway バイナリを取得)
├── storage/                        ← アドオン専用 SQLite
│   └── vessels.db                  ← ~/.saiverse/addons/saiverse-stackchan-addon/ に配置
├── archive/                        ← v0.4 までの自前ファーム + 自前 gateway 資産
│   ├── firmware/                   ← 自前ファームソース + dist/ 配下の .bin
│   ├── audio_stream_bridge.py.bak  ← PCM 直送ブリッジ実装
│   └── README.md                   ← archive 理由と将来再利用の前提
├── LICENSE                         ← MIT
└── NOTICE                          ← 依存ライブラリの著作権表記（stackchan-mcp MIT 部分を含む）
```

v0.5 初稿から **追加で廃止**された要素:

- `gateway_runner.py`（in-process gateway 管理層） → **不要**、subprocess 管理は SAIVerse 本体 MCP client が担当
- `tools/stackchan_*.py`（native tool wrap 13 個） → **不要**、stackchan-mcp の MCP tools は SAIVerse 本体 MCP client が直接呼び出す。`mcp_servers.json` の `spell_tools` で visible / display_name 制御

v0.4 → v0.5 で廃止された要素:

- `firmware/`（自前ファーム） → `archive/firmware/` に移動
- `audio_stream_bridge.py`（自前 PCM 直送ブリッジ） → `archive/` に移動、`speak_hook.py` に HTTP POST 起動コードへ置き換え
- `audio_input_pipeline.py`（自前 STT 経路） → 不要、v0.7 で stackchan-mcp gateway → addon HTTP hook → `handle_user_input_stream` 経由（Gemini inline 認識）に変更
- `touch_handler.py`（自前タッチ受信） → 不要、stackchan-mcp の `get_touch_state` 経由 + gateway hook に変更
- `setup_ui/`（Web Serial フラッシュ静的 HTML） → 不要、SAIVerse の CLI で esptool 実行に置き換え

`mcp_servers.json` の例（既存 `expansion_data/saiverse-elyth-addon/mcp_servers.json` と同じ枠組み、Phase 1' で作成）:

```json
{
  "mcpServers": {
    "stackchan": {
      "command": "stackchan-mcp",
      "args": [],
      "env": {
        "STACKCHAN_TOKEN": "${addon.saiverse-stackchan-addon.master_token}",
        "STACKCHAN_PCM_TOKEN": "${addon.saiverse-stackchan-addon.pcm_token}",
        "HOST": "0.0.0.0",
        "WS_PORT": "8765",
        "CAPTURE_PORT": "8766",
        "VISION_HOST": "${addon.saiverse-stackchan-addon.vision_host}"
      },
      "scope": "global",
      "timeout": 30,
      "spell_tools": [
        {"name": "move_head", "display_name": "首を動かす", "visible": true},
        {"name": "take_photo", "display_name": "写真を撮る", "visible": true},
        {"name": "set_avatar", "display_name": "表情を変える", "visible": true},
        {"name": "set_mouth", "display_name": "口形状を設定", "visible": true},
        {"name": "set_mouth_sequence", "display_name": "口パクシーケンス", "visible": true},
        {"name": "set_blink", "display_name": "まばたき制御", "visible": true},
        {"name": "set_led", "display_name": "LED を変える", "visible": true},
        {"name": "set_all_leds", "display_name": "全 LED を変える", "visible": true},
        {"name": "set_leds", "display_name": "複数 LED を変える", "visible": true},
        {"name": "clear_leds", "display_name": "LED 消灯", "visible": true},
        {"name": "set_brightness", "display_name": "画面輝度", "visible": true},
        {"name": "set_volume", "display_name": "音量設定", "visible": true},
        {"name": "get_touch_state", "display_name": "タッチ状態取得", "visible": true},
        {"name": "get_head_angles", "display_name": "首の角度取得", "visible": true},
        {"name": "get_device_info", "display_name": "デバイス情報", "visible": true},
        {"name": "get_status", "display_name": "接続状態", "visible": false},
        {"name": "say", "visible": false},
        {"name": "listen", "visible": false},
        {"name": "gpio_test", "visible": false},
        {"name": "uart_diag", "visible": false},
        {"name": "check_vm_en", "visible": false}
      ]
    }
  }
}
```

`say` と `listen` を `visible: false` にしている理由:

- `say`: voice-tts ベースの音声に置き換える（不変条件 #5 参照、HTTP PCM 経由）
- `listen`: 音声入力経路は Phase 3' で別途設計（v0.7 で Gemini inline 認識に転換、stackchan-mcp の `listen()` MCP tool は使用しない）

Phase 4' で Building 単位 visibility が実装されたら、`spell_tools` 各エントリに `"building_ids": ["<vessel_building_id>"]` を追加して Vessel Building 内でのみ visible にする。

### C. データフロー

#### C-1. TTS 出力経路

```
[ペルソナ発話]
  ↓ emit_speak / emit_say
  ↓ dispatch_hook("persona_speak", ...)
  ↓
[voice-tts] speak_hook.on_persona_speak()
  ↓ enqueue_tts(text, persona_id, message_id)
[voice-tts] _TTSWorker background thread
  ↓ engine.synthesize_stream() で各チャンク yield
  ↓ audio_stream.push_pcm_chunk(msg_id, pcm)  ← PCM 経路 (broadcast)
  ↓
[saiverse-stackchan-addon] speak_hook.on_persona_speak() 並行起動:
  ↓ 該当ペルソナが現在 Vessel Building 内かチェック
  ↓ vessel_manager.get_vessel_for_persona(persona_id, building_id)
  ↓   ── persona-specific bind を優先、なければ building-only bind
  ↓      (= bound_persona_id IS NULL) を fallback で返す。Vessel Building は
  ↓      capacity=1 なので「Building に居る persona = vessel の使い手」と
  ↓      自然に解決される
  ↓ vessel が居れば、audio_stream.subscribe_pcm(msg_id) で PCM Queue 取得
  ↓ ── 最初の chunk が来るまで HTTP POST を開始しない (= _wait_first_chunk)。
  ↓    voice-tts (GPT-SoVITS) は GPU model load 中だと first chunk 生成に
  ↓    10〜20 秒かかる。POST を先に開いて idle にすると device 側で
  ↓    「もう音が来ない」と判定されて speaking → listening に戻ってしまう
  ↓    (= Phase 1' 検証で観測: state 遷移 18 秒で listening、結果として
  ↓    発話の冒頭だけ届いて以降沈黙)。最初の chunk が来てから POST を開始
  ↓    すれば以降 voice-tts の連続生成 cadence (= realtime ≒ 64 KB/s) に
  ↓    乗って chunk が流れる
  ↓ ── 同 vessel に既 POST がある場合の調整 (= 同 pulse FIFO wait /
  ↓    別 pulse preempt) と voice-tts 側 chunk push の流量制御の詳細は
  ↓    voice_tts_playback_queue.md (= subscriber 全体の queue 設計) 参照。
  ↓ HTTP POST: http://127.0.0.1:8766/pcm
  ↓   Authorization: Bearer ${STACKCHAN_PCM_TOKEN}
  ↓   Content-Type: application/octet-stream
  ↓   X-Sample-Rate: 32000
  ↓   X-Channels: 1
  ↓   X-Message-Id: <msg_id>
  ↓   Transfer-Encoding: chunked
  ↓   body: 先取りした最初の chunk + 続きを Queue から逐次 yield して chunked 送信
  ↓
[stackchan-mcp gateway (subprocess)] HTTP PCM endpoint:
  ↓ Bearer token 検証
  ↓ body を chunked で読みながら AsyncIterator として保持
  ↓ send_pcm_stream(gateway, pcm_iterator, source_rate=32000) を呼ぶ
[stackchan-mcp gateway] send_pcm_stream (Phase 1 で実装済み):
  ↓ chunk ごとに resample (32 kHz → 16 kHz) + Opus encode (60ms フレーム)
  ↓ TTS lock 取得 → send_tts_state("start") → 50ms sleep
  ↓ 各 Opus フレームを 60ms 間隔で送信
  ↓ stream 終端で send_tts_state("stop")
  ↓ WebSocket binary frame で device に送信
[device]
  ↓ xiaozhi-esp32 audio_service が Opus decode → I2S スピーカー
```

#### C-2. 音声入力経路（v0.7 — Gemini inline 認識）

ｽﾀｯｸﾁｬﾝ device → gateway → SAIVerse addon → Gemini ペルソナの順で音声ファイルを届ける。stackchan-mcp gateway 内部 Whisper STT は経由しない（転換理由は § なぜ Gemini inline 認識経路に転換したか）。

```
[Stack-chan device] ウェイクワード検出 or LCD 画面短タップ (< 500ms)
  ↓ device: Application::ToggleChatState() → 録音開始
  ↓ device → gateway: PCM Opus フレーム送信
  ↓ device: listen.stop 要求 (発話区切り = 無音検知)
[stackchan-mcp gateway]
  ↓ 発話区切り後、Ogg/Opus コンテナ化
  ↓ 外部 hook POST (addon が起動時に hook URL 登録、認証 token 付き)
[saiverse-stackchan-addon] audio_input_relay.py:
  ↓ POST 受信、認証検証
  ↓ ~/.saiverse/audio/<timestamp>_<uuid>.ogg にファイル保存
  ↓ vessel_id → bound_building_id 解決
  ↓ manager.handle_user_input_stream(
       text=None,
       building_id=vessel.bound_building_id,
       metadata={
           "source": "stackchan_voice",
           "vessel_id": vessel.vessel_id,
           "media": [{"type": "audio", "uri": "saiverse://audio/...", "mime_type": "audio/ogg"}]
       }
     )
[SAIVerse 本体] 既存メディア経路（v1.0 ユーザー添付音声経路、2026-05-18 実装済み）に合流:
  ↓ metadata.media[] → iter_audio_media → Gemini inline_data として送信
[Gemini ペルソナ] 音を直接理解して返答
```

stackchan-mcp gateway 側に「STT せず Ogg/Opus を外部 hook に push するモード」を実装する PR が必要（= 新規 `listen_raw()` MCP tool か、既存 `listen()` に `transcribe: false` オプション追加）。Phase 3' 着手時に PR スケッチを上げて手元 fork で動作確認、検証後 upstream へ提出。

#### C-3. タッチ・カメラ・サーボ・LED

stackchan-mcp が提供する MCP tools を **SAIVerse 本体の MCP client が直接呼び出す**。`mcp_servers.json` に `command: "stackchan-mcp"` + `spell_tools` 設定を書けば、SAIVerse 起動時に subprocess 起動 + tool 自動登録される（Elyth と同じ枠組み）。

ペルソナの playbook 内では namespaced 名で呼び出し:

```
stackchan__move_head({"yaw": 30, "pitch": -5})
stackchan__take_photo({"question": "What do you see?"})
stackchan__set_avatar({"face": "happy"})
stackchan__set_led({"index": 0, "r": 255, "g": 0, "b": 0})
```

native wrap (= addon 側 `tools/stackchan_*.py`) は **作らない**。Vessel チェック / 切断ハンドリングは Phase 4' の本体改修 A-3-c (`spell_tools` の Building 単位 visibility) で対応する。それまでは「Vessel Building 外でもツールが見える / 呼べる」状態だが、Phase 4' で `building_ids` フィルタを足すことで物理身体ツールが Vessel Building 内でのみ visible になる。

タッチ知覚は別経路。`get_touch_state` MCP tool は polling 型なので、device からの push 通知（= "撫でられた瞬間に SAIVerse 側に通知") を扱うには gateway 側に push hook を追加する必要がある（Phase 5' 着手前に検証、必要なら upstream PR）。

#### C-4. 内蔵センサー（pull 型身体感覚）

内蔵 IMU の値は stackchan-mcp firmware の専用 tool → gateway の MCP relay → addon native wrapper の 3 層で取得する。firmware は内部 I2C bus handle を driver にだけ渡し、gateway / addon には sensor 値だけを返す。addon wrapper は K-4 の dispatcher を使って現在の Vessel に対応する gateway instance を選ぶ。

初期実装は単発の 9 軸読取に限定する。静止時の重力軸（約 ±1g）と、機体を上下反転した時の符号反転を実機検証の主判定にする。BMM150 は StackChan のサーボ磁石・周辺磁場の影響を受けるため、初期検証では有限値を取得できることを確認し、方位精度までは合否条件に含めない。

### D. gateway のライフサイクル

stackchan-mcp gateway は SAIVerse 本体の MCP client が subprocess として起動・停止を管理する（`tools/mcp_client.py` の `MCPServerConnection` 経由、`stdio_client(server_params)` で起動）。addon 側に gateway 管理層は **不要**:

- **起動**: SAIVerse 起動時、`tools/mcp_client.py` が `mcp_servers.json` を読んで stackchan の `command: "stackchan-mcp"` + `args` + `env` を実行
- **接続**: stdio で接続、`list_tools` で 21 ツールを取得、`spell_tools` 設定で 15 個を visible 化
- **死活監視**: 既存 MCP client の機構（接続失敗時の backoff、`_failed_instances` 管理）が機能する
- **停止**: SAIVerse shutdown 時に MCP client が subprocess を terminate

addon 側がやるべきこと:
- `mcp_servers.json` を addon ディレクトリに配置する（= 起動時に自動マウントされる）
- 環境変数 (`STACKCHAN_TOKEN` 等) は `env` の `${addon.X.Y}` placeholder で AddonConfig から resolve

**認証の二段階構造**:

- **gateway ↔ device 認証**: stackchan-mcp の `STACKCHAN_TOKEN`（WebSocket 接続時の Bearer）。Phase 1' は **single vessel 前提**で master token 1 個を配る。複数 vessel 対応は将来 PR (= stackchan-mcp 側で multi-token 検証を実装)
- **SAIVerse → gateway 認証 (HTTP PCM endpoint)**: 新規 `STACKCHAN_PCM_TOKEN`（`VISION_TOKEN` の前例に倣う、上流 PR で追加）。voice-tts → HTTP POST の Bearer に使う
- **addon 内の vessel 紐付け**: `vessels.db` の `token_hash → vessel_id → bound_building_id` 対応は addon 内で管理（不変条件 #7）。gateway の token 認証通過後、addon 側で device の `vessel_id` を解決

### E. 認証・ペアリングフロー

#### E-1. ペアリング操作

```
[初回セットアップ]
1. SAIVerse の AddonManager UI で「スタックチャンを追加」ボタン押下
   → SAIVerse が token (128bit ランダム) + 内部 vessel_id (UUID) を生成
   → vessels.db に「ペアリング待ち」レコード作成 (bound_building_id = NULL)
   → UI に以下を表示:
     - Wi-Fi SSID 入力欄 (ユーザーが自宅 SSID 入力)
     - Wi-Fi Password 入力欄 (ユーザーが自宅 Pass 入力)
     - Gateway URL: ws://<SAIVerse host>:8765/ (SAIVerse が自動表示)
     - Token: <生成済み値> (SAIVerse が自動表示)
   → 上記 4 値を QR コード化して表示
2. ユーザー: Stack-chan を AP モード起動 (初回 boot 時、または設定リセット時)
3. ユーザー: スマホ/PC で Stack-chan AP に接続 (SSID: "Xiaozhi-XXXX" 等)
4. ユーザー: ブラウザで 192.168.4.1 / captive portal 自動転送 → 設定 Web UI 開く
5. ユーザー: 4 値を入力 (QR 利用可なら QR スキャン)
6. Stack-chan: 値を NVS に保存 → 再起動
7. Stack-chan: Wi-Fi 接続 → gateway WS 接続 (Bearer 付き)
8. gateway: token を addon の vessel_manager に検証要求
9. vessel_manager: token hash で vessels.db 照合 → 該当レコードあれば bound_building_id 確定 (or UI で Building 選択要求)
10. ペアリング完了
```

#### E-2. ペアリング後の再接続

Wi-Fi 断 / device 再起動の場合: token は NVS に残っているので、device は自動的に gateway に Bearer 付きで再接続。vessel_manager が `last_seen_at` を更新するだけ。

ユーザーが Wi-Fi を変えた場合: AP モードに戻す手順が必要（= Stack-chan の物理ボタン / 画面タッチ長押し等で AP モードリセット、xiaozhi-esp32 のデフォルト動作で対応）。

#### E-3. 再ペアリング・登録解除

- 紛失・買い替え: AddonManager UI から既存 vessel を削除 → vessels.db レコード削除 → 新規ペアリングをやり直す
- 一時切断: vessel_id は残っているので自動再接続で復帰

### F. タッチ知覚（なでなで）

v0.4 から **基本維持**、経路の細部更新:

```
[device] Si12T タッチパネル検知 (= xiaozhi-esp32 + stackchan board の touch ドライバ)
  → device: gateway に touch event 通知 (要 PR 検証)
[gateway] touch_hook で SAIVerse 側に転送 (要実装)
[saiverse-stackchan-addon] touch_handler.py:
  ↓ manager.add_building_event(
        vessel.bound_building_id,
        {
            "role": "host",
            "content": '👋 まはーが Air の頭をなでた (約1.2秒)',
            "metadata": {
                "event": {
                    "type": "vessel_touch",
                    "zone": "head",
                    "duration_ms": 1200,
                    "vessel_id": vessel.vessel_id,
                }
            }
        },
        heard_by=[vessel.bound_persona_id]
    )
```

stackchan-mcp 側の touch event push 通知が現状ない場合は、`get_touch_state()` を polling で叩くか、stackchan-mcp gateway に push hook を追加する PR を出すか、検討。

### G. サーボ・カメラ・画面・LED（ツール経由）

stackchan-mcp の MCP tools は SAIVerse 本体の MCP client が直接呼び出す（native wrap は作らない）。`mcp_servers.json` に `command: "stackchan-mcp"` + `spell_tools` 配列を書けば、SAIVerse 起動時に subprocess + stdio で接続 + tool 自動登録が行われ、ペルソナの playbook 内で namespaced 名（`stackchan__<tool_name>`）で呼び出し可能になる。

`spell_tools` で visible = true にする 15 個（実 wrap 対象、診断系 / `say` / `listen` / `get_status` の 6 個は visible = false）:

| MCP tool 名 (= namespaced で `stackchan__<name>`) | 用途 | visible |
|---|---|---|
| `move_head` | サーボ pan/tilt | true |
| `take_photo` | カメラ撮影（gateway → HTTP capture endpoint で画像受信、結果は file path で返る） | true |
| `get_touch_state` | タッチ状態取得（polling 型） | true |
| `set_avatar` | アバター表情切替（idle/happy/thinking/sad/surprised/embarrassed/off） | true |
| `set_blink` | まばたき有効/無効 | true |
| `set_mouth` | 口形状（closed/half/open/e/u） | true |
| `set_mouth_sequence` | 口パクシーケンス（lip-sync 用） | true |
| `set_led` | 単一 LED 制御 | true |
| `set_all_leds` | 全 LED 一括 | true |
| `set_leds` | 複数 LED 個別 | true |
| `clear_leds` | LED 消灯 | true |
| `set_brightness` | 画面輝度 | true |
| `set_volume` | スピーカー音量 | true |
| `get_head_angles` | 首角度取得 | true |
| `get_device_info` | デバイス情報（バッテリー / 音量 / 輝度 / ネットワーク） | true |
| `say` | TTS 発話 | **false**（voice-tts に置き換え） |
| `listen` | STT | **false**（v0.7 で Gemini inline 認識に転換、本 tool は使用しない） |
| `get_status` | gateway 接続状態 | **false**（管理者向け） |
| `gpio_test` / `uart_diag` / `check_vm_en` | サーボ診断系 | **false**（管理者向け） |

カメラ撮影 (`take_photo`) の MediaBuffer 連携は、tool の返り値（画像 file path）を SAIVerse 側の `multimodal_input_pipeline` に流す薄いラッパー (= MCP tool 後処理 hook) で対応する。これは SAIVerse 本体側の MCP client への汎用機能拡張として、addon 側じゃなく本体側で受け取るのが自然。Phase 4' で扱う。

### H. Avatar 連動（Phase 5 で対応、口パクは Phase 6）

stackchan-mcp の `set_avatar` / `set_blink` / `set_mouth` で基本的な表情制御は可能。SAIVerse のペルソナ感情パラメータと連動させるマッピング層を `set_avatar` wrap 内に実装:

- 感情パラメータ（喜び・怒り・悲しみ等）→ stackchan-mcp が用意する Avatar 表情 ID にマッピング
- ペルソナ発話時の口パクは Phase 6（= TTS 音声のエンベロープから `set_mouth` を高頻度で叩く）

### I. ファームウェア・導入フロー

#### I-1. ファーム取得

stackchan-mcp の GitHub Releases から `merged-binary.bin`（または `xiaozhi.bin`）をダウンロード。SAIVerse の `saiverse stackchan download-firmware` サブコマンド（または UI ボタン）が、最新リリースタグを自動取得 → `~/.saiverse/addons/saiverse-stackchan-addon/firmware/` に保存。

#### I-2. ファーム書き込み

esptool を `uv tool install esptool` で global tool としてセットアップ済みの前提:

```bash
# SAIVerse の CLI / UI から実行
esptool --chip esp32s3 --port COM3 --baud 921600 --before usb-reset --after hard-reset write-flash 0x0 /path/to/merged-binary.bin
```

`merged-binary.bin` を `0x0` に書くと NVS リセット込みのクリーンインストール（既存設定全消去 → AP モード起動）。setup script は COM port 自動検出 + 書き込み実行 + 書き込み後の起動ログ確認まで一気にやる。

#### I-3. AP モード設定

書き込み後、device は AP モードで起動（SSID: `Xiaozhi-XXXX`）。ユーザーがスマホ/PC で接続 → captive portal で Web UI 開く → SAIVerse が表示した 4 値（SSID/Pass/Gateway URL/Token）を入力 → 保存 → 再起動 → gateway に接続。

#### I-4. SAIVerse 側 UI

AddonManager に Stack-chan アドオン専用パネル:

- 「スタックチャンを追加」ボタン → token 発行 + QR コード表示
- 接続済み vessel 一覧（接続状態、紐付け Building、ファーム version、最終接続時刻）
- 「ファームウェアを書き込む」ボタン → esptool 自動書き込み（プログレス UI 付き）
- 「ペアリング解除」ボタン → vessels.db からレコード削除

### J. archive 配下の旧資産

`archive/firmware/` に v0.4 までの自前ファーム実装一式（src/, dist/, platformio.ini）を保存。`archive/audio_stream_bridge.py.bak` に PCM 直送ブリッジ実装を保存。

`archive/README.md` で:

- archive した日付（2026-05-13）
- archive 理由（stackchan-mcp 採用への路線変更）
- 将来再利用する場合の前提（= stackchan-mcp が満たさない要件が出てきた時に出発点として参照）
- 旧 Phase 2-D まで動いていた知見（PCM 直送 + rotation buffer 4×32KB + 8 KB 分割 + identity-aware unregister）の要約

を記録。

### K. マルチ機体対応（v0.10）

複数の Stack-chan を同時に稼働させ、ペルソナがそれぞれ別の機体に降りられるようにする。設計の柱は 7 つ。**ファーム改修は不要**（device は接続時に `Device-Id`=MAC / `Client-Id`=UUID / per-device token を既に送り、gateway URL も NVS の captive portal 設定で機体ごとに変えられる。`websocket_protocol.cc` 425-430 行で確認済み。複数機体に必要な自己識別と接続先設定はファームに既存）。

#### K-1. 全体構成: 機体ごとに gateway インスタンス（A-2 方式）

採用方式は「**1 gateway = 1 device** を保ったまま、機体数ぶん gateway subprocess を別ポートで起動」。各 gateway は単一 device 前提の現状コードのまま無改修で、機体ごとに別ポート・別 token で listen する。

検討した代替（A-1）: gateway 1 個で N device を多重化する。これは upstream stackchan-mcp の根本改修（`ESP32Manager` の `self._connection` 単一スロット → `Dict[device_id]` 化、`_check_auth` の multi-token validation、MCP tool への宛先 device 引数追加）を要し、xiaozhi-esp32 の device セッション管理に踏み込む。A-2 なら本体の既存機構（1 server エントリ = 1 subprocess + tool 名前空間 + instance_key 管理）にそのまま乗るので upstream 非依存・自分の制御下で完結する。よって A-2 を採る。

副次効果として、将来余地 §6 に挙げていた「multi-token authentication の upstream PR」は不要化される（各 gateway が単一 token を持てば足りる）。

#### K-2. 本体 MCP client: 汎用「名前付きインスタンス」（別 intent に切り出し）

現状 `tools/mcp_client.py` の instance_key は `{server}:global` / `{server}:persona:{persona_id}` の 2 次元。ここに `{server}:instance:{instance_id}` 次元を足し、**同一 server 定義から名前付き N インスタンスを動的起動**できるよう一般化する。各インスタンスに per-instance の config context（token / port 等）を注入する（既存の `resolve_config_placeholders` が persona context で `${...}` を解決する仕組みを instance context に拡張）。

これは Stack-chan 固有ではなく、他 addon でも「同一 MCP server を設定違いで複数立てたい」需要に応える**汎用基盤**。詳細設計は `docs/intent/mcp_addon_integration.md` に切り出し、本書はそれを前提に使う（将来余地 §2「共通 device gateway」の実装フェーズ化）。

**静的手書き（`mcp_servers.json` に機体数ぶんエントリを書く）は不採用**。`vessels.db` を source に機体数ぶんのインスタンスを動的起動する。ペアリング追加で起動、削除で停止、アプリ起動時は全機体ぶん起動。「動的起動まで作らないとリリースしない」をリリース要件とする。

**gateway ライフサイクルは「常時接続」（ペルソナ在室に連動させない）**。ペアリング済みの各機体の gateway は SAIVerse 稼働中ずっと起動しておく。起動契機は 3 つ ——① 起動時 reconcile（全ペアリング機体）② ペアリング直後（`pair_vessel`）③ 入室時の冪等な保険（`vessel_gateways:on_persona_entered_building`）。停止はペアリング解除時（`delete_vessel`）のみ。**退室では止めない**。理由: (1) 機体設定（音量など `gateway_config`）はペルソナが降りていなくても機体管理 UI から触れて当然、(2) 入室のたびに subprocess 起動 + device 接続を待たされる体験を避ける、(3) 退室時に「gateway 停止」と avatar の「表情消し（`set_avatar`）」が同一イベントでレースし、停止済み gateway 宛の `set_avatar` が timeout → MCP client の auto-reconnect で **port を掴んだ孤児 subprocess** を生む退行を構造的に潰す（実装 `expansion_data/saiverse-stackchan-addon/vessel_gateways.py`。孤児防止の根ガードは `tools/mcp_client.py` の `MCPServerConnection._closed`＝意図的 stop 済みは call_tool の auto-reconnect を素通しで raise）。※ かつて一時的に「入室で起動・退室で停止」の lazy 実装だった時期があるが、本 K-2 の当初設計（全機体常時起動）に揃え直した。

#### K-3. ポート: ペアリング時に確定・vessels.db 永続

device は NVS の固定 URL（`ws://<lan-ip>:<port>`）に繋ぐため、ポートは安定していなければならない。**起動ごとに変わる動的割当は不可**（device が繋ぎ先を見失う）。

ペアリング時にシステムが空きポート（`ws_port` / `capture_port` のペア）を 1 つ選んで `vessels.db` に永続化し、以降その機体に固定する。captive portal はこのポートを含む URL をユーザーに提示し、device の NVS に焼かせる。gateway 起動時は `vessels.db` の永続値で listen。`lan_ip` は従来通り `${runtime.lan_ip}` で自動追従し、ポートだけ機体固定。

ここでの「動的」は「**ユーザーがポート番号を手で選ばない（システムが自動割当して永続化する）**」の意味であって、「起動ごとに変わる」ではない。後者は device の固定 URL と両立しない。

#### K-4. ツール: wrapper dispatcher で単一名前空間

**ペルソナに生 MCP ツールを露出させない**。全身体ツールを addon の native wrapper に集約し、ペルソナには機体に依らない単一論理名（`move_head` / `see` / `set_avatar` …）だけを見せる。wrapper は実行時に「現在ペルソナが居る Vessel Building → `vessels.db` で vessel を逆引き → その vessel の gateway インスタンス（`{server}:instance:{vessel_id}`）」を解決し、該当機体の生 MCP ツールに転送する。

ペルソナは機体を意識しない。リビングの機体に降りていればリビングの身体が、机の機体に降りていれば机の身体が動く。「Vessel Building = 身体」メタファーの一貫性を保つ。

現状 `move_head` / `see` / `body_status` は既にこの wrapper パターン（生 MCP は `visible:false`）。ただし宛先 server がハードコード（`{ADDON}__stackchan`）なので、現在 building からの実行時解決に置き換える。`set_avatar` / `set_led` / `set_mouth` / `set_brightness` / `set_volume` 等は現状 `visible:true` の生 MCP なので、同じ wrapper dispatcher に巻き取る。

生ツール露出をやめる判断は複数機体に依らず正しい: 後から挙動を変えたい・機能を足したいときに結局ラップが要るため、最初から wrapper に統一しておく。

#### K-5. capability カタログ: per-vessel、手動が基盤

> **更新 (2026-07-03)**: 「有効ユニット集合 + ハブ構成」は当初 bool capability + グローバル hub で実装したが、同アドレスユニットを別 channel に挿す要求（VL53L1X ×2）を受けて **per-vessel の「ユニット配置」（`unit_config` = ハブ + channel + label のリスト）** に発展させた。bool capability を包含し、hub 構成も per-vessel 化（K-5 当初意図への回帰）。可視性判定は配置リストに type が含まれるかで行う。詳細は `docs/intent/stackchan_unit_placement.md`。

機体ごとに刺さっている Port A ユニットが違う（ENV III を挿した機体・挿していない機体）。ユニット由来ツール（`env3` / `servo8` / `sonic` / `tof`）の使用可否は vessel ごとの capability に依存する（不変条件 #14）。

`vessels.db` に per-vessel の capability（有効ユニット集合 + ハブ構成）を持つ。ツール可視性は K-4 と同じ dispatcher で扱い、building_ids を 2 層で決める:

- 共通ツール: building_ids = 全 Vessel Building
- ユニット由来ツール: building_ids = そのユニットを capability に持つ vessel の building のみ

i2c の宛先も現在 vessel の gateway インスタンスに実行時解決する（現状 `env3.py` の `MCP_QUALIFIED_SERVER` ハードコードを置き換え）。現状の addon 単一 toggle（`unit_env3_enabled` が AddonConfig 全体で 1 個）を **per-vessel 設定に作り変える**。

capability の source は **手動が基盤、自動検出は後付けアシスト**:

- **手動（v0.10 / 必須）**: 機体管理 UI で機体ごとに搭載ユニットをチェック → `vessels.db` に保存
- **自動検出（Phase 8' / 後付け）**: 各 vessel の gateway で `i2c_scan` / `get_device_info` を叩いて capability 候補を推定し、ペアリング時の自動検出 +「自動検出」ボタンで手動設定欄を埋める。手動を上書きせず**提案で埋める**補助に留める

手動を先に確実な基盤として作る理由: 自動検出が誤検出 / 未検出したとき手動で確定できないと運用が詰む。自動検出は手動の上に乗る便利機能であり、順序を逆にしない。

#### K-6. 機体管理 UI

機体ごとの設定を一望・編集できる管理面。addon の `ui/` + `frontend/src/addon-panels/saiverse-stackchan-addon`（既存 addon UI パネル機構）に実装する。機体ごとに:

- 基本情報: vessel_id, hardware_model, firmware_version, paired_at, last_seen
- 接続: ws_port / capture_port（ペアリング時に自動割当・表示）、token 再発行
- バインド: bound_building_id, bound_persona_id
- capability: 搭載ユニット toggle（手動）、自動検出ボタン（Phase 8'）
- ペアリング / 削除

#### K-7. PCM 出力・音声入力の機体振り分け

- **TTS 出力（speak_hook）**: 現在 vessel を `get_vessel_for_persona` で解決し、その vessel の gateway の `/pcm` ポートに PCM POST する（単一 endpoint ハードコードをやめ、vessel → ポート解決を挟む）。
- **音声入力（audio-in hook）**: 複数 gateway が同じ addon hook を叩くので、どの vessel からの音声かを区別する。各 gateway の `STACKCHAN_AUDIO_HOOK_URL` に vessel 識別子を埋める、または token 逆引きで vessel → `bound_building_id` を解決して inject する。

## 設計判断の理由

### なぜ自前ファームを廃止して stackchan-mcp に乗り換えるか

本書「なぜ stackchan-mcp に乗り換えるか」節で整理した通り、判断軸は単純: 既に stackchan-mcp が実装済みの機能を自前で再実装する必要がなく、唯一の欠け（TTS）は Phase 2 で完成した経路の宛先切り替えで埋まる。これにより将来にわたって作業量が最短になる。

v0.4 まで自前ファームを進めていた経緯と、その作業が無駄になるのではないかという懸念について:

- Phase 1（ペアリング + WS）と Phase 2（音声出力）の自前実装は archive する。コードは消えないが active 開発はしない。
- Phase 2-D で得た知見（PCM 直送 + rotation buffer + 8 KB 分割 + identity-aware unregister）は、将来別の vessel addon が出てきた時の参照実装として残す。
- voice-tts の PCM broadcast 経路追加（`open_pcm_stream` / `push_pcm_chunk` / `subscribe_pcm`）は **乗り換え後も継続して使う**（= stackchan-mcp gateway の `send_pcm_stream` の入力になる）。これは voice-tts 自体の機能拡張として残るので、無駄にならない。
- voice-tts PR #3（subscribe-before-open + PCM broadcast）も乗り換え後に活きる。

### なぜ stackchan-mcp gateway を subprocess として起動するか

v0.5 初稿では「SAIVerse プロセス内で `import + start`」を採用していたが、再改訂で **subprocess（本体 MCP client による起動）** に変更した。

検討した 4 つの組み合わせ:

| 案 | ツール経路 | gateway 境界 | 結論 |
|---|---|---|---|
| (1α) | MCP client | subprocess | **採用** |
| (1β) | MCP client | in-process | MCP transport を stdio 以外で実現する必要、複雑 → 除外 |
| (2β) = 初稿 | native wrap | in-process | SAIVerse の MCP client 機構を活用しない、勿体ない → 撤回 |
| (2α) | native wrap | subprocess | voice-tts PCM 跨ぎ + MCP client 機構活用しないの両損 → 除外 |

(1α) 採用理由:

- **upstream の前提に合致**: stackchan-mcp は標準で「`python -m stackchan_mcp` 起動 + stdio MCP server + WS gateway」設計。subprocess + stdio で叩くのが想定された使い方
- **MCP transport が stdio で動かせる**: 既存 SAIVerse MCP client (`tools/mcp_client.py` の `stdio_client`) がそのまま使える、複雑な工夫不要
- **プロセス分離による安定性**: gateway クラッシュが SAIVerse 本体に波及しない
- **SAIVerse の既存 MCP integration 機構を活用**: subprocess 管理、`spell_tools` ベース可視性制御、env placeholder resolve、すべて既存資産

(1β) を選ばなかった理由:

- 同一プロセス内で stdio MCP server を起動すると、SAIVerse 自身の標準入出力と衝突する
- 代替 transport（in-process pipe / SSE 等）は MCP Python SDK の標準サポート外、自前実装が必要
- voice-tts PCM の in-process 渡しのために MCP transport を発明するのは本末転倒

(1α) の代償:

- voice-tts → gateway の PCM 経路は subprocess 境界を跨ぐ。stackchan-mcp 側に HTTP PCM 受入 endpoint を上流 PR で追加することで解決（不変条件 #5 参照、PR3 として投稿予定）
- subprocess 管理は既存 SAIVerse MCP client が担当、addon 側で自前管理する必要はない

### なぜ MCP tools を SAIVerse の MCP client 経路で呼び出すか

v0.5 初稿では「native tool として thin wrap する」を採用していたが、再改訂で **MCP client 経路（subprocess + stdio）** に変更した。

候補:

- 案 A: 本体 MCP client が stackchan-mcp の MCP server に stdio で接続、`spell_tools` ベースで visible 制御（採用）
- 案 B: native tool として thin wrap（初稿、撤回）

採用理由（A）:

- **SAIVerse の MCP client 機構が既に存在し、`spell_tools` で visible/display_name 制御が active**（Elyth addon で実機動作中、`tools/mcp_client.py:_normalize_spell_config` + `_tool_schema_from_mcp`）
- addon 側のコード量がゼロ近く（= `mcp_servers.json` を書くだけ、native wrap 13 個分のファイル不要）
- 将来 stackchan-mcp 側で MCP tool が追加されても `mcp_servers.json` の `spell_tools` に行を足すだけで対応可能、native wrap みたいに対応コードを書く必要がない
- 「addon 個別の事情で本体に改修入れない、汎用機能として本体 MCP client を改修する」原則と整合（Phase 4' の A-3-c は本体 MCP client の Building 単位 visibility 拡張、これは他 addon でも使える）

案 B を撤回した理由:

- SAIVerse の MCP client 機構を活用しない設計は、既存資産の重複実装を生む
- 「将来 Resources / Sampling 等への拡張余地」を理由に native wrap を選んだのは、現状の Tools 機能だけで stackchan-mcp の利用要件をカバーできるという事実と整合しなかった
- `BuildingToolLink` を Vessel Building に紐付ける前提だったが、`BuildingToolLink` は数ヶ月触られておらず active かどうか不明（実態として機能してない可能性）。代わりに `spell_tools` の Building 単位拡張で対応する

### なぜ Bearer Token ベースの認証にするか（vessel_id を hello で送る形にしない）

stackchan-mcp プロトコルが既に Bearer Token ベース。これを変える PR を上流に出すのは:

- 上流の認証モデルを変更する大きな変更
- xiaozhi-esp32 全体のエコシステムと互換性問題を引き起こす可能性

一方、SAIVerse 側で `token_hash → vessel_id → bound_building_id` のテーブルを持つのはコストが軽い（= 我々のアドオン内に SQLite テーブル 1 つ追加）。プロトコルレベルでの vessel_id 概念がなくても、認証層の対応テーブルで論理的な vessel_id を維持できる。

### なぜ Gemini inline 認識経路に転換したか（v0.7）

v0.6 までは「stackchan-mcp gateway 内部 Whisper で STT → テキストを SAIVerse に返す」設計だった。v0.7 で以下の理由により Gemini inline 認識（= 音声ファイルそのものを Gemini ペルソナの `inline_data` として渡す）に転換:

1. **文脈なし STT の精度限界**: ペルソナ名・ユーザー名・SAIVerse 固有用語など、Whisper 学習データに含まれない固有名詞が高頻度で誤認識される（= 別案件ナチュレの実証で足踏みした既知パターン）。
2. **Gemini の音声理解能力**: 1 秒 = 32 token、9.5 時間まで対応。SAIVerse の対話シナリオでは精度・コスト・遅延すべての面で gateway 側 Whisper を上回る。
3. **既存基盤の流用**: ユーザー添付音声経路（v1.0, 2026-05-18 実装済み）で `/upload-audio` + `metadata.media[]` + Gemini `inline_data` の出口が既に揃っており、入口側（= gateway → SAIVerse の音声ファイル push）を追加すれば足りる。
4. **責務の分離**: gateway 側に STT エンジン（Whisper）を持たせる必要がなくなり、device + gateway はマイク信号の集約と Opus 圧縮配信に専念できる。

非Gemini モデル（Claude / OpenAI 等）を Vessel ペルソナのデフォルトにしたい場合の書き起こし経路は将来 Phase で別途設計する。既存の `ensure_audio_summary`（ファイル単位キャッシュ、コンテキスト非依存）には触らず、ペルソナ単位のコンテキスト付き書き起こし経路を独立して立てる方針。

### なぜ Web Serial フラッシュにこだわらないか（v0.4 から変更）

v0.4 では Web Serial（esptool-js）でブラウザ完結を狙っていたが、stackchan-mcp 採用では:

- SAIVerse の CLI / UI が esptool を `uv tool install` で global tool として持つ
- COM port 自動検出 + 書き込み + ログ確認まで CLI で一気通貫
- ユーザーは「`saiverse stackchan flash`」一発で書き込み完了

これは Web Serial と同等以上の UX（= ブラウザ立ち上げる必要なし）であり、esptool-js のメンテナンス不要。SAIVerse の CLI ラッパで十分。

### なぜ自前 WebSocket gateway を廃止するか

stackchan-mcp の gateway が既に WS server を持つ。自前で同等品を持つ意味がない。我々のコード上の責務は:

- vessel_manager（token ↔ vessel/building 対応）
- speak_hook（voice-tts → gateway 流通）
- native tool wrap

それ以外（WS handshake, session 管理, audio routing, MCP routing, OTA, etc）は全部 gateway 側が担当する。

## スコープ

Phase の番号は v0.4 から **再定義**。v0.4 までの Phase 1〜2-D は完了済み（archive 扱い）として、v0.5 以降を Phase 1' から振り直す。

### Phase 1' — gateway 起動と PCM 経路確立（v0.5 着手、**2026-05-13 完了**）

1. ✅ **手元 fork で PR を実装** (= 計 9 commit / 7 PR 相当、Phase 1' 中に当初想定の 3 PR を超えて 7 PR まで膨らんだ):
   - PR1〜PR3: `send_pcm_audio` / `send_pcm_stream` / `POST /pcm` endpoint (= 想定通り)
   - **PR4** (= Phase 1' 中に新規発見): xiaozhi-cloud OTA `CheckVersion()` 撤去 — NVS websocket.url が boot 時に server から上書きされる副作用を遮断
   - **PR5** (= Phase 1' 中に新規発見): Windows 用 `libopus.dll` を gateway wheel に同梱 + `os.add_dll_directory()` + `PATH` prepend (= `ctypes.util.find_library` 経路救済)
   - **PR6** (= Phase 1' 中に新規発見、 **2026-05-20 取り下げ**): `intentional_close_` flag bug fix + 起動時 OpenAudioChannel + 失敗時 ScheduleReconnect (= NVS flag `websocket.persistent` opt-in)。「voice session 中だけ繋ぐ」設計を「server-driven push を許可する」モードに拡張。 → upstream PR #169 / #197 で transport-level persistent connect + auto-reconnect + sleep policy が正式実装されたため fork 暫定実装は不要に。 `dev/integration` `1d3179f` (upstream merge) + 続く dead code 削除 commit で fork 側の NVS flag / UI checkbox / gate logic を撤去。 `application.cc::ActivationTask` での `OpenAudioChannel()` 呼び出しのみ残置 (= audio channel の boot 時 open は依然 fork 必要、 ただし opt-in 無しで常時実行)
   - **PR7** (= Phase 1' 中に新規発見): `client_max_size=0` で aiohttp の 1 MiB body cap を撤去 (= 長時間 chunked transfer が途中で切られる現象を解消)
   - PR 投稿戦略の整理は [`docs/issues/stackchan_mcp_upstream_pr_strategy.md`](../issues/stackchan_mcp_upstream_pr_strategy.md) 参照、投稿自体は Phase X' で
2. ✅ **アドオン**: `mcp_servers.json` 作成 (= Elyth と同じ枠組み、`command: "uvx"` で fork branch を指定、`env` で AddonConfig placeholder を解決、`spell_tools` 配列で 15 個 visible / 6 個 visible=false)
   - 補助: `--with opuslib` を args に追加し、`uvx` が `[tts]` extra を取り損ねるパターンでも opuslib を確実に install
3. ✅ **アドオン**: `speak_hook.py` で persona_speak fired → `voice-tts.subscribe_pcm` → **最初の chunk を queue で待ってから** HTTP POST (chunked) で stackchan-mcp gateway の `/pcm` endpoint に送信
4. ✅ **アドオン**: `vessel_manager.py` で `vessels.db` の token-based テーブル管理。`get_vessel_for_persona` は persona-specific bind を優先しつつ、`bound_persona_id IS NULL` の building-only bind を fallback で返す (Vessel Building capacity=1 セマンティクスに整合)
5. ✅ **検証**: ファーム書き込み → AP 設定 (常時接続モード ON) → device 接続 → voice-tts 発話 → device スピーカーから完走 (= 100 秒・200 秒の長文を 2 回連続 status 200 OK で完走、GPU load 中 / load 後の両条件で確認)

**Phase 1' 中に未解明のまま残った観測 (= 別 issue 化候補)**:

- 一度だけ「発話の末尾 10 秒程度が取りこぼされる」現象を観測 (= 2026-05-13 20:25 頃の発話)。後続の 2 連続発話 (GPU load 中 / load 後) では再現せず完走したため「偶発」評価。再発時に分析できるよう、speak_hook に投入 cadence の DEBUG ログを残してある (`iter yield #N` / `None sentinel received` / `POST returned status=N`)

### Phase 2' — 認証・ペアリング UX（**2026-05-19 完了**）

6. ✅ **アドオン**: `api_routes.py` でペアリング HTTP API（`POST /pair` / `GET /vessels` / `DELETE /vessels/{id}` + ペアリング時に AddonConfig.master_token / vessel_building_id 自動更新）
7. ✅ **アドオン**: AddonManager UI にパネル追加（`VesselPairingSection` = Building dropdown + 「スタックチャンを追加」 + token + Gateway URL 表示 + 「解除」 + connected status）
8. ⏭ **QR コード生成 + 表示** — 省略（device 側に QR スキャナがない、 Phase 6 以降の候補。 当面はテキスト + コピーボタンで代替）
9. ✅ **UI 経由 esptool flash** — 当初計画の `saiverse stackchan flash` CLI から **UI 経由 SSE 進捗 stream** に変更。 `POST /flash/erase-nvs` (NVS partition のみ erase = AP モード復帰) と `POST /flash/firmware` (merged-binary.bin 書き込み) + `GET /flash/ports` (COM port 自動検出、 ESP32-S3 VID 303A filter) + `GET /flash/firmware-info` (使用 path + mtime + size + source 分類)。 esptool は `shutil.which("esptool")` → fallback で `uvx esptool` (= uv は addon 前提として既に install 済み)
10. ✅ **検証**: ペアリング解除 → NVS リセット → device の AP モード復帰 → captive portal で再設定 → 再ペアリング → device 自動再接続まで UI 内完結 (実機検証完了、 2026-05-19)

**Phase 2' 中に発見した汎用拡張** (= 本体側 / addon 横断で得た改修):

- **本体 `tools/mcp_client.py` の `reconnect_server` 改修**: ペアリング操作で AddonConfig を内部更新しても、 既起動の MCP subprocess は古い env で動き続ける問題を解消。 `reconnect_server` 内で **source `mcp_servers.json` を再 load → `_interpolate_value` で AddonConfig 最新値で再 interpolate → `_server_meta["raw_config"]` を上書き → connection.config を resolve → disconnect/connect** の流れを実装。 注意: `_server_meta["raw_config"]` は起動時に解決済みなので、 そのまま再 resolve すると no-op (= source JSON 再 load が必須)。 他 addon が AddonConfig を rotate するシナリオ全般で活きる
- **本体 `AddonManagerModal.tsx` + `sync-addon-panels.mjs` の `AddonPanelProps.onConfigChanged` 追加**: addon panel が内部的に AddonConfig を書き換えた時、 親 (AddonManagerModal) に通知して `/api/addon/` を再 fetch → `ParamsSection` の表示を最新値で再描画する経路。 `ParamsSection` 側にも `useEffect([addon.params])` で globalParams state の追従を追加。 他 addon (OAuth token 自動更新等) でも汎用に使える
- **アドオン `api_routes.py` の `_firmware_resolve_path()` 3 段階 fallback**: AddonConfig.firmware_path → `<repo>/temp/stackchan-mcp/firmware/build/merged-binary.bin` (= 開発者ローカルビルド) → `~/.saiverse/addons/saiverse-stackchan-addon/firmware/merged-binary.bin` (= 一般ユーザー DL 配置)。 GPL-3.0 firmware を addon に複製しない方針 (= §不変条件 8) と整合
- **アドオン `PairResponse.gateway_ws_url`**: device に提示する Gateway URL を backend で AddonConfig (`vision_host` + `gateway_ws_port`) から組み立てて返す。 UI は表示するだけ (= port hardcode の不整合を回避)

**解決済み 引き継ぎ事項** (Phase 1'/Phase 4 からの繰越):

- ✅ **vessels.db と AddonConfig.vessel_building_id の二重管理**: Phase 2' のペアリング API (`POST /pair`) ハンドラ内で `_update_addon_config_after_pair()` を呼び、 `vessel_manager.create_pairing()` の直後に AddonConfig.master_token / vessel_building_id を自動書き換え。 二重管理は維持されるが、 UI 操作時の自動同期で実用上は単一情報源として機能する。 placeholder 解決層 (= `tools/mcp_config.py`) への改修は不要だった
- 暫定対応として書いた「ペアリング完了前は AddonConfig.vessel_building_id にダミー値」 の UI 注意書きも不要に

### Phase 3' — 音声入力経路（Gemini inline 認識）

ｽﾀｯｸﾁｬﾝ device の発話区切り後の Opus 音声を SAIVerse へ届けて、Gemini ペルソナが `inline_data` で直接理解して返答する経路。詳細は § C-2 参照、転換理由は § なぜ Gemini inline 認識経路に転換したか。stackchan-mcp gateway 内部 Whisper は経由しない。

11. **gateway 側 PR**: stackchan-mcp に「STT せず Ogg/Opus を外部 hook に push するモード」を追加（= 新規 `listen_raw()` MCP tool か、既存 `listen()` に `transcribe: false` オプション追加）。手元 fork で動作確認後 upstream PR 投稿。
12. **アドオン**: `audio_input_relay.py` 新規 — gateway からの音声 POST 受信、`~/.saiverse/audio/` にファイル保存、`vessel_id` → `bound_building_id` 解決、`manager.handle_user_input_stream` に `metadata.media[]` 付きで注入。
13. **検証**: LCD 画面短タップ（< 500ms）で listen 起動 → 発話 → Gemini ペルソナが音声内容を理解して返答。固有名詞（まはー / エア等）が正しく認識されることを確認。ウェイクワード（"你好小智" 等 sdkconfig で有効なもの）でも同等の起動が可能。

非Gemini ペルソナ向け書き起こし経路は将来 Phase で別途設計（§「将来 Phase（範囲外）」参照）。

### Phase 4' — 本体改修（visibility 制御）+ tool 一覧の最終確定

**Phase 4' で扱うこと**:

- ツール定義の追加は **Phase 1' で `mcp_servers.json` を書いた時点で完了**（spell_tools 配列で 15 個 visible / 6 個 visible=false）
- Phase 4' は **本体改修 (A-3-a/b/c)** が主スコープ、加えて実機検証中に新規発見されたツール（or 不要と判明したツール）の `mcp_servers.json` 調整

**ツール選別** (現時点候補、実機検証中に最終確定):

stackchan-mcp の 21 ツールから:

- **visible (15 個)**: move_head, take_photo, set_avatar, set_mouth, set_mouth_sequence, set_blink, set_led, set_all_leds, set_leds, clear_leds, set_brightness, set_volume, get_touch_state, get_head_angles, get_device_info
- **visible=false (6 個)**: get_status（管理者向け）, say（voice-tts に置き換え）, listen（v0.7 で Gemini inline 認識に転換、使用しない）, gpio_test / uart_diag / check_vm_en（診断系、管理者向け）
- **継続洗い出し**: SAIVerse の認知モデル上有用な追加ツールがあるか実機検証で確認、必要なら `mcp_servers.json` の `spell_tools` を追加・調整

**本体改修ステップ**:

14. **A-3-a**: `OccupancyManager.move_entity` の `enter_metadata` に `building_info` 追加 + `auto_ingest` 側の受け取り経路
15. **A-3-b**: Vessel Building 用 `SYSTEM_PROMPT` テンプレート整備 + addon setup での初期流し込み
16. **A-3-c**: SAIVerse 本体 MCP client (`tools/mcp_client.py`) に Building 単位 visibility 拡張
    - `spell_tools` schema に `building_ids: ["<vessel_building_id>"]` フィールド追加（オプション、未指定なら全 Building で visible）
    - `is_tool_available_for_persona` → `is_tool_available_for_persona_and_building` 等に拡張、`building_ids` フィルタを適用
    - playbook 内 tool 一覧生成箇所で `building_id` を渡す経路整備
17. **アドオン**: `mcp_servers.json` の `spell_tools` 各エントリに `building_ids: ["<vessel_building_id>"]` を追加して Vessel Building 内のみ visible に
18. **アドオン**: `multimodal_input_pipeline` 連携 (= `take_photo` の返り値 file path を MediaBuffer に流す薄いラッパー、本体側 MCP client への汎用機能拡張として実装)
19. **検証**: Vessel Building 内でペルソナがツール呼び出し成立、Vessel Building 外ではツールが visible にならない、移動時に Building 情報が `enter_metadata` 経由でペルソナに届く

### Phase 5' — タッチ知覚

17. **gateway 側調査**: touch event push hook の有無確認、なければ PR
18. **アドオン**: `touch_handler.py` で `add_building_event` 注入
19. **検証**: 頭を撫でるとペルソナが反応、Building history に表示

### Phase 6' — Avatar 連動

20. **アドオン**: 感情パラメータ → `set_avatar` マッピング
21. **アドオン**: TTS エンベロープ → `set_mouth` 高頻度駆動
22. **検証**: 発話中に口が動く、感情に応じて表情変化

### Phase 7' — 複数機体の同時稼働（v0.10、リリース要件）

A-2 方式（機体ごとに gateway インスタンス）+ wrapper dispatcher + 手動 capability + 機体管理 UI まで。**動的起動・手動 capability・機体管理 UI が揃わなければリリースしない**（静的手書きは不採用）。

- **本体（別 intent `mcp_addon_integration.md`）**: instance_key に `:instance:{id}` 次元追加、per-instance config context 注入、`vessels.db` 駆動の動的 register / start / stop（K-2）
- **vessels.db スキーマ拡張**: `ws_port` / `capture_port` / capability（搭載ユニット集合 + ハブ構成）カラム追加。ポートはペアリング時に空きを確定して永続（K-3）
- **アドオン: wrapper dispatcher**: `move_head` / `see` / `body_status` の宛先 server ハードコードを「現在 building → vessel → instance」解決に置換。`set_avatar` / `set_led` / `set_mouth` / `set_brightness` / `set_volume` 等の生 MCP も wrapper に巻き取り、ペルソナへの生ツール露出をゼロにする（K-4）
- **アドオン: capability カタログ**: `unit_env3_enabled` 等の addon 単一 toggle を per-vessel に作り変え、ユニット由来ツールの building_ids を capability から生成。`env3.py` 等の i2c 宛先を現在 vessel の instance に解決（K-5）
- **アドオン: 機体管理 UI**: 機体ごとの接続 / バインド / capability / ペアリング / 削除（K-6）
- **アドオン: PCM / audio-in 振り分け**: speak_hook の `/pcm` POST 先を vessel 解決、audio-in hook で発信元 vessel を区別（K-7）
- **検証**: 2 機体（片方 ENV III あり / 片方なし）を同時接続し、(a) 別ペルソナが各機体に降りて同時に首振り・発話、(b) ENV III なしの機体に降りたペルソナに温湿度スペルが見えない、(c) 機体を持ち替えると同じ `move_head` で別機体が動く、を実機確認

### Phase 8' — capability 自動検出（後付けアシスト）

手動 capability（Phase 7'）の上に乗せる補助機能。

- **アドオン**: 各 vessel の gateway で `i2c_scan` / `get_device_info` を叩いて搭載ユニット候補を推定
- **UI**: ペアリング時の自動検出 + 機体管理 UI の「自動検出」ボタンで手動設定欄を**提案で埋める**（手動を上書きしない）
- **検証**: ENV III / PaHub 構成を自動検出が正しく拾い、誤検出をユーザーが手動で弾けること

### Phase X' — 上流 PR 投稿（Phase 1'〜6' の実機検証後）

stackchan-mcp 本家へ **計 7 PR** を投稿。Phase 1' 中に当初想定の 3 PR を超えて拡張された (= 検証中に発見した必須修正 4 件 = PR4-PR7 を追加)。**ブランチ分割の具体手順 + 依存グラフ + 各 PR の注意点は [`docs/issues/stackchan_mcp_upstream_pr_strategy.md`](../issues/stackchan_mcp_upstream_pr_strategy.md) を参照**。

サマリ (= 詳細はハンドオフ doc に書いた):

- **Series A (= 4 PR、Stacked)**: PR #A1 `send_pcm_audio` 抽出 → PR #A2 `send_pcm_stream` → PR #A3 `POST /pcm` endpoint → PR #A4 `client_max_size=0`
- **PR #B (= 独立)**: xiaozhi-cloud OTA `CheckVersion()` 撤去
- **PR #C (= 独立)**: Windows 用 `libopus.dll` 同梱 + DLL search path 設定
- **PR #D (= 独立)**: opt-in persistent WS connection + `intentional_close_` bug fix

Phase X' 着手の前提:

- Phase 1'〜6' で手元 fork の動作妥当性を実機検証してから提出 (= PR 投げて実際には使いませんでした、になるのを避ける)
- 各 PR の review-cycle に 1-2 週間想定、全体 1-3 ヶ月程度を見込む
- maintainer (kisaragi-mochi) の判断で受け入れ拒否された PR は手元 fork で運用継続 (= memory `feedback_user_experience_first.md` の精神に整合)

23. **Series A 投稿** (= PR #A1〜A4、Stacked 順次)
24. **PR #B 投稿** (= xiaozhi OTA 切離し、独立)
25. **PR #C 投稿** (= libopus bundle、独立。CI build pipeline 整備も併走で提案推奨)
26. **PR #D 投稿** (= persistent WS opt-in + bug fix、独立)
27. **(Phase 3' で必須)** Ogg/Opus 音声ファイルの external push hook（= v0.7 で Gemini inline 認識経路に転換、gateway 側で「STT せず音声を push するモード」が必要。新規 `listen_raw()` MCP tool か `listen()` の `transcribe: false` オプション追加）
28. **(必要なら)** touch event push hook (Phase 5' で polling じゃ要件満たさない場合)

### 将来 Phase（範囲外）

- 非Gemini ペルソナ向け書き起こし経路（= Vessel Building に Claude / OpenAI ペルソナを置きたい需要が出たら、ペルソナの会話文脈を含めた `persona_audio_transcribe` 経路を別 Intent Doc で設計。既存 `ensure_audio_summary` キャッシュ経路は touch しない）
- カスタムウェイクワード（= Espressif の Wake Word Customization 経由で "エア" 等ペルソナ名トリガーのモデル発注）
- 歩行（外付け車輪モジュール対応）
- IMU 連動
- ~~複数 Vessel 対応の本格化~~ → **v0.10 で範囲内化**（Phase 7'、設計 K 参照）。`Vessel` テーブルの本格切り出し（`Building.PHYSICAL_VESSEL_ID` の FK 化）は引き続き将来余地だが、複数機体の同時稼働そのものは v0.10 で実装する
- 別機種対応（眼鏡型、別ロボット）
- 物理 Vessel SDK 共通基盤化
- Stack-chan シリアルログを SAIVerse logs/ に統合
- xiaozhi-esp32 側に QR スキャン機能追加 PR（device 単独で AP 設定を完結させる UX）

## 検証観点

実機検証で必ず通すケース:

### Phase 1' (gateway 起動 + PCM 経路)

- SAIVerse 起動時に本体 MCP client が `mcp_servers.json` を読んで stackchan-mcp gateway を subprocess 起動、gateway が WS port 8765 + HTTP capture port 8766 (PCM endpoint 含む) を listen 開始
- speak_hook が voice-tts.subscribe_pcm から PCM 取得 → HTTP POST (chunked transfer) で gateway の /pcm endpoint に送信、`STACKCHAN_PCM_TOKEN` 認証通過
- voice-tts で発話 → Vessel Building 内なら物理スピーカーから声が出る、文章として連続している（途切れなし）
- 同じペルソナが Vessel Building から出る → 以降の発話は物理スピーカーから出ない
- ペルソナ A が Vessel Building にいる時に、別 Building のペルソナ B が発話 → B の音声は鳴らない
- 長尺発話の連続実行（10 回以上）で gateway 側にメモリリーク・session ゾンビ・接続切れがない

### Phase 2' (ペアリング)

- 新品 Stack-chan → 書き込み → AP 設定 → WS 接続 → vessel record 紐付け → Vessel Building 紐付け完了まで 30 分以内
- Wi-Fi 断 → 復帰 → 自動再接続（同じ vessel record と紐付け）
- ペアリング解除 → vessels.db からレコード削除 → device 再接続 → "vessel not registered" エラー

### Phase 3' (音声入力 — Gemini inline 認識)

- **タッチ起動**: LCD 画面短タップ（< 500ms）→ listen 開始 → 発話 → Gemini ペルソナが音声内容を理解して返答（= ウェイクワード発音不要、実機テストの基本経路）
- **ウェイクワード起動**: sdkconfig で有効なウェイクワード（現状デフォルトは "你好小智"）→ listen 開始 → 発話 → ペルソナが応答（= 発音できる場合の確認用、optional）
- **固有名詞認識**: ｽﾀｯｸﾁｬﾝ経由で「まはー」「エア」等の固有名詞を含む発話 → ペルソナが正しく解釈して返答（= Gemini inline 認識のキー価値検証、Whisper STT では落ちていたケース）
- **Vessel に誰も居ない時**: 音声受信しても何も起きない（= addon 側で occupant 不在を検出して破棄、ファイル保存しない）
- **非Gemini ペルソナが Vessel にいる場合**: 警告ログ出力、ペルソナには無音扱い（将来 Phase で対応予定）
- **連続発話**: 短い間隔で連続発話（= 5 回程度）で gateway / addon にメモリリーク・session ゾンビ・接続切れがない

### Phase 4' (ネイティブツール)

- ペルソナが「右を見て」と言って自分で首を振る
- ペルソナが「部屋を見せて」とカメラ撮影 → 撮影画像が次の発話で参照される
- ペルソナが LED を「赤」に変える
- ペルソナが Vessel Building **外** にいる時、物理身体ツール（move_head 等）が **ツール一覧に見えない**
- ペルソナが Vessel Building **内** に入った直後、新 Building の情報（system_prompt / 使えるツール一覧）を認識して反応に反映される（= 移動メッセージの `building_info` が auto_ingest 経由でペルソナの context に届いている）
- ペルソナが Vessel Building から退出した直後、物理身体ツールがツール一覧から消える（= スペル / Playbook 切り替えが機能している）

### Phase 5' (タッチ)

- 頭を撫でる → Building history に host メッセージ → ペルソナが認識

### Phase 6' (Avatar)

- 発話中に口が動く
- 感情変化に応じて表情が変わる

### 全 Phase 共通

- ファームウェア書き込みが SAIVerse の CLI / UI 経由で完結する（手動 esptool 不要）
- USB ドライバの手動インストールが不要（OS 標準で USB-CDC 認識）
- 本体改修は Phase 1' 着手時点では `Building.PHYSICAL_VESSEL_ID` カラム追加のみ（v0.4 で済み）。Phase 4' で追加改修（移動メッセージ拡張 + スペル/Playbook の Building 単位切り替え）が入る。Stack-chan 固有ロジックを本体に持ち込まないという原則は維持（追加改修は汎用機能として正当化される範囲）

## 将来 / Vessel 共通仕様への展開余地

v0.4 から **基本維持**、stackchan-mcp 採用を踏まえて若干更新。**§1〜§3 は v0.10（Phase 7'/8'、設計 K）で実装フェーズに引き上げた**。当初「将来余地」として置いていた区画が、複数機体対応の骨格として地続きに繋がった形。

### 1. `Vessel` テーブルの切り出し（v0.10 で部分実装）

固有メタデータ（型番、ファーム version、能力一覧、ws_port / capture_port）の集約は v0.10 の `vessels.db` 拡張で実装する（設計 K-3 / K-5）。ただし `Building.PHYSICAL_VESSEL_ID` を `FK → Vessel.vessel_id` に変更する DB 正規化は v0.10 では行わず、addon 側 `vessels.db` の `bound_building_id` で紐付けを保つ。本格的な FK 化は引き続き将来余地。

### 2. 共通 device gateway（v0.10 で汎用基盤化）

stackchan-mcp を `mcp_servers.json` で subprocess 起動するパターンは維持しつつ、v0.10 で本体 MCP client に「同一 server 定義から名前付き N インスタンスを動的起動」する汎用機構を足す（設計 K-2、詳細は `mcp_addon_integration.md`）。「stackchan-mcp 以外の vessel」が出てきた時も、この名前付きインスタンス基盤に乗せられる。

### 3. Vessel 能力宣言（capabilities）（v0.10 で実装）

per-vessel capability を `vessels.db` に持ち、ツール可視性（共通 / ユニット由来の 2 層）と i2c 宛先解決に使う（設計 K-5、不変条件 #14）。source は手動が基盤（Phase 7'）、`get_device_info()` / `i2c_scan` による自動検出は後付けアシスト（Phase 8'）。

### 4. 共通の感情 → 表情マッピング

stackchan-mcp の `set_avatar` を基盤にしつつ、別機種が出てきた時に共通の感情パラメータレイヤを切り出す。

### 5. voice-tts の PCM broadcast 経路（v0.4 で実装完了、v0.5 でも継続利用）

`open_pcm_stream` / `push_pcm_chunk` / `subscribe_pcm` は v0.4 で実装。v0.5 では `subscribe_pcm` の出力を addon の speak_hook が取り出し、HTTP POST (chunked transfer) で stackchan-mcp gateway の `/pcm` endpoint に送る形で継続利用。voice-tts 自体の機能は変わらず、流通先が `audio_stream_bridge` → `HTTP POST → gateway` に変わるだけ。

### 6. stackchan-mcp upstream PR 群

我々が出した PR が merge されれば本家の機能になる:

- PR1: `send_pcm_audio(gateway, pcm)` 切り出し（手元 fork で動作確認済み、2026-05-13）
- PR2: `send_pcm_stream(gateway, pcm_chunks)` 追加（手元 fork で動作確認済み、2026-05-13）
- 将来 PR: gateway を stdio MCP server 抜きで起動するオプション（必要なら）
- Phase 3' で必須 PR: Ogg/Opus 音声ファイルの external push hook（= v0.7 で Gemini inline 認識経路に転換、新規 `listen_raw()` MCP tool か `listen()` の `transcribe: false` オプション追加）
- 将来 PR: touch event の external push hook（必要なら）
- 将来 PR: multi-token authentication（必要なら）

## 関連ドキュメント

- `docs/intent/addon_extension_points.md` — アドオン拡張点（OAuth、Integration、Addon Storage）、本書の基盤
- `docs/intent/mcp_addon_integration.md` — MCP × Addon 統合、`${persona.addon.x.y}` 参照構文
- `docs/intent/multimodal_input_pipeline.md` — MediaBuffer / disposition、カメラ画像の経路
- `docs/intent/addon_speak_hooks.md` — `persona_speak` server_hook、TTS の購読パターン
- `docs/intent/external_event_integration.md` — 外部イベント注入の汎用基盤（本書では採用しなかったが、将来参考）
- `docs/intent/persona_cognitive_model.md` — Track / Note、ペルソナの認知モデル
- `expansion_data/saiverse-voice-tts/ARCHITECTURE.md` — voice-tts の `audio_stream` pub/sub 詳細
- `docs/issues/websocket_session_registry.md` — 物理 Vessel SDK 共通基盤化案件
- `docs/issues/stackchan_serial_log_integration.md` — Stack-chan シリアルログ統合案件
- stackchan-mcp / Stack-chan 関連:
  - `https://github.com/kisaragi-mochi/stackchan-mcp` — 採用する MCP gateway + firmware リポジトリ（MIT + GPL-3.0 ハイブリッド）
  - `https://github.com/maha0525/stackchan-mcp` — 我々の fork（feature/external-pcm-stream ブランチで PR 作業中）
  - `https://github.com/m5stack/StackChan` — M5Stack 公式（参考、Apache 2.0）
  - `https://www.switch-science.com/products/11129` — Kickstarter 版（SKU 11129、組み立て済み販売）

## 決定事項記録

実装着手前のインタビューで確定した設計判断、および路線変更の経緯。

### v0.1 → v0.2 で確定

- **TTS 統合方針**: 案 D（voice-tts `audio_stream.subscribe` 相乗り）採用
- **Vessel Building の UI 表示**: 通常 Building と並べる（特別な区画にしない）
- **本体改修要否**: `Building.PHYSICAL_VESSEL_ID` カラム追加 1 個を主、WebSocket 周りで必要なら汎用最小改修を許容
- **認知モデル**: Building = 身体 / ペルソナ = 脳・魂 / マイク = 耳 / 音声理解 (Gemini inline、v0.7 で転換) = 聴覚野 / スピーカー = 口 / TTS = 発声 / カメラ = 目 / サーボ = 姿勢 / タッチ = 触覚 のメタファーで統一
- **音声入力経路**: `manager.handle_user_input_stream` 経由でユーザー発言として注入（v0.7 で `metadata.media[]` 経由の音声ファイル添付に拡張）

### v0.2 → v0.3 で確定

- **MP3 vs PCM 直送**: Phase 2 は MP3 のまま流す（後に v0.4 で PCM 直送に変更、v0.5 でさらに Opus encode に変更）
- **WebSocket 自動マウント**: アドオン `api_routes.py` の `@router.websocket()` が動くか Phase 1 で実機検証（v0.5 で gateway が WS server を持つ形になり、自動マウント検証は不要に）
- **STT 初期バックエンド**: OpenAI Whisper API → v0.5 で stackchan-mcp gateway 側 Whisper に変更 → **v0.7 で Gemini inline 認識に転換（= STT を経由せず音声ファイルを Gemini ペルソナに直接渡す）**
- **ウェイクワード**: sdkconfig で有効なもの（現状デフォルトは ESP-SR の `WN9_NIHAOXIAOZHI`、= "你好小智"）+ LCD 画面短タップ起動の併用。発音できないユーザーはタッチ経路を使う。カスタムウェイクワード（ペルソナ名等）は将来 Phase
- **ペアリング UX**: QR コード表示 + 手入力フォーム併用
- **複数 Vessel 対応**: Phase 1 は 1 台前提、データモデルは複数対応

### v0.3 → v0.4 で確定（Phase 2 実装中）

- **MP3 → PCM 直送に切り替え**（v0.5 では Opus encode に変更）
- **broadcast model の pacing 責務は consumer 側**（v0.5 では gateway 側に移管）
- **WS frame 8 KB 分割**（v0.5 では不要に、stackchan-mcp 側で処理）
- **PCM rotation buffer (4 個 × 32 KB) 必須**（v0.5 では不要に、stackchan-mcp ファームが管理）
- **WebSocket session の identity-aware unregister**（v0.5 では不要に、stackchan-mcp gateway が処理）
- **シリアルログのキャプチャ**: `temp/stackchan_serial_capture.py` で `~/.saiverse/user_data/logs/<最新>/stackchan_serial.log` に書き出す（v0.5 でも継続利用、stackchan-mcp ファームの起動ログ確認用）
- **物理 Vessel SDK 共通基盤化**: 2 例目が出たら本格着手

### v0.4 → v0.5 で確定（路線変更）

- **自前ファーム廃止、stackchan-mcp 採用**: stackchan-mcp が既に実装している機能を自前で再実装する必要がない、唯一の欠け（TTS）は Phase 2 で完成した経路の宛先切り替えで埋まる、将来にわたって作業量が最短、の判断軸で乗り換え決定。決定日 2026-05-13
- **audio 経路の構造**: voice-tts → addon の speak_hook → HTTP PCM POST → stackchan-mcp gateway → Opus encode → device。**device への** PCM 直送（自前ファーム時代の経路）は廃止、**gateway への** PCM 直送経路（Phase 1 で stackchan-mcp に追加した `send_pcm_audio` / `send_pcm_stream` + PR3 で追加する HTTP PCM endpoint）に置き換え
- **認証**: Bearer Token ベース。SAIVerse-stackchan-addon 側で `token_hash → vessel_id → bound_building_id` の対応テーブル（`vessels.db` スキーマ改修）。HTTP PCM 経路は別 token `STACKCHAN_PCM_TOKEN`（`VISION_TOKEN` の前例に倣う）
- **ファーム書き込み**: esptool 経由（SAIVerse の CLI / UI で自動化）。Web Serial フラッシュは廃止
- **不変条件 #10〜#13 (v0.4)**: 自前ファーム特有の制約として無効化。本書「廃止された不変条件」節で記録
- **archive 化**: `expansion_data/saiverse-stackchan-addon/firmware/` と `audio_stream_bridge.py` 等を archive へ移動

### v0.5 初稿 → v0.5 再改訂で確定（統合方式変更、2026-05-13 同日）

v0.5 初稿の以下 2 点を撤回・再決定:

- **gateway 起動方式**: 初稿「SAIVerse プロセス内で `import + start`」→ 再改訂「**subprocess として SAIVerse 本体の MCP client が起動**」(`expansion_data/saiverse-stackchan-addon/mcp_servers.json` を Elyth と同じ枠組みで配置)
- **MCP tools 取り扱い**: 初稿「SAIVerse-stackchan-addon の native tool として thin wrap」→ 再改訂「**SAIVerse 本体 MCP client が直接呼び出す**」（`spell_tools` で visible 制御）

理由:

- 初稿は「SAIVerse は MCP client じゃない」という誤認識のうえで `import + start` を選んでいた。実際は SAIVerse は MCP client 機構を実装一巡完了 (Elyth で実機検証中) で、これを活用する設計が筋
- in-process import は MCP transport (stdio) を SAIVerse の標準入出力と衝突させる問題があり、技術的に複雑
- subprocess 起動なら upstream の標準起動方法に従える、SAIVerse の既存 MCP integration 機構をそのまま活用できる

派生する追加:

- **upstream PR3 (HTTP PCM 受入 endpoint)**: subprocess 化で voice-tts → gateway の PCM 経路が in-process で渡せなくなる代わりに、HTTP POST (chunked transfer) で渡す。stackchan-mcp 側に endpoint を新設する PR を Phase 5' で投稿
- **本体 MCP client 改修 (A-3-c)**: Building 単位 visibility 制御を本体 MCP client に汎用機能として追加。addon 個別じゃなく他の MCP server でも「特定 Building でだけ使えるツール」を表現可能になる
- **不要になった項目**: `gateway_runner.py`（in-process 管理層）、`tools/stackchan_*.py`（native wrap 13 個）

### v0.5 → v0.6 で確定（Phase 1' 完了、2026-05-13）

Phase 1' を実機検証込みで完走。当初の想定 (= PR 3 件) から実装範囲が拡大して **計 7 PR + addon 側 3 修正**で完了。

**新規発見された必須修正** (= Phase 1' 検証中に判明、当初想定の Phase 1' スコープ外だったが、実機動作の前提条件):

- **PR4 (xiaozhi OTA skip)**: NVS の `websocket.url` が boot 時に xiaozhi OTA server から書き戻される副作用を遮断。stackchan-mcp 設計の「OTA-config code path is disabled by design」コメントは実装上は嘘で、消費側 (application.cc) は止めてるが副作用 (ota.cc の NVS write) は残ってた
- **PR5 (libopus bundle)**: Windows ユーザーが `pip install stackchan-mcp[tts]` で詰まる致命罠を addon-side じゃなく upstream で恒久対処 (= addon が gateway の不完全性を補う構図を避ける、memory `feedback_user_experience_first.md` の判断軸)
- **PR6 (persistent WS opt-in)**: stackchan-mcp の「voice session driven」設計に「server-driven push」モードを追加。SAIVerse の persona 主導発話に必須、他の用途 (= 来客通知音、event 駆動 audio 等) にも恩恵
- **PR7 (`client_max_size=0`)**: aiohttp default 1 MiB cap で長時間 chunked transfer が途中切られる致命問題を解消

**新規発見された addon 側の必須修正**:

- `vessel_manager.get_vessel_for_persona` に NULL fallback 追加 (= Vessel Building capacity=1 セマンティクスと整合させる building-only bind)
- `speak_hook` に `_wait_first_chunk` ロジック追加 (= voice-tts の GPU model load 中の TTS first chunk 遅延で device が speaking → listening に勝手に戻る問題を解消)
- `mcp_servers.json` に `--with opuslib` 追加 (= uvx が `[tts]` extra を取り損ねるパターンへの保険)

**確定した動作**:

- ✅ persona 主導発話 (= voice-session 外、SAIVerse 内で AI 返答した瞬間に device speaker から音) が成立
- ✅ 100 秒 / 200 秒の長文 2 回連続完走、GPU load 中 / load 後の両条件で安定
- ✅ device WS は起動時から persistent モードで接続維持 (= NVS opt-in)
- ✅ Windows ユーザーは `[tts]` extra で完結 (= 手動 libopus install 不要)

**残った未解明事項** (= Phase 5'〜X' での再観察対象):

- 一度だけ観測された「発話末尾の取りこぼし」(= 2026-05-13 20:25 頃)。再現せず偶発と評価したが、再発時に分析できるよう speak_hook に投入 cadence の DEBUG ログを残してある

### v0.6 → v0.7 で確定（Phase 3' 設計転換、2026-05-18）

Phase 3' の音声入力経路を **stackchan-mcp gateway 内部 Whisper STT 経由から Gemini inline 認識経路に転換**。

**転換の判断軸**:

- 文脈なし STT は固有名詞（ペルソナ名・ユーザー名・SAIVerse 固有用語）を高頻度で誤認識する（= 別案件ナチュレの実証で足踏みした既知パターン）
- 2026-05-18 に SAIVerse 本体でユーザー添付音声・動画経路（v1.0、`/upload-audio` + `metadata.media[]` + Gemini `inline_data`）が実装完了し、Vessel 経由音声もこの基盤に乗せれば追加実装が最小
- Gemini 音声理解能力（1 秒 = 32 token、9.5 時間まで対応）は SAIVerse の対話シナリオで精度・コスト・遅延すべて gateway Whisper を上回る

**設計の主要変更点**:

- **音声入力経路（§ C-2）**: ｽﾀｯｸﾁｬﾝ device → gateway → addon HTTP hook → `~/.saiverse/audio/` に保存 → `manager.handle_user_input_stream` に `metadata.media[]` 付きで注入 → Gemini ペルソナが `inline_data` で「音」を直接理解。Whisper STT を経由しない。
- **メタファー表**: `STT (gateway 側 Whisper)` を `音声理解 (Gemini inline 認識)` に書き換え。認知モデル（マイク=耳、聴覚野=音声理解、…）の頑健性は維持。
- **不変条件 §6**: `STT 結果は metadata で由来を明示する` を `音声入力は metadata で由来を明示する` に改題。`metadata.media[]` を含む形に拡張。
- **listen MCP tool**: `visible: false` を維持。理由を「Phase 3' で別経路設計」から「v0.7 で Gemini inline 認識に転換、本 tool は使用しない」に変更。
- **将来 Phase**: 非Gemini ペルソナ向け書き起こし経路（= Vessel に Claude / OpenAI ペルソナを置きたい需要が出た際の `persona_audio_transcribe`）を将来 Phase リストに追加。既存 `ensure_audio_summary` キャッシュ経路は touch しない方針。
- **ウェイクワード**: 確定事項を更新。sdkconfig デフォルトは ESP-SR の `WN9_NIHAOXIAOZHI`（= "你好小智"）。発音できないユーザーは LCD 画面短タップ（< 500ms、`Application::ToggleChatState()` 起動）で listen を発火可能（= stackchan board の `Ft6336TouchPoll` で実装済み、`firmware/main/boards/stackchan/stackchan.cc:871-878`）。カスタムウェイクワード（ペルソナ名等）は将来 Phase。

**新規必要となる上流 PR**:

- stackchan-mcp gateway に「STT せず Ogg/Opus を外部 hook に push するモード」（= 新規 `listen_raw()` MCP tool か、既存 `listen()` に `transcribe: false` オプション追加）。Phase 3' 着手時に手元 fork で動作確認 → upstream PR 提出。

**追加するアドオン実装**:

- `audio_input_relay.py` 新規（旧設計の `stt_relay.py` から命名変更、STT を経由しないため）

### v0.7 → v0.8 で確定（Phase 2' 完了、2026-05-19）

Phase 2' (= 認証・ペアリング UX) を実機検証込みで完走。 当初の想定 (= 5 step) から実装範囲が拡大して **計 6 step + 本体 2 箇所改修 + addon 大幅拡張** で完了。 詳細は §「スコープ」 §Phase 2' の完了マーク + 「Phase 2' 中に発見した汎用拡張」 節を参照。

**完了した step (アドオン側)**:

- **Step 1** ペアリング HTTP API (`POST /pair` / `GET /vessels` / `DELETE /vessels/{id}` + AddonConfig 自動更新)
- **Step 2** UI パネル `VesselPairingSection` (Building dropdown + token 表示 + コピーボタン + 解除ボタン + connected status)
- **Step 4** UI 経由 esptool flash (NVS erase + firmware 書き込み + SSE 進捗 + COM port 検出 + firmware path 解決)
- **Step 6** AddonPanelProps の `onConfigChanged` 統合 (ペアリング後に親 AddonManagerModal が再 fetch)

**完了した本体改修**:

- **Step 5** `tools/mcp_client.py` の `reconnect_server`: source JSON 再 load + AddonConfig 最新値で再 interpolate → connection.config 更新 → subprocess 再起動。 他 addon が AddonConfig を rotate するシナリオ全般で活きる汎用改修
- **Step 6** `frontend/src/components/AddonManagerModal.tsx` + `frontend/scripts/sync-addon-panels.mjs`: `AddonPanelProps.onConfigChanged?` 追加 + `ParamsSection` の `useEffect([addon.params])` 追従 + `refetchAddons` callback 経路。 他 addon (OAuth token 更新等) でも汎用に使える

**設計判断の固定 (v0.8 で確定)**:

- **GPL-3.0 firmware を addon に複製しない**: stackchan-mcp の ESP-IDF build 成果物 (= `<repo>/temp/stackchan-mcp/firmware/build/merged-binary.bin`) を path 参照で使う、 addon storage には複製しない。 §I-1 で想定されていた `~/.saiverse/addons/.../firmware/` への配置経路は一般ユーザー向け fallback として残す
- **`reconnect_server` の env 再 interpolate は source JSON 再 load が必須**: `_server_meta["raw_config"]` は起動時に解決済みの dict (= `${addon.X.Y}` マーカーは消えてる)、 そのまま `resolve_config_placeholders` を再呼び出ししても no-op。 source JSON から生 cfg を読み直して `_interpolate_value` を新たに走らせる
- **Phase 1' single vessel 前提を維持**: `POST /pair` で既存 vessel があれば 409 を返す。 multi-token validation は upstream PR 待ち
- **delete vessel で AddonConfig をクリアしない**: 再ペアリング時に上書きされる、 削除直後の入力欄が空になると UX 不便
- **NVS partition のみ erase で AP モード復帰**: `esptool erase-region 0x9000 0x4000` (= partitions/v2/16m.csv) で firmware 保持のまま再設定 UI に戻れる。 stackchan board に SystemReset が組み込まれてないため、 物理ボタン長押し等の AP 復帰トリガは無く esptool 経由が唯一の手段

**v0.8 で別 Phase に移送/省略した item**:

- **Step 3** (= 自前 flash + UI ペアリング検証) は Step 4 (= UI 経由 esptool flash) の完成で UI 内完結する形に統合、 個別 step としては撤回
- **Step 8 (intent doc 更新) / Step 9 (Wi-Fi 入力 1 回目失敗バグ調査)** は Phase 2' 後の引き継ぎ項目として継続
- **QR コード生成 + 表示**: device 側に QR スキャナがない (= xiaozhi-esp32 / stackchan board firmware 標準には未実装) ため省略。 Phase 6 以降の候補。 当面はテキスト + 「コピー」 ボタンで代替
- **`saiverse stackchan flash` CLI サブコマンド**: UI 経由 SSE 進捗 stream に統合 (= ターミナルを開かない UX が優先、 まはー判断 2026-05-19)

**Phase 2' の運用上の罠 (= Phase 3' 着手前に把握しておく)**:

- **ペアリング解除後の再ペアリング**: addon UI で「解除」 ボタンを押すだけでは device 側 NVS の古い token が残るため、 device は古い token で接続を試みて 401 reject になる。 解除後に **必ず NVS erase (= addon UI の「NVS リセット」 ボタン)** を実行して device を AP モードに戻す、 captive portal で新 token を入力する流れが正規 UX
- **Wi-Fi 入力 1 回目失敗**: 実機検証中に発見、 stackchan-mcp ファーム (= xiaozhi-esp32 base) の captive portal で Wi-Fi SSID/Password を入力すると 1 回目は必ず失敗、 同値で 2 回目入力で通る挙動。 Step 9 で調査予定 (= 直せるなら upstream PR 候補)

### v0.8 → v0.9 で確定（Phase 3' 完了、2026-05-21）

Phase 3' (= 音声入力経路 / Gemini inline 認識) を実機検証込みで完走。 当初の想定 (= step 11〜13 + 上流 PR 1 件) から実装範囲が拡大し、 タッチ UX 周りで firmware 側に **計 5 commit** の追加修正が必要になった。 詳細は `docs/issues/stackchan_mcp_upstream_pr_strategy.md` の追補「PR-L/M」 節を参照。

**完了した step (アドオン側)**:

- **Step 11** stackchan-mcp gateway に「device-driven listen 音声を外部 hook に POST するモード」 を fork 実装 (= `feature/device-driven-audio-capture-with-hook` の 4 commit、 PR-F として upstream 投稿予定)
- **Step 12** `audio_input_relay.py` で gateway POST 受信 → `~/.saiverse/audio/` に保存 → `manager.handle_user_input_stream` に `metadata.media[]` 付きで注入
- **Step 13** LCD 短タップ → 発話 → タッチで送信 → Gemini ペルソナが固有名詞含む発話を理解して応答、 実機で確認

**Phase 3' で発見した firmware 改修事項 (= PR-L/M 経路)**:

- **listening 中タップを CloseAudioChannel ではなく StopListening に分岐 (commit `759508b`)**: xiaozhi-esp32 既定の `Application::HandleToggleChatEvent` は listening 状態 2 回目タッチで `CloseAudioChannel()` (= WS 切断) を呼ぶ、 gateway 側で `aborted_mid_capture` として buffer 破棄。 device-driven audio capture push 経路 (= PR-F) に流れず音声が SAIVerse に届かないので、 stack-chan board の `PollTouchpad` で listening 中タップを `StopListening()` (= SendStopListening 経路) に分岐
- **listen 起動を ToggleChatState から StartListening に変更 (commit `397d3bc`)**: 前者は `SetListeningMode(AutoStop)` を渡すため、 ペルソナ発話終了 (`tts.stop` 受信) の Schedule 内で device が自動的に Listening 再復帰 (`application.cc:565` = xiaozhi の連続会話モデル)。 結果「タッチして喋ろう」 が「listen.stop = 即送信」 として処理されてしまう。 `StartListening()` 経由は `HandleStartListeningEvent` で `SetListeningMode(ManualStop)` を強制するので、 tts.stop 後は Idle に留まり、 次のタッチで明示的に listen 開始する Vessel UX が成立する
- **タッチ瞬時の OGG_POPUP 音 (commit `e5f62d4`)**: `Application::StartListening()` 内で `play_popup_on_listening_ = true` を立てて、 `HandleStateChangedEvent` の `kDeviceStateListening` 分岐内 ResetDecoder 後 PlaySound 経路に乗せる。 既存実装は WakeWord 経路でしか flag 化されていなかったので、 タッチ / API 経由の StartListening では音が鳴らなかった
- **デバウンス / listening タイムアウト / LED feedback (commit `e13a544`)**: 直前 release から 300ms 以内の press を無視 (= 連打事故防止)、 listening 滞在 30 秒で auto-StopListening (= タッチ忘れ放置防止)、 タッチ確定で全 RGB LED 緑点灯 / 消灯 (= 体感フィードバック、 MCP set_led で上書き可能)。 加えて nano-printf 非対応の `%lld` を `%d + (int)cast` に format 修正

**Phase 3' の運用上の罠**:

- **タッチ後の音 cue は state 遷移完了を待つ**: PollTouchpad から直接 `app.PlaySound()` 呼び出しても、 直後の `EnableVoiceProcessing(true)` → `ResetDecoder()` で playback queue がクリアされて音が消える。 `play_popup_on_listening_` flag 経由 (= xiaozhi 標準の WakeWord 経路と同じ仕組み) でしか確実に鳴らせない
- **LED は MCP `self.led.set_*` ツールで上書き可能**: ペルソナがツール経由で LED を操作するシーンでは、 タッチフィードバックの LED 緑点灯と衝突する。 当面は LED feedback を維持して試行運用、 将来的に画面オーバーレイ等の独立フィードバック経路を別 PR で追加検討

**v0.9 で別 Phase に移送/省略した item**:

- **画面オーバーレイ (= マイク icon 等)**: LED feedback が体感的に効いているので当面はそのまま、 LED とペルソナの set_led 衝突が実運用で問題化した時点で画面側に再検討
- **デバッグ ESP_LOGI の最終整理**: 観測 instrumentation (= `97cd6bd` の PollTouchpad / Application 状態遷移 ログ) は upstream PR には入れない、 fork-only で運用継続。 観測完了で必要性が薄れたら撤去 or LOGD 化を別 PR で

**Phase 3' の上流 PR**:

- **PR-F** (= 既存): stackchan-mcp gateway に device-driven listen audio forwarding を追加 (= `feature/device-driven-audio-capture-with-hook` の 4 commit)
- **PR-L** (= 新規 2026-05-21): `Application::StartListening()` で popup-on-listening flag を立てる (= WakeWord 以外の listen 起動でも音 cue を鳴らす、 xiaozhi-esp32 ecosystem 全体に有用)
- **PR-M** (= 新規 2026-05-21): stack-chan board のタッチ UX 統合 (= StopListening 分岐 + StartListening 経路 + RGB LED feedback + デバウンス + タイムアウト + nano-printf format fix)

PR 投稿は当面 dev/integration 運用継続、 実機で安定運用が確認できた段階で着手 (= まはー判断 2026-05-21、 「ひとまずこのまましばらく運用してみる」)。

### v0.9 → v0.10 で確定（複数機体の同時稼働、2026-06-30）

複数の Stack-chan を同時稼働させ、ペルソナが各機体に降りられるようにする方針を設計（Phase 7'/8'、設計 K）。対話で詰めた確定事項:

- **A-2 方式採用（機体ごとに gateway インスタンス）**: 「1 gateway = 1 device」を保ったまま機体数ぶん subprocess を別ポートで起動。upstream gateway の multi-device 改修（A-1: `ESP32Manager` の Dict 化 + multi-token + tool 宛先引数）は不採用。本体の既存機構（1 エントリ = 1 subprocess + 名前空間 + instance_key）に乗るため upstream 非依存で完結する。
- **ファーム改修は不要**: device は接続時に `Device-Id`=MAC / `Client-Id`=UUID / per-device token を既に送り、gateway URL も NVS（captive portal）で機体ごとに設定可能（`websocket_protocol.cc` 425-430 で確認）。当初「device state machine に波及して重い」と見積もったが、コード確認の結果その波及は無く、改修は gateway(Python) 側に閉じると訂正。
- **本体 MCP client を汎用「名前付きインスタンス」化**: instance_key に `:instance:{id}` 次元を足し、同一 server から N インスタンスを動的起動 + per-instance config 注入。Stack-chan 固有でなく汎用基盤として `mcp_addon_integration.md` に切り出す。
- **静的手書きは不採用、動的起動がリリース要件**（まはー判断: 「静的手書きは絶対やらん、動的起動までやらないならリリースできない」）。`vessels.db` 駆動で動的 register / start / stop。
- **ポートはペアリング時に確定・vessels.db 永続**（起動ごとの動的割当は device の固定 URL と両立せず不可。「動的」= ユーザーが手で選ばない、の意味）。
- **ペルソナに生 MCP ツールを露出させない（wrapper dispatcher で単一名前空間）**: 全身体ツールを native wrapper に集約し、現在 building → vessel → instance を実行時解決。生ツール露出をやめるのは複数機体に依らず正しい投資（まはー同意: 「変えたい・足したい時に結局ラップが要る」）。
- **per-vessel capability カタログ、手動が基盤**: ユニット由来ツールは搭載機体でのみ visible（不変条件 #14）。`unit_env3_enabled` 等の addon 単一 toggle を per-vessel に作り変える。自動検出は手動の上に乗る後付けアシスト（まはー判断: 手動を先に確実な基盤として作り、自動検出ボタンで設定欄を埋める）。
- **機体管理 UI が必要**（まはー判断）: 機体ごとの接続 / バインド / capability / ペアリング / 削除を一望。
- 当初「v0.9 で書く」としていたが既存が v0.9（Phase 3' 完了）だったため **v0.10** として追記。将来余地 §1〜§3 を実装フェーズに引き上げ。
