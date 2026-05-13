# Intent: スタックチャン Vessel 統合（saiverse-stackchan-addon）

**ステータス**: v0.5（2026-05-13 改訂、stackchan-mcp 採用への路線変更を反映）

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
| STT（gateway 側 Whisper 等） | テキスト変換 | 聴覚野 |
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

- **聴覚知覚 = Building 内ユーザー発言**: STT 結果は通常のユーザー発言として `handle_user_input_stream` 経由で注入する（外部イベントとして扱わない）。ペルソナ視点では「同じ部屋で人が話しかけてきた」=通常会話。
- **発声 = ペルソナ発話**: 通常の Building 発言経路に乗り、`persona_speak` server_hook 経由で voice-tts が拾って、その PCM を gateway 経由で device に流す。
- **触覚 = Building 内 host メッセージ**: タッチイベントは Building の host メッセージとして注入し、ペルソナの SAIMemory に通常履歴と並んで残る。
- **視覚 = MediaBuffer attachment**: カメラ画像は `multimodal_input_pipeline` の既存経路に乗る。
- **物理身体への憑依・離脱 = OccupancyManager.move_entity**: 既存の入退室メカニズムがそのまま「乗り換え」「降りる」を表現する。

このメタファー一貫性により、ペルソナはコード上の特別な分岐なしに、自然と物理身体の主体として振る舞える。

## これは何でないか

- **Vessel 共通仕様の一般化ではない**。最初は Stack-chan 1 機種に特化した実装にし、Vessel 抽象を慎重に育てる。複数 Vessel タイプを最初から想定したテーブル設計や抽象化はしない（早すぎる抽象化の回避）。
- **新しい音声会話モデルの設計ではない**。STT / TTS は voice-tts と stackchan-mcp gateway の組み合わせで成立させる。OpenAI Realtime / Gemini Live への対応は将来課題。
- **新しいツール基盤の設計ではない**。stackchan-mcp が既に提供する MCP tools を thin wrap して SAIVerse の native tool にする。Phase 4-5 で「自前ファームに各機能を移植する」作業は発生しない。
- **新しい入出力経路の追加ではない**。STT は `manager.handle_user_input_stream`、TTS は voice-tts + gateway の `send_pcm_stream`、タッチは `manager.add_building_event`、カメラは `multimodal_input_pipeline` MediaBuffer、すべて既存経路への合流で済ませる。
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

v0.4 から **変更なし**。物理機体は 1 台しかなく、複数ペルソナが同時に同一身体に降りられる概念ではない。`Building.CAPACITY = 1` で既存 OccupancyManager のキャパシティチェックが効くようにする。

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

- **音声入力 (STT 後テキスト)** → `manager.handle_user_input_stream(text, building_id=vessel_building_id, metadata={"source": "stackchan_voice"})`。stackchan-mcp の `listen()` MCP tool が STT を内部で実行するので、その結果を SAIVerse 側で受け取って `handle_user_input_stream` に流す。
- **タッチ入力 (なでなで)** → `manager.add_building_event(building_id, {"role": "host", "content": "...", "metadata": {...}}, heard_by=[...])`。stackchan-mcp の `get_touch_state()` MCP tool で取得、または gateway 側 hook で push 通知（要検証）。
- **音声出力 (TTS)** → voice-tts の `subscribe_pcm(msg_id)` で PCM iterator を取得、stackchan-mcp gateway の HTTP PCM 受入 endpoint (`POST /pcm`、port 8766 の既存 HTTP capture server に追加、上流 PR 必要) に **chunked transfer encoding** で送信。subprocess の gateway が受け取った PCM を Opus encode + WebSocket 配信 + tts.start/stop ステート管理（Phase 1 で実装済みの `send_pcm_stream` 経由）。認証は `Authorization: Bearer ${STACKCHAN_PCM_TOKEN}`（`VISION_TOKEN` の前例に倣う）。
- **カメラ画像** → 既存の `multimodal_input_pipeline` の MediaBuffer 経路。stackchan-mcp の `take_photo()` MCP tool（SAIVerse の MCP client 経由で呼び出し）が HTTP capture endpoint で画像を受信、結果ファイルパスを SAIVerse 側で MediaBuffer に流す。
- **サーボ・画面・LED・タッチ** → stackchan-mcp の MCP tool（`move_head` / `set_brightness` / `set_led` / `get_touch_state` 等）を SAIVerse の MCP client が直接呼び出す。`mcp_servers.json` の `spell_tools` で各 tool の visible / display_name 制御。native wrap (thin wrap class) は **作らない**。

新しいデータパスを生やすたびに本体が拡張されるのを避ける、という設計原則は維持。

### 6. STT 結果は metadata で由来を明示する

v0.4 から **変更なし**。`handle_user_input_stream` に渡す `metadata` に `{"source": "stackchan_voice", "vessel_id": "..."}` を含める。ペルソナ側はこの metadata からその発言が物理マイク経由であることを認識でき、応答の文体・反応を調整する余地を残す。

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
- `audio_input_pipeline.py`（自前 STT 経路） → 不要、stackchan-mcp の `listen()` MCP tool 経由に変更
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
- `listen`: STT 経路は Phase 3' で別途設計（直接 MCP tool 呼び出しじゃなく `handle_user_input_stream` 経由）

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
  ↓ vessel_manager.get_active_vessel_for_persona(persona_id, building_id)
  ↓ vessel が居れば、audio_stream.subscribe_pcm(msg_id) で PCM Queue 取得
  ↓ HTTP POST: http://127.0.0.1:8766/pcm
  ↓   Authorization: Bearer ${STACKCHAN_PCM_TOKEN}
  ↓   Content-Type: application/octet-stream
  ↓   X-Sample-Rate: 32000
  ↓   X-Channels: 1
  ↓   X-Message-Id: <msg_id>
  ↓   Transfer-Encoding: chunked
  ↓   body: PCM bytes を Queue から逐次 yield して chunked 送信
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

#### C-2. STT 入力経路

stackchan-mcp は v1.3.0 で `listen()` MCP tool を持つ（gateway 側 Whisper、AudioCodec で device PCM 受信 → STT）。SAIVerse からの呼び出し:

```
[Stack-chan device] ウェイクワード検出
  ↓ device → gateway: listen.start 要求
  ↓ gateway: audio_stream.start_recording()
  ↓ device: PCM Opus フレーム送信
  ↓ device: listen.stop 要求 (無音検知)
  ↓ gateway: audio_stream.stop_recording() → STT
  ↓ gateway: 結果テキストを SAIVerse 側に通知 (= speak_hook 的な hook が要る、PR 検討)
[saiverse-stackchan-addon] stt_relay:
  ↓ manager.handle_user_input_stream(text, building_id=vessel.bound_building_id, metadata={"source": "stackchan_voice", "vessel_id": vessel.vessel_id})
  ↓ 既存経路: Building 履歴記録 → ペルソナの auto_ingest → 応答生成
```

stackchan-mcp 側に「STT 結果を gateway 内部で MCP client に返すんじゃなく、外部 hook に渡す」仕組みが現状ない可能性があり、必要なら PR で追加する（Phase 3 着手前に検証）。

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
| `listen` | STT | **false**（Phase 3' で別経路設計） |
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

### なぜ STT は stackchan-mcp の `listen()` MCP tool 経由にするか

stackchan-mcp は v1.3.0 で gateway 側 Whisper を持つ。これに乗ることで:

- STT エンジンの選択・差し替え（faster-whisper / openai whisper API）が gateway 側で完結
- device 側 PCM 集約・無音検知も gateway 側
- 我々の `audio_input_pipeline.py`（自前 STT 経路）が不要に

ただし、gateway 側の `listen()` は MCP client への返り値として STT 結果を返す設計。SAIVerse 側（=  MCP client じゃなく direct caller）は別の取得経路が必要。具体的には:

- a. `listen()` を Python 関数として import して呼ぶ（= 同期 / await で結果取得）
- b. gateway に STT 結果 push hook を追加する PR

案 a が現実的（= MCP layer をスキップして handler を直接呼ぶ）。実装で確認。

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

### Phase 1' — gateway 起動と PCM 経路確立（v0.5 着手）

1. **手元 fork で PR 3 つを実装** (= `send_pcm_audio` + `send_pcm_stream` は Phase 1 完了、加えて **PR3: HTTP PCM 受入 endpoint** = `POST /pcm` を既存 HTTP capture server に追加、`STACKCHAN_PCM_TOKEN` 認証 + chunked transfer 対応)。PR 投稿は Phase 1'〜4' の実機検証後、Phase 5' で
2. **アドオン**: `mcp_servers.json` 作成 (= Elyth と同じ枠組み、`command: "stackchan-mcp"` + `env` + `spell_tools` 配列で 15 個 visible / 6 個 visible=false)
3. **アドオン**: `speak_hook.py` で persona_speak fired → `voice-tts.subscribe_pcm` → HTTP POST (chunked) で stackchan-mcp gateway の `/pcm` endpoint に送信
4. **アドオン**: `vessel_manager.py` で `vessels.db` の token-based テーブル管理
5. **検証**: ファーム書き込み → AP 設定 → device 接続 → voice-tts 発話 → device スピーカーから音が出る

### Phase 2' — 認証・ペアリング UX

6. **アドオン**: `api_routes.py` でペアリング HTTP API（token 発行、vessel 一覧）
7. **アドオン**: AddonManager UI にパネル追加（追加・削除・状態表示）
8. **アドオン**: QR コード生成 + 表示
9. **アドオン**: `saiverse stackchan flash` CLI サブコマンド実装（esptool 自動書き込み）
10. **検証**: 新品 Stack-chan → 開封 → SAIVerse UI 経由で書き込み → AP 設定 → 接続完了まで 30 分以内

### Phase 3' — STT 経路

11. **gateway 側調査**: stackchan-mcp の `listen()` を direct Python 経由で呼ぶ実装（必要なら PR）
12. **アドオン**: `stt_relay.py` で `listen()` 結果を `handle_user_input_stream` に流す
13. **検証**: "Hi, stack-chan" → ペルソナが音声で応答

### Phase 4' — 本体改修（visibility 制御）+ tool 一覧の最終確定

**Phase 4' で扱うこと**:

- ツール定義の追加は **Phase 1' で `mcp_servers.json` を書いた時点で完了**（spell_tools 配列で 15 個 visible / 6 個 visible=false）
- Phase 4' は **本体改修 (A-3-a/b/c)** が主スコープ、加えて実機検証中に新規発見されたツール（or 不要と判明したツール）の `mcp_servers.json` 調整

**ツール選別** (現時点候補、実機検証中に最終確定):

stackchan-mcp の 21 ツールから:

- **visible (15 個)**: move_head, take_photo, set_avatar, set_mouth, set_mouth_sequence, set_blink, set_led, set_all_leds, set_leds, clear_leds, set_brightness, set_volume, get_touch_state, get_head_angles, get_device_info
- **visible=false (6 個)**: get_status（管理者向け）, say（voice-tts に置き換え）, listen（Phase 3' で別経路）, gpio_test / uart_diag / check_vm_en（診断系、管理者向け）
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

### Phase X' — 上流 PR 投稿（Phase 1'〜4' の実機検証後）

stackchan-mcp 本家へ PR 3 つを投稿。Phase 1'〜4' で手元 fork の動作妥当性を実機検証してから提出する（= PR 投げて実際には使いませんでした、になるのを避ける）。

23. **PR1 整形 + 投稿**: `send_pcm_audio(gateway, pcm)` の切り出し（Phase 1 で実装済み、commit `5df0460`）。`feature/external-pcm-stream` から PR1 用に分離した branch に整形
24. **PR2 整形 + 投稿**: `send_pcm_stream(gateway, pcm_chunks)` の追加（Phase 1 で実装済み、commit `e5a83fa`）。PR1 が merge された後に提出（積み上げ）
25. **PR3 整形 + 投稿**: HTTP PCM 受入 endpoint (`POST /pcm` を既存 HTTP capture server port 8766 に追加、`STACKCHAN_PCM_TOKEN` 認証、chunked transfer encoding 対応)。PR1/PR2 とは独立に進行可能
26. **必要なら PR4**: gateway を stdio MCP server 抜きで起動するオプション、Phase 1'〜4' の実機検証で問題が出た場合のみ
27. **必要なら PR5**: STT 結果の external hook (Phase 3' でウェイクワード起動経路を作る際、gateway 側 push hook が無ければ追加)
28. **必要なら PR6**: touch event push hook (Phase 5' で polling じゃ要件満たさない場合)

### 将来 Phase（範囲外）

- カスタムウェイクワード
- 歩行（外付け車輪モジュール対応）
- IMU 連動
- 複数 Vessel 対応の本格化（Vessel テーブル切り出し）
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

### Phase 3' (STT)

- "Hi, stack-chan" → 質問 → ペルソナが応答
- Vessel に誰も居ない時のウェイクワード → 何も起きない（または "誰もいません" 応答）
- STT 失敗（雑音、無言）→ ユーザーに「聞き取れませんでした」相当のフィードバック

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

v0.4 から **基本維持**、stackchan-mcp 採用を踏まえて若干更新:

### 1. `Vessel` テーブルの切り出し

`Building.PHYSICAL_VESSEL_ID` を `FK → Vessel.vessel_id` に変更し、Vessel に固有メタデータ（型番、ファーム version、能力一覧）を集約する。stackchan-mcp 採用後は `Vessel.firmware_version` を `xiaozhi-esp32 v2.2.6` 等の値で管理。

### 2. 共通 device gateway

stackchan-mcp は xiaozhi-esp32 ベースで汎用 device gateway として既に機能している。SAIVerse 内で「stackchan-mcp 以外の vessel」が出てきた時は、stackchan-mcp と同様に `mcp_servers.json` で subprocess 起動 + stdio MCP 経由でツール公開、というパターンを踏襲する。

### 3. Vessel 能力宣言（capabilities）

stackchan-mcp の `get_device_info()` MCP tool で device の capability を取得できる。これを SAIVerse 側で Vessel レコードに記録し、ペルソナがツール選択時に参照できるようにする。

### 4. 共通の感情 → 表情マッピング

stackchan-mcp の `set_avatar` を基盤にしつつ、別機種が出てきた時に共通の感情パラメータレイヤを切り出す。

### 5. voice-tts の PCM broadcast 経路（v0.4 で実装完了、v0.5 でも継続利用）

`open_pcm_stream` / `push_pcm_chunk` / `subscribe_pcm` は v0.4 で実装。v0.5 では `subscribe_pcm` の出力を addon の speak_hook が取り出し、HTTP POST (chunked transfer) で stackchan-mcp gateway の `/pcm` endpoint に送る形で継続利用。voice-tts 自体の機能は変わらず、流通先が `audio_stream_bridge` → `HTTP POST → gateway` に変わるだけ。

### 6. stackchan-mcp upstream PR 群

我々が出した PR が merge されれば本家の機能になる:

- PR1: `send_pcm_audio(gateway, pcm)` 切り出し（手元 fork で動作確認済み、2026-05-13）
- PR2: `send_pcm_stream(gateway, pcm_chunks)` 追加（手元 fork で動作確認済み、2026-05-13）
- 将来 PR: gateway を stdio MCP server 抜きで起動するオプション（必要なら）
- 将来 PR: STT 結果の external hook（必要なら）
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
- **認知モデル**: Building = 身体 / ペルソナ = 脳・魂 / マイク = 耳 / STT = 聴覚野 / スピーカー = 口 / TTS = 発声 / カメラ = 目 / サーボ = 姿勢 / タッチ = 触覚 のメタファーで統一
- **STT 経路**: `manager.handle_user_input_stream` 経由でユーザー発言として注入

### v0.2 → v0.3 で確定

- **MP3 vs PCM 直送**: Phase 2 は MP3 のまま流す（後に v0.4 で PCM 直送に変更、v0.5 でさらに Opus encode に変更）
- **WebSocket 自動マウント**: アドオン `api_routes.py` の `@router.websocket()` が動くか Phase 1 で実機検証（v0.5 で gateway が WS server を持つ形になり、自動マウント検証は不要に）
- **STT 初期バックエンド**: OpenAI Whisper API（v0.5 では stackchan-mcp gateway 側 Whisper に変更）
- **ウェイクワード**: 固定（"Hi, stack-chan" 等の既製ワード）。カスタムウェイクワードは将来 Phase
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

### 実装フェーズ前にまはーへ確認すべき残課題

- なし。v0.5 起草完了、まはーレビュー後に Phase 1' 着手。
