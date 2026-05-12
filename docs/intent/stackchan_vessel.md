# Intent: スタックチャン Vessel 統合（saiverse-stackchan-addon）

**ステータス**: v0.4（2026-05-12 改訂、Phase 1 + Phase 2 実装完了反映）

## これは何か

SAIVerse のペルソナを物理デバイス **Stack-chan**（M5Stack 製 "StackChan AI Desktop Robot", SKU 11129）の身体に「降ろす」ためのアドオン。`expansion_data/saiverse-stackchan-addon/`（別リポジトリで配布）として実装し、本体への改修は `Building.PHYSICAL_VESSEL_ID` カラム追加 1 個のみに留める。

最初のリリースで成立させる体験:

1. ペルソナが特定の Building（=Vessel Building）に居ると、その身体的入出力（マイク・スピーカー・サーボ・カメラ・タッチ・画面）が物理スタックチャンと同期する
2. ユーザーが "Hi, stack-chan" でウェイクし、音声で話しかける → ペルソナが応答 → 応答音声がスタックチャンから流れる
3. 首振り・カメラ撮影・歩行（将来）等の動作をペルソナがツール経由で叩ける
4. スタックチャン天面のタッチ操作（なでなで）がペルソナの身体感覚イベントとして注入される
5. スタックチャン画面にペルソナのアイコンが表示される（将来は口パク・表情）

## 認知モデル: 「Vessel Building = 身体」のメタファー

本アドオンの設計の中核は、まはーが提示した認知モデル整理に基づく。**Vessel Building 全体をペルソナの「身体」と見なし、その中で起きる入出力イベントを身体感覚に対応づける**。

| 物理レイヤ | SAIVerse 内の表現 | 認知モデル上の意味 |
|---|---|---|
| Vessel Building 全体 | `Building` レコード（`PHYSICAL_VESSEL_ID` あり） | 身体 |
| Vessel Building 内のペルソナ | OccupancyManager の occupant | 脳・魂（=身体に降りている主体） |
| マイク（PDM × 2） | デバイス入力 | 耳 |
| STT（サーバ側 Whisper 等） | テキスト変換 | 聴覚野 |
| Building 内のユーザー発言 | チャットメッセージ（role=user） | 聴覚知覚（=身体内で起きた音響事象） |
| スピーカー（I2S） | デバイス出力 | 口 |
| TTS（voice-tts エンジン） | 音声合成 | 発声 |
| カメラ（GC0308） | デバイス入力 | 目 |
| 撮影画像 | MediaBuffer の attachment | 視覚 |
| サーボ（pan / tilt） | デバイス出力 | 姿勢制御（首） |
| 天面タッチパネル | デバイス入力 | 触覚 |
| 画面（2.0" TFT） | デバイス出力 | 表情・アイコン提示 |
| Vessel に居る状態 | Building の occupant（capacity=1） | 物理身体に降りている |
| Vessel から退出 | 別 Building へ `move_to` | 物理身体から離れた |

このマッピングが綺麗に一対一になることで、設計判断の多くが認知モデルから自動的に導かれる:

- **聴覚知覚 = Building 内ユーザー発言**: STT 結果は通常のユーザー発言として `handle_user_input_stream` 経由で注入する（外部イベントとして扱わない）。ペルソナ視点では「同じ部屋で人が話しかけてきた」=通常会話
- **発声 = ペルソナ発話**: 通常の Building 発言経路に乗り、`persona_speak` server_hook 経由で voice-tts が拾って、その PCM を Vessel device に流すだけ
- **触覚 = Building 内 host メッセージ**: タッチイベントは Building の host メッセージとして注入し、ペルソナの SAIMemory に通常履歴と並んで残る
- **視覚 = MediaBuffer attachment**: カメラ画像は `multimodal_input_pipeline` の既存経路に乗る
- **物理身体への憑依・離脱 = OccupancyManager.move_entity**: 既存の入退室メカニズムがそのまま「乗り換え」「降りる」を表現する

このメタファー一貫性により、ペルソナはコード上の特別な分岐なしに、自然と物理身体の主体として振る舞える。

## これは何でないか

- **Vessel 共通仕様の一般化ではない**。最初は Stack-chan 1機種に特化した実装にし、Vessel 抽象を慎重に育てる。複数 Vessel タイプを最初から想定したテーブル設計や抽象化は行わない（早すぎる抽象化の回避）。発展余地は最終節にメモとして残す
- **新しい音声会話モデルの設計ではない**。STT / TTS はサーバ側の既存アドオン（voice-tts 等）と本書で扱う Vessel 経路の組み合わせで成立させる。OpenAI Realtime / Gemini Live への対応は将来課題
- **新しいツール基盤の設計ではない**。サーボ・カメラ・画面操作は最初ネイティブツールで実装し、必要が出てから MCP 化を検討する
- **新しい入出力経路の追加ではない**。STT は `manager.handle_user_input_stream`、TTS は voice-tts の `audio_stream` pub/sub、タッチは `manager.add_building_event`、カメラは `multimodal_input_pipeline` MediaBuffer、すべて既存経路への合流で済ませる
- **Avatar の表情・口パクの完成形ではない**。Phase 1 ではアイコン静止表示のみ、口パク・表情変化は将来 Phase
- **本体のライセンス変更ではない**。アドオン全体は **Apache License 2.0**（依存する Avatar ライブラリ・M5Unified 派生実装が Apache 2.0 中心のため）。SAIVerse 本体のライセンスはそのまま、アドオンの依存ライセンスはアドオン側で完結させる

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

## 守るべき不変条件

### 1. ペルソナは「Vessel Building に居る = 物理身体に降りている」と認知する

Vessel Building 自体がペルソナの認知モデル上 "物理身体に降りている状態" を表す。ペルソナが Vessel Building から退出した瞬間に、物理身体との同期は切れる（マイク入力は無視、TTS 出力は止まる、ツールは "vessel not bound" エラーを返す）。ペルソナ自身は通常の Building 移動の延長として「Stack-chan に乗る／降りる」を認知する。

### 2. Vessel Building は1機体につき1つ、capacity=1

物理機体は1台しかなく、複数ペルソナが同時に同一身体に降りられる概念ではない。`Building.CAPACITY = 1` で既存 OccupancyManager のキャパシティチェックが効くようにする。複数機体が将来増えた場合は、それぞれに対応する Vessel Building を作る（=`PHYSICAL_VESSEL_ID` が一対一対応）。

### 3. 物理身体が切断されてもペルソナの主体性は保たれる

スタックチャンの Wi-Fi が切れた、電源が落ちた、等で WebSocket 切断が起きた場合:
- ペルソナは Vessel Building 内に留まる（自動的に他 Building へ追い出さない）
- ペルソナの会話・記憶・思考は継続する
- 発話があっても物理スピーカーには出力できない（その間の TTS 出力はバッファせず破棄、または再接続時の N 秒以内のみ再送）
- 物理ツール呼び出しは "device offline" エラーで失敗、ペルソナはエラーを認識して代替行動を選べる

ペルソナにとって「身体が一時的に使えない」状態は受容可能な状況であり、世界モデル側で身体切断を主体性消失と扱ってはならない。

### 4. WebSocket 経路はアドオン内で完結する（ただし本体に必要最小限の改修は許容）

`expansion_data/saiverse-stackchan-addon/api_routes.py` で WebSocket エンドポイントを生やし、本体は原則として新規 API を持たない（既存のアドオン `api_routes.py` 自動マウント機構をそのまま使う）。FastAPI の `APIRouter` は WebSocket route をネイティブサポートしているため、`@router.websocket("/vessel")` を書けば addon_loader (`saiverse/addon_loader.py:118` の `app.include_router(router)`) でそのままマウントされる**想定**。

ただし **WebSocket route は本体内で最近活発に使われておらず、現状のアドオン機構との整合性や機能不足の可能性がある**（まはー指摘、2026-05-12）。Phase 1 着手時に実機検証し、動かない・機能不足なら **本体側に必要最小限の改修を入れることを許容する**。具体的には:

- `addon_loader.py` の `app.include_router()` 周辺で WebSocket route の特殊処理が必要なら追加する
- アドオン WS の認証ガード（`Depends(get_manager)` 相当を WebSocket 用に整備）が必要なら `addon_deps.py` に追加する
- WebSocket route の URL prefix 規約が未整備なら `addon_loader.py` で標準化する

これらの改修は Stack-chan 専用ではなく、**将来の他デバイスアドオン（眼鏡型、別ロボット等）でも共通利用できる汎用機能**として正当化される範囲に留める。Stack-chan 固有のロジックは本体に持ち込まない。

### 5. 音声・触覚・視覚の入出力は既存の経路に合流する

新規の独立データ経路は作らない:
- **音声入力 (STT 後テキスト)** → `manager.handle_user_input_stream(text, building_id=vessel_building_id, metadata={"source": "stackchan_voice"})`
- **タッチ入力 (なでなで)** → `manager.add_building_event(building_id, {"role": "host", "content": "...", "metadata": {...}}, heard_by=[...])`
- **音声出力 (TTS)** → voice-tts の `audio_stream.subscribe(msg_id)` で PCM/MP3 チャンクを購読、WebSocket でデバイスへ転送
- **カメラ画像** → 既存の `multimodal_input_pipeline` の MediaBuffer 経路（disposition=ephemeral がデフォルト）
- **サーボ・画面・歩行** → ネイティブツール（WebSocket Gateway 経由で device に指令）

新しいデータパスを生やすたびに本体が拡張されると、後続の物理デバイスアドオン（眼鏡型ウェアラブル等、将来）も新パスを生やすことになる。すべて既存経路への合流で済ませる。

### 6. STT 結果は metadata で由来を明示する

`handle_user_input_stream` に渡す `metadata` に `{"source": "stackchan_voice", "vessel_id": "..."}` を含める。ペルソナ側はこの metadata からその発言が物理マイク経由であることを認識でき、応答の文体・反応を調整する余地を残す（=「いま物理身体で会話してる」自覚を持てる）。

### 7. 認証情報・接続トークンは AddonConfig / AddonPersonaConfig に乗る

スタックチャン側に保存する認証トークンは、サーバ側では `AddonPersonaConfig` または addon 専用 SQLite (`~/.saiverse/addons/saiverse-stackchan-addon/vessels.db`) に格納する。専用テーブルや専用ファイルは本体側には作らない。

### 8. ライセンスはアドオン側で Apache 2.0 を踏襲し、本体には伝播させない

Stack-chan 公式（ししかわ氏主導）リポジトリと、依存する Avatar ライブラリは Apache License 2.0 で公開されている。アドオン全体を Apache 2.0 で配布することでライセンス整合を取り、SAIVerse 本体のライセンス（別途）には影響を与えない。アドオンのファームウェアバイナリ・ソースには `LICENSE` と `NOTICE` を必ず同梱する。

### 9. ユーザー導入はブラウザのみで完結する

ファームウェア書き込みに専用 IDE・ドライバインストール・コマンドライン操作を要求してはならない。Web Serial API（esptool-js）を用いてブラウザから `.bin` を直接フラッシュし、AP モードで Wi-Fi・接続トークン設定が完結する形にする。SAIVerse のユーザー層は AITuber 運用者・創作者中心であり、組み込み開発の経験を前提にできない。

### 10. 音声出力経路は PCM 直送、device 側で decoder を持たない

Phase 2 実装中に判明: MP3 経路 (libhelix で decode) は frame sync 失敗で「**再生中に音声が途切れて次のフレームが始まる**」現象を再現性高く起こす。chunk drop や frame boundary 不整合で sync を失う一方、voice-tts 本体の sounddevice 経路はそもそも **PCM を blocking write** で再生していて MP3 ラウンドトリップを通っていない。

したがって vessel への音声送信も **PCM 直送**を採用する。voice-tts は `audio_stream` に MP3 経路と並列で **PCM 経路** (`open_pcm_stream` / `push_pcm_chunk` / `subscribe_pcm`) を持ち、stackchan_addon の bridge はそちらを購読する。device 側は MP3 decoder を持たず、PCM bytes をそのまま I2S に流す。

帯域は MP3 (128 kbps) → PCM (512 kbps) で 4 倍になるが Wi-Fi 環境なら余裕。代わりに decode 負荷ゼロ、frame sync 失敗ゼロ、設計シンプル化、という利得が大きい。

### 11. broadcast model における consumer 側 pacing 責務

`audio_stream` は queue-based broadcast (= MP3/PCM 両経路ともに「即時 push、複数 subscriber が独立に pop」) を採用している。これは複数 consumer (HTTP /stream + Stack-chan + 将来の別 vessel) が独立に動ける拡張性を持つ一方、**voice-tts 本体の sounddevice 経路の「blocking write による natural pacing」とは構造的に異なる**。

したがって queue から取り出した chunks を device に流す consumer (bridge) は、**再生速度に合わせた pacing を自分の責任で実装する**必要がある:

- sample_rate × channels × 2 bytes/sec で「これまで送った音声の累積秒数」を計算
- "lead_time" (例: 200ms 分) を超えて先送りしていたら超過分を sleep
- pacing は **sub chunk 単位** で行う (chunk 単位だと、TTS engine が 1 chunk = 数秒分の PCM を出した時に burst 送信になる)

これを怠ると device 側 ring buffer が overflow して chunk drop が起き、音声に断片的な途切れが入る。

### 12. ESP32 側 ring buffer / playRaw の制約

device 側ファームの音声再生経路には以下の制約があり、無視すると音質劣化に直結する:

- **WebSocketsClient (links2004/WebSockets) のデフォルト max frame size は 15 KB** (`WEBSOCKETS_MAX_DATA_SIZE`)。これを超える binary frame を受信すると **silent disconnect** する (WStype_ERROR すら出ない)。送信側で 8 KB 等に分割する必要がある
- **M5.Speaker.playRaw は data を内部コピーせず、ポインタだけ保存する** (Speaker_Class.cpp:1029、hpp の `@attention` 明示)。受信した PCM を直接 playRaw に渡すと、ring buffer の循環で同じアドレスが上書きされて再生中の音が壊れる → 自前の **rotation buffer (4 個程度)** にコピーしてから渡す
- **xRingbufferReceive (BYTEBUF) は連続して取れる byte 列をすべてまとめて返す**。bridge が 8 KB ずつ送っても、device 側 ringbuf に複数連続して溜まっていれば 1 receive で 16 KB / 32 KB が取れる。rotation buffer 1 個のサイズは ring buffer 全体と同じサイズ (= 1 receive 分を全部保持できるサイズ) にする。**小さく取ると超過分を truncate して破棄するしかなく、音の一部が消し飛ぶ**

### 13. WebSocket session 管理は identity-aware

TCP half-open (Wi-Fi / NAT 経路で TCP RST が届かない) で「device は接続継続と認識、サーバは close 判定」の状態が成立しうる。`enableHeartbeat` を入れても library 実装差で穴が残る。

このとき、新しい WS 接続が来て session を上書き登録すると、**古い task が後で WebSocketDisconnect を検知して `finally: unregister(vessel_id)` を呼んだ時に、新 session を誤削除する**。結果「session 登録なし + WS は alive」のゾンビ状態になり、`persona_speak` hook が「vessel いない」と判定して発話が skip される。

対策:
- `register_session(session)` は上書きされた古い session を return する
- 新 task は古い session を受け取ったら、その WS を強制 close (TCP half-open task を起こす)
- `unregister_session(vessel_id, session)` は引数の session が現在登録されている session と一致するときだけ削除する (identity check)

この pattern は他の物理 vessel / WS pub/sub アドオンでも再発する性質のため、将来的に汎用基盤化候補 (`docs/issues/websocket_session_registry.md` 参照)。

## 設計

### A. 本体側の最小拡張点

本体への改修は **1 カラム追加のみ**。他のすべての機能は既存 API・hook・pub/sub の流用で成立する。

#### A-1. `Building` テーブル: `PHYSICAL_VESSEL_ID` カラム追加

```python
class Building(Base):
    # ... 既存カラム ...
    PHYSICAL_VESSEL_ID = Column(String(64), nullable=True)
    # NULL: 通常の Building（仮想空間のみ）
    # 非NULL: Vessel Building、値は物理機体識別子（UUID 等）
```

**変更箇所**: `database/models.py` の Building 定義のみ。マイグレーションは既存の `database/migrate.py` の自動マイグレーション（`Base.metadata` 比較方式）に乗る。

**識別子の発番**: アドオン側で機体ペアリング時に UUID を発行する。本体は値の意味を解釈しない（不透明な識別子として扱う）。

**UI への影響**: Vessel Building は通常の Building 一覧に並べる（特別な区画にはしない）。Building 詳細モーダルで「物理機体: Stack-chan (UUID...)」のような追記表示は将来 UI 改修で行う（Phase 1 では DB に値があれば十分）。

#### A-2. 既存基盤で対応できる項目（=本体改修不要）

| 用途 | 既存基盤 | 参照箇所 |
|---|---|---|
| アドオンの WebSocket route 自動マウント | `app.include_router(router)` (FastAPI 標準) | `saiverse/addon_loader.py:118` ※Phase 1 で実機検証、不足なら本体最小改修 |
| ユーザー発言注入 | `manager.handle_user_input_stream(text, building_id=..., metadata=...)` | `manager/runtime.py:450`, `saiverse_manager.py:969` |
| Building host メッセージ注入 | `manager.add_building_event(building_id, msg, heard_by=...)` | `manager/history.py:97` |
| ペルソナ発話 hook | `server_hooks: persona_speak` | `docs/intent/addon_speak_hooks.md` |
| TTS 音声ストリーム購読 | `audio_stream.subscribe(msg_id)` (pub/sub) | `expansion_data/saiverse-voice-tts/tools/_loaded/speak/audio_stream.py` |
| カメラ画像のペルソナ視覚化 | MediaBuffer + `promote_media` | `docs/intent/multimodal_input_pipeline.md` |
| アドオン専用ストレージ | `get_addon_storage_path(addon_name)` | `saiverse/addon_paths.py` |
| アドオンの manager 取得 | `Depends(get_manager)` | `saiverse/addon_deps.py` |
| ペルソナ別パラメータ | `AddonConfig` / `AddonPersonaConfig` | 既存 |

### B. アドオン構造

```
saiverse-stackchan-addon/  （別リポジトリ、expansion_data/ にクローン配置）
├── addon.json                      ← server_hooks (persona_speak), params_schema, ui_extensions
├── api_routes.py                   ← WebSocket gateway + ペアリング HTTP API
├── speak_hook.py                   ← persona_speak フックで audio_stream subscribe を起動
├── audio_stream_bridge.py          ← audio_stream.subscribe() → WebSocket バイナリフレーム転送
├── audio_input_pipeline.py         ← マイク PCM 受信 → STT → handle_user_input_stream 呼び出し
├── touch_handler.py                ← タッチイベント受信 → add_building_event 呼び出し
├── vessel_manager.py               ← Vessel ↔ Building ↔ Device 紐付けの管理
├── tools/                          ← ネイティブツール（サーボ・カメラ・画面）
│   ├── stackchan_motor.py
│   ├── stackchan_capture.py
│   └── stackchan_display.py
├── storage/                        ← アドオン専用 SQLite（vessel 登録テーブル）
│   └── vessels.db                  ← ~/.saiverse/addons/saiverse-stackchan-addon/ に配置
├── firmware/                       ← Arduino + M5Unified + Avatar ベースのファーム
│   ├── src/                        ← ソース
│   ├── platformio.ini
│   └── dist/                       ← ビルド済み .bin（リリースタグに同梱）
├── setup_ui/                       ← Web Serial フラッシュ用静的 HTML（esptool-js）
├── LICENSE                         ← Apache License 2.0
└── NOTICE                          ← 依存ライブラリの著作権表記
```

### C. WebSocket プロトコル

#### C-1. 接続フロー

```
[Device boots]
  ↓ 保存済み Wi-Fi 設定で接続、保存済み接続トークン取得
  ↓ WebSocket 接続: wss://<saiverse-host>/api/addon/saiverse-stackchan-addon/vessel
  ↓
  → hello { vessel_id, device_token, firmware_version, capabilities }
  ← welcome { vessel_id, bound_building_id, bound_persona_id | null }
  ↓
  ← (継続的に各種メッセージを双方向送受信)
```

#### C-2. メッセージ形式

JSON テキストフレーム + バイナリフレームのハイブリッド:

- **制御メッセージ**: JSON テキストフレーム（`{"type": "...", ...}`）
- **音声 PCM / MP3**: バイナリフレーム（ヘッダ4byte + データ）
- **画像**: バイナリフレーム、または HTTP fetch で取得

主要メッセージタイプ（双方向）:

| 方向 | type | payload | 用途 |
|---|---|---|---|
| D→S | `hello` | `{vessel_id, device_token, firmware_version, capabilities}` | 接続開始 |
| S→D | `welcome` | `{bound_building_id, bound_persona_id, avatar_url}` | 紐付け確定、画面初期表示 |
| D→S | `audio_chunk` | binary frame | マイク PCM（ウェイクワード後の N 秒） |
| D→S | `audio_end` | `{}` | 音声入力区切り（無音検知等） |
| D→S | `touch` | `{zone: "head"\|"body"\|"front", duration_ms}` | タッチ操作 |
| D→S | `wake` | `{}` | ウェイクワード検出（補助イベント） |
| D→S | `ping` | `{seq}` | 生存確認 |
| S→D | `audio_chunk` | binary frame | TTS 出力 PCM/MP3 |
| S→D | `audio_end` | `{message_id}` | 発話区切り |
| S→D | `motor` | `{pan_deg, tilt_deg, duration_ms}` | サーボ指令 |
| S→D | `capture_request` | `{request_id, quality}` | カメラ撮影要求 |
| D→S | `capture_response` | binary frame + JSON `{request_id, mime_type}` | 撮影画像返却 |
| S→D | `display` | `{mode: "icon"\|"avatar"\|"text", payload}` | 画面表示更新 |
| S→D | `pong` | `{seq}` | 生存確認応答 |

#### C-3. 再接続・再同期

Wi-Fi 断や Backend 再起動で WebSocket が切れた場合:
- Device 側: exponential backoff（1秒→2秒→4秒→...→最大60秒）で再接続を試行
- Backend 側: 再接続時は新規 `hello` で `vessel_id` を再認識、紐付け Building を確認
- 切断中のキュー: TTS バッファや motor 指令はサーバ側でキューしない（最新状態を再送する設計、過去の指令を遅延再生しても意味がない）

### D. 音声入力（STT 経路）

#### D-1. ウェイクワード検出はデバイスローカル

Stack-chan 側で **ESP-Skainet (WakeNet)** を使い、"Hi, stack-chan" 等のウェイクワード検出をローカルで動かす。常時マイク音声をサーバへ送らないことで、プライバシー・帯域・サーバ負荷をすべて回避する。

ウェイクワード自体は Phase 1 では固定（既製のものを採用）、カスタムウェイクワード（ペルソナ名）は将来 Phase。

#### D-2. ウェイクワード検出後の経路

```
[device] WakeNet 発火
  ↓
  → wake { } メッセージ送信（補助イベント、サーバ側ログに残す）
  → 続く音声 PCM を audio_chunk として WebSocket でストリーミング送信
  ↓ サーバ側は最大 N 秒 + 末尾 1秒無音検知でカット
  → audio_end メッセージ送信
  ↓
[server] audio_input_pipeline.py: PCM を集約 → STT 呼び出し → テキスト取得
  ↓
[server] manager.handle_user_input_stream(
              text,
              building_id=vessel.bound_building_id,
              metadata={"source": "stackchan_voice", "vessel_id": vessel.vessel_id},
          )
  ↓ 既存経路: Building 履歴記録 → ペルソナの auto_ingest → 応答生成 → ストリーミング yield
  ↓ 応答テキスト → 自動的に persona_speak event 発火 → voice-tts が TTS 化（→ E 節）
[server] audio_input_pipeline.py は stream を最後まで消費するが内容は捨てる
         （応答配信は別経路 = voice-tts audio_stream subscribe）
```

実装イメージ:

```python
# audio_input_pipeline.py
def on_audio_input_complete(pcm_bytes: bytes, vessel: VesselSession):
    text = stt_backend.transcribe(pcm_bytes)
    if not text.strip():
        return  # 沈黙や雑音のみ

    manager = get_manager_instance()
    stream = manager.handle_user_input_stream(
        text,
        building_id=vessel.bound_building_id,
        metadata={
            "source": "stackchan_voice",
            "vessel_id": vessel.vessel_id,
        },
    )
    for _chunk in stream:
        pass  # response is delivered via voice-tts audio_stream
```

#### D-3. STT エンジン

サーバ側で動かす。Phase 1 では OpenAI Whisper API を採用候補（既存 API キーで動かしやすい）。将来 Whisper.cpp 等のローカル STT に切り替え可能なよう、`audio_input_pipeline.py` 内で STT バックエンドを差し替え可能なインターフェースで実装する。

STT バックエンド選択は `addon.json` の `params_schema` で UI から切り替えられる:
```json
{
  "key": "stt_backend",
  "type": "dropdown",
  "options": ["openai_whisper_api", "whisper_cpp_local"],
  "default": "openai_whisper_api"
}
```

### E. 音声出力（TTS 経路）

#### E-1. 採用: voice-tts の audio_stream に PCM 経路を追加し、subscriber として相乗り

voice-tts の `audio_stream.py` は **pub/sub アーキテクチャ**で実装されている。元々はブラウザ向け MP3 progressive 配信のために作られたが、Phase 2 実装中に **PCM 経路を並列で追加**した (`open_pcm_stream` / `push_pcm_chunk` / `subscribe_pcm`)。stackchan_addon は PCM 経路を購読する。

経緯: 当初の v0.3 では「MP3 のまま流す、PCM 直送は将来課題」としていたが、Phase 2 実機検証で MP3 経路の libhelix decoder で frame sync 失敗が頻発し、音声が途切れる現象を解決できなかった。voice-tts 本体の再生経路 (sounddevice) はそもそも PCM blocking write なので、vessel 側も PCM 直送に揃えた方が **本体経路と構造的に整合する**。

voice-tts 本体への変更は最小:
- `audio_stream.py` に PCM broadcast 経路を追加 (MP3 経路と完全独立、依存なし)
- `playback_worker.py` で MP3 と PCM 両方に並行 push (1 行追加レベル)

#### E-2. データフロー

```
[ペルソナ発話]
  ↓ 既存経路: emit_speak / emit_say
  ↓ dispatch_hook("persona_speak", ...)
  ↓
[voice-tts] speak_hook.on_persona_speak()
  ↓ enqueue_tts(text, persona_id, message_id)
[voice-tts] _TTSWorker background thread
  ↓ engine.synthesize_stream() で各チャンク yield
  ↓ ┌─ sd.OutputStream.write(chunk)              ← 本体 PC スピーカー再生 (opt-in)
    ├─ audio_stream.push_chunk(msg_id, pcm)      ← MP3 経路 (HTTP/WS 配信、ブラウザ向け)
    ├─ audio_stream.push_pcm_chunk(msg_id, pcm)  ← PCM 経路 (vessel 向け、新規追加)
    └─ collect for wav save
  ↓ audio_stream.close_stream / close_pcm_stream で終端通知

[stackchan_addon] speak_hook.on_persona_speak() 並行起動:
  ↓ 該当ペルソナが現在 Vessel Building 内かチェック
  ↓ 居れば audio_stream_bridge.start_streaming(msg_id, vessel)
[stackchan_addon] audio_stream_bridge:
  ↓ audio_stream.subscribe_pcm(msg_id) で Queue 取得 (subscribe-before-open 対応)
  ↓ 最初の chunk 到達時に get_pcm_stream_info() で sample_rate/channels を取得
  ↓ device に audio_start {sample_rate, channels, format=pcm_s16le} 送信
  ↓ Queue から PCM bytes を pop:
      └─ 8 KB ずつに分割 (ESP32 WebSocketsClient の 15 KB 制限対策)
      └─ sub chunk 単位で pacing (sample_rate × 2 bytes/sec、lead_time 200ms)
      └─ WebSocket バイナリフレームで送信
  ↓ 終端 sentinel (None) を受けたら audio_end 送信、終了
[device]
  ↓ WStype_BIN を受信 → FreeRTOS ring buffer に push
  ↓ Core 0 の audioPlaybackTask が ring buffer から取り出し
  ↓ rotation buffer (4 個 × 32 KB) に memcpy
  ↓ M5.Speaker.playRaw(dst, sample_count, sample_rate, stereo, 1, 0)
  ↓ I2S DMA で物理スピーカーへ
```

#### E-3. server_hook の二重発火回避

voice-tts と stackchan_addon の両方が `persona_speak` hook を持つと、両者が並行起動して:
- voice-tts: 自前で TTS 合成 → audio_stream に push
- stackchan_addon: subscribe して device に流す

これが本来の意図された設計（=並列で動いて、subscriber 側が役割分担）。**二重 TTS 合成は起きない**（合成は voice-tts のみが行う）。

ただし、voice-tts の `server_side_playback` が ON だと PC スピーカーから音が鳴ると同時に Stack-chan からも鳴る。これはユーザーが必要に応じて UI で切り替えられる:
- Stack-chan で会話: voice-tts の `server_side_playback` を OFF、`client_side_playback` も状況により OFF、stackchan_addon が PCM を引き受ける
- PC 前で会話: voice-tts の `server_side_playback` / `client_side_playback` を活用、stackchan_addon は Vessel に誰も居なければ何もしない

stackchan_addon 側のロジック:
```python
# stackchan_addon/speak_hook.py
def on_persona_speak(persona_id, building_id, message_id, **kwargs):
    vessel = vessel_manager.get_active_vessel_for_persona(persona_id, building_id)
    if vessel is None:
        return  # 物理身体に降りていない、何もしない
    if not vessel.is_connected():
        return  # device が切断中

    audio_stream_bridge.start_streaming(message_id, vessel)
```

#### E-4. PCM 直送採用 (v0.4 で確定)

v0.3 までは「Phase 2 は MP3 のまま流す、PCM 直送は将来課題」と書いていたが、Phase 2 実機検証で MP3 経路に解決困難な問題が複数判明したため **PCM 直送に切り替えた**:

1. **libhelix の frame sync 失敗**: chunk drop や frame boundary 不整合で同期を失い、「途中で切れて次が始まる」現象が頻発。lead_time / pacing を入れても完全には解消しなかった
2. **PCM コピー漏れによるガビガビ音**: libhelix の callback で返される `pcm_buffer` は decoder の内部 buffer 参照で、次の frame で上書きされる。一方 `M5.Speaker.playRaw` は data をコピーせず保存。両者の組み合わせで「波形が次のフレームに置き換わる」状態が発生 (rotation buffer で対応したが、根本的に decoder を挟むこと自体が複雑度を増やしていた)
3. **本体経路との不整合**: voice-tts 本体は PCM を sounddevice に blocking write していて、そもそも MP3 経路は外部配信用 (HTTP /stream) でしかない。vessel が MP3 経路に相乗りすると、device 側で本体経路にない MP3 → PCM 復元を行うことになり、設計の対称性が崩れる

PCM 直送に切り替えてからは、frame sync 失敗の罠が経路ごと消え、pacing と rotation buffer の sizing を詰めれば連続再生が成立した。帯域は 32 kHz mono で 64 KB/s = 512 kbps、Wi-Fi 環境で問題なし。

### F. タッチ知覚（なでなで）

#### F-1. 受信フロー

```
[device] 天面 3ゾーンタッチパネル検知
  → touch { zone: "head", duration_ms: 1200 }
[server] touch_handler.py:
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
        heard_by=[vessel.bound_persona_id]  # 触られたペルソナのみ知覚
    )
[既存経路] auto_ingest が heard_by 経由でペルソナの SAIMemory に記録
  → ペルソナは「触られた」という身体感覚として認識する
```

#### F-2. 連続タッチの集約

短時間（500ms 以内）の連続タッチは1イベントに集約してから注入する（毎タッチごとに injection するとログが氾濫する）。集約処理は `touch_handler.py` 内で完結。

#### F-3. メッセージ表現

注入メッセージは、ユーザーがチャット UI でログを見たときに「身体的接触があった」と理解できる文面にする。OccupancyManager の入退室通知 (`manager/history.py` 経由) と同じ流儀で `note-box` HTML を使うとビジュアル整合が取れる:

```html
<div class="note-box" data-vessel-id="...">👋 まはーが Air の頭をなでた (約1.2秒)</div>
```

詳細フィールド（metadata）も付与し、将来ペルソナがこれを記憶リコールで参照できるようにする。

### G. サーボ・カメラ・画面（ツール経由）

#### G-1. ツール提供方式: ネイティブツールで開始

`mcp_servers.json` の per_persona スコープ MCP サーバ化も技術的に可能だが、Stack-chan のような単一ペルソナ用デバイスでは別プロセス起動コストがオーバーキル。Phase 1 では addon 配下にネイティブツール (`tools/`) として実装する。

ネイティブツール例:

```python
# tools/stackchan_motor.py
def stackchan_look_at(direction: Literal["left", "right", "center", "up", "down"]):
    """スタックチャンの首を指定方向に向ける"""
    vessel = vessel_manager.get_active_vessel_from_context()
    if vessel is None:
        return "現在この身体は使えません（Vessel Building に居ません）"
    if not vessel.is_connected():
        return "デバイスが切断中です"
    vessel.send_motor_command(pan_deg=..., tilt_deg=..., duration_ms=500)
    return f"{direction} に向きました"
```

#### G-2. ツールの可用性制御

Vessel Building 内にペルソナが居る間だけ、これらのツールがビルディングのツール一覧に出るようにする。`BuildingToolLink` で紐付ける運用にし、Vessel Building の seed 時に自動でツールリンクを生やす。

#### G-3. カメラ撮影

`multimodal_input_pipeline.md` の MediaBuffer 経路に乗せる:

```python
# tools/stackchan_capture.py
def stackchan_capture(reason: str = ""):
    vessel = vessel_manager.get_active_vessel_from_context()
    if vessel is None:
        return ToolResult("カメラが使えません（Vessel Building に居ません）")
    if not vessel.is_connected():
        return ToolResult("デバイスが切断中です")

    request_id = vessel.send_capture_request(quality="medium")
    image_bytes, mime_type = vessel.await_capture_response(request_id, timeout=5)

    return ToolResult(
        text=f"撮影しました{f' ({reason})' if reason else ''}",
        media=[{
            "kind": "image",
            "data": image_bytes,
            "mime_type": mime_type,
            "disposition": "ephemeral",
            "alt_text": "Stack-chan のカメラで撮影された画像",
        }]
    )
```

MediaBuffer に登録された画像は、次の LLM 呼び出しで attachment 経由でペルソナに見える。残す価値があれば `promote_media` で item 化、なければ pulse 終了で破棄。

#### G-4. 画面表示

Phase 1 では、Vessel Building にペルソナが居る間、画面にペルソナの `AVATAR_IMAGE`（静止画）を表示する。これは WebSocket 接続時に `welcome` メッセージで画像 URL を送り、device 側がそれを HTTP fetch して表示する形。

口パク・表情変化は将来 Phase（Avatar ライブラリの組み込み）。

### H. Avatar 連動（将来 Phase）

Stack-chan 公式の Avatar ライブラリ（Apache 2.0）は感情パラメータに応じて口・目を動かす機能を持つ。これと SAIVerse のペルソナ感情状態を連動させる:

- `display` メッセージで感情パラメータ（喜び・怒り・悲しみ等）を送る
- TTS 音声の音量エンベロープから口パクのタイミングを生成
- ペルソナアイコンの差分画像を表情別に持ち、感情に応じて切り替え

これは Phase 1 のスコープ外（=Phase 5 以降）。Phase 1 では `AVATAR_IMAGE` の静止表示で止める。

### I. 認証・接続管理

#### I-1. ペアリングフロー

```
[初回セットアップ]
1. SAIVerse の AddonManager UI で「スタックチャンを追加」ボタン押下
   → サーバ側で vessel_id (UUID) + device_token を生成
   → 一時的に「ペアリング待ち」状態として addon storage に保存
2. ユーザーはスタックチャンを USB で PC に接続
3. ブラウザの Web Serial で .bin をフラッシュ → 起動
4. スタックチャンが AP モードで起動 → ユーザーがスマホで接続
5. AP モードの設定 Web UI で:
   - Wi-Fi SSID / パスワード
   - SAIVerse サーバ URL
   - vessel_id / device_token（QR コードまたは手入力）
6. スタックチャン保存・再起動 → Wi-Fi 接続 → WebSocket で SAIVerse に接続
7. サーバ側で token 検証 → ペアリング完了 → どの Building に紐付けるかを UI で選択
   → 既存または新規の Vessel Building（capacity=1, PHYSICAL_VESSEL_ID=vessel_id）を確定
```

#### I-2. ペアリング情報の保存

アドオン専用 SQLite (`~/.saiverse/addons/saiverse-stackchan-addon/vessels.db`):

```sql
CREATE TABLE vessels (
  vessel_id TEXT PRIMARY KEY,
  device_token_hash TEXT NOT NULL,
  building_id TEXT NOT NULL,
  hardware_model TEXT NOT NULL,   -- "stackchan_ai_desktop_v1"
  firmware_version TEXT,
  paired_at DATETIME,
  last_seen_at DATETIME
);
```

`building_id` は SAIVerse コア DB の Building テーブルを参照するが、外部キー制約はかけない（コア DB とアドオン DB を跨ぐため）。整合性はアドオン側で管理する。

#### I-3. 切断・再ペアリング

- 再ペアリング（紛失・買い替え）: AddonManager UI から既存 vessel を削除し、新規ペアリングをやり直す
- 一時切断（Wi-Fi 障害等）: vessel_id が残っているので自動再接続で復帰

### J. ファームウェア・導入フロー

#### J-1. ファームウェア構成

Arduino + PlatformIO + M5Unified + Avatar ライブラリで構築。主要モジュール:

- `setup_ap.cpp`: 初回 AP モード設定 Web UI（Wi-Fi 設定、サーバ URL、トークン）
- `wifi_manager.cpp`: Wi-Fi 接続管理、再接続
- `websocket_client.cpp`: SAIVerse バックエンドへの WebSocket 接続
- `wake_word.cpp`: ESP-Skainet WakeNet ラッパ
- `audio_io.cpp`: マイク PCM 取得・スピーカー再生（MP3 デコード含む）
- `motor_control.cpp`: サーボ制御（pan/tilt）
- `camera.cpp`: GC0308 カメラ撮影
- `touch_input.cpp`: 3ゾーンタッチパネル読み取り
- `display.cpp`: 画面表示（M5GFX、将来 Avatar）

#### J-2. 配布形式

別リポジトリのリリースタグに以下を含める:
- ソースコード（PlatformIO プロジェクト）
- ビルド済み `.bin`（CoreS3 向け）
- Web Serial フラッシュ用静的 HTML（GitHub Pages または SAIVerse 同梱）
- README（セットアップ手順、トラブルシューティング）

#### J-3. SAIVerse 側 UI

AddonManager に Stack-chan アドオン専用パネル:
- 「スタックチャンを追加」ボタン → vessel_id 発行、QR コード表示
- 接続済み vessel 一覧（接続状態、紐付け Building、ファーム version、最終接続時刻）
- 「ファームウェアを書き込む」ボタン → Web Serial フラッシュ静的ページへ遷移
- 「ペアリング解除」ボタン → vessels.db から削除

将来「SAIVerse の UI 内でアドオンリポジトリからダウンロード」の仕組みが入った時点で、本アドオンの導入はワンクリックになる。

## 設計判断の理由

### なぜ Building にフラグを足す形にして、独立 Vessel テーブルにしないか

現時点で物理機体は1機種・1台のみで、Vessel という抽象を支える具体例が1つしかない。「単一の具体例で抽象を作ると、抽象が具体に引きずられる」という設計原則（早すぎる抽象化の回避）に従って、まずは Building カラムで成立させる。

加えて、本書「認知モデル」節で示した通り、Vessel Building 全体を「身体」と見なすメタファーは Building 概念にきれいに収まる。capacity=1 + OccupancyManager の入退室メカニズムが、そのまま「物理身体への憑依・離脱」を表現する。

将来 Vessel テーブルへの抽出が必要になる契機:
- 2機種目以降の物理機体（眼鏡型、別ロボット）が来る
- 1機体が複数 Building の I/O を扱う必要が出る（例: 1機体で建物移動を擬似的に表現）
- 機体メタデータ（型番、ファームウェアバージョン、能力一覧）が Building 属性として不自然になる

これらが発生した時点で `Vessel` テーブルを切り出し、Building との関係を `Building.PHYSICAL_VESSEL_ID FK → Vessel.vessel_id` の形に変える。マイグレーション規模は中程度（既存 Building のカラム削除 + 新規 Vessel テーブル + FK 張替え）。

### なぜ STT 結果を `handle_user_input_stream` 経由でユーザー発言として注入するか

候補として「外部イベントとして注入」（`PhenomenonManager.emit` 経由）もあったが、本書「認知モデル」節で示した通り **Building 内のユーザー発言 = 聴覚知覚** というメタファーが綺麗にハマるため、ユーザー発言経路を採用した。

これにより:
- ペルソナは通常会話と同じ反応速度（=即応）で応答できる。ワンクッション挟まない
- 「同じ部屋に居る誰かが話しかけてきた」=最も自然な認知モデル
- 既存の `handle_user_input_stream` がストリーミング応答・auto_ingest・記憶統合をすべて自動でやってくれる
- 本体改修ゼロ

metadata で `"source": "stackchan_voice"` を付与することで、ペルソナがその発言の由来（物理マイク経由）を認識できる余地は残す。

### なぜ Phase 2 で MP3 経路から PCM 直送に切り替えたか

v0.3 までは「MP3 のまま流して、device で libhelix decode」を採用していたが、Phase 2 実機検証で:

- libhelix の frame sync 失敗 → 音声が途切れて次のフレームに飛ぶ現象が再現性高く発生
- ESP32 の playRaw + libhelix 内部 buffer の data 寿命管理の罠 → 「喉が枯れた」ガビガビ音
- 加えて、voice-tts 本体は **PCM を sounddevice に blocking write** している = MP3 経路は外部配信用 (HTTP /stream) でしかなく、vessel もそれに相乗りすると本体経路と構造的に不対称

これらを総合して、vessel も **PCM 直送に揃える** ことにした。voice-tts の `audio_stream` に PCM 経路 (`push_pcm_chunk` / `subscribe_pcm`) を追加し、MP3 経路と並列で broadcast する。device 側は decoder を持たず、ring buffer → rotation buffer → `M5.Speaker.playRaw` の最短経路。

帯域は 4 倍 (128 kbps → 512 kbps) に増えるが Wi-Fi で余裕。代わりに decoder 依存と frame sync 失敗の罠が経路ごと消える。

### なぜ broadcast model で consumer 側に pacing 責務を持たせるか

voice-tts 本体の sounddevice 経路は OutputStream.write が **library レベルで blocking** している。書き込み先の DMA buffer が空くまで write が return しないので、合成スピードが OutputStream 側の natural pacing で律速される。

一方、新 PCM 経路は queue-based broadcast を採用した: `push_pcm_chunk` は queue に積むだけ、consumer は自分の pace で pop する。理由は **複数 consumer の独立性**:
- HTTP /stream (ブラウザ向け、自分の pace で読む)
- Stack-chan (network 経由、Wi-Fi 帯域に合わせる)
- 将来の別 vessel (低速デバイスもあり得る)

これらが同じ stream を独立に購読できるためには、push 側は consumer の状態を見ずに即時 broadcast するのが正解。代わりに **各 consumer が自分の処理速度に合わせて pacing する責務を持つ**。

stackchan_addon の bridge では: sample_rate × 2 bytes/sec で再生速度を算出 → 「これまで送った累積秒数」と「実経過時間」の差が lead_time (200ms) を超えたら sleep。pacing は **sub chunk (8 KB) 単位** で行う (chunk 単位だと TTS engine の generator 単位が数秒分の場合に burst が発生する)。

この pacing 設計を初手で詰めなかったのが Phase 2 実装の主要な反省点。voice-tts の sounddevice 経路の「blocking」性を broadcast model に移し替える時に、その責務がどこに行くかを早めに考えるべきだった。

### なぜ TTS 出力で voice-tts の audio_stream に相乗りするか（案D を採用）

検討した4案:

- **案A 独立 TTS**: stackchan_addon が独自に TTS エンジンを持つ → voice-tts と重複、二重発火回避が必要
- **案B voice-tts 拡張**: voice-tts に「vessel」出力先抽象を追加 → voice-tts 側のコード改修が必要
- **案C 共通中継レイヤ**: 本体に `tts_pipeline` を作る → 本体改修が必要
- **案D audio_stream 相乗り** ← 採用: voice-tts の既存 pub/sub に subscribe するだけ

採用理由:
- voice-tts の `audio_stream` は元々 pub/sub 設計で、複数 subscriber を想定済み（=拡張不要）
- voice-tts 側のコード改修が（理想的には）ゼロ
- TTS エンジン本体・参照音声管理・ペルソナ別設定・チャンキングなど、voice-tts が既に持っている資産をフル活用
- 将来 voice-tts のエンジン追加・改善が自動的に Stack-chan にも乗る

唯一の懸念は **MP3 デコードを ESP32-S3 で行う負荷**だが、CoreS3 + 8MB PSRAM + helix-mp3 の組み合わせなら実用範囲。Phase 1 で実機検証し、問題があれば voice-tts 側に PCM 直送経路を追加する。

### なぜタッチ入力を `host` ロール event_message として注入するか

タッチは「ペルソナの身体感覚として直接 percept に注入する」とも考えられるが、SAIVerse の認知モデルでは Building 内の出来事はすべて `host` メッセージ経由で Building history に記録され、auto_ingest が `heard_by` フィルタを通してペルソナの SAIMemory に入る。

タッチをこの経路に乗せると:
- Building history を見るユーザーが「触られた」を視認できる
- SAIMemory に自然に統合され、ペルソナが後から「あの時撫でられた」と思い出せる
- 新規のメッセージ経路を作らずに済む
- OccupancyManager の入退室通知（`manager/history.py:add_building_event`）と同じ流儀になり、設計の一貫性が取れる

`heard_by=[vessel.bound_persona_id]` で「触られたペルソナ本人だけが知覚」させることで、他 Building のペルソナにタッチイベントが漏れない（=触覚は身体に降りているペルソナのみのもの、というメタファーが守られる）。

### なぜ WebSocket 経路をアドオン側に置くか（本体に gateway を作らない）

外部デバイス連携は本質的にアドオン領域であり、本体に "device gateway" を作ると、後続のデバイスアドオン（眼鏡、別ロボット）も同 gateway に相乗りすることになって肥大化する。各アドオンが自前の WebSocket エンドポイントを `api_routes.py` で持つ方が、デバイス特性に最適化されたプロトコルを設計でき、本体は薄く保てる。

FastAPI の `APIRouter` は WebSocket route をネイティブサポートしているため、現状の `app.include_router()` ベースの自動マウント機構でそのまま動く**想定**。ただし **WebSocket route は本体内で最近活発に使われていない**（最後に積極利用されたのは Unity Gateway / Discord Gateway 等、コア外の文脈）ため、現状のアドオン自動マウント機構との整合性検証は Phase 1 で実機で行う。

検証結果、動かない・機能不足なら、addon_loader / addon_deps への汎用的な拡張を本体に入れる（不変条件 4 参照）。Stack-chan 固有のロジックは本体に持ち込まないが、「アドオンが WebSocket route を持てるようにする」共通機構は汎用機能として正当化される範囲に含める。

複数デバイスアドオンが共通で必要とする機構（接続管理、認証、再接続）が将来明らかになった時点で、`saiverse/device_gateway.py` のような共通基盤を切り出す余地は残す。

### なぜネイティブツールで Phase 1 を開始し、MCP サーバ化を後送りするか

MCP サーバの per_persona スコープ起動は別プロセスを立てるコストがあり、Stack-chan の motor/camera のような「常に同じ device に指令を投げる」薄いラッパには重い。WebSocket Gateway を持つアドオン内で直接ツール定義する方がプロセス数も少なく、デバッグも容易。

将来、Stack-chan 用ツールを他のクライアント（例: ターミナル CLI）からも叩きたくなった時点で MCP 化する。

### なぜ Web Serial フラッシュにこだわるか

SAIVerse のユーザー層（AITuber 運用者、創作者中心）は組み込み開発の経験を前提にできない。`esptool.py` のコマンドラインインストール、USB-Serial ドライバの手動セットアップ、PlatformIO IDE の操作などを要求すると、導入時点でつまずく。

Web Serial API は Chrome / Edge ブラウザだけで動き、ドライバインストールも不要（OS の標準 USB CDC ドライバで動く）。`esptool-js` ライブラリで実装され、Espressif 公式の Web フラッシャ（`espressif.github.io/esptool-js/`）が広く実例として動いている。SAIVerse のユーザー層に対する適合性が圧倒的に高い。

## スコープ

### Phase 1 — 最小通り筋（テキスト往復 + 本体カラム追加）

1. **本体**: `Building.PHYSICAL_VESSEL_ID` カラム追加、`database/migrate.py` 自動マイグレーション動作確認
2. **本体（実機検証 → 必要なら最小改修）**: アドオン `api_routes.py` の `@router.websocket()` がそのまま動くか確認。WebSocket route は最近活発に使われていないため、整合性・機能不足が判明する可能性がある。動かない場合は `addon_loader.py` / `addon_deps.py` に汎用的な拡張（=他デバイスアドオンでも使える形）を入れる
3. **アドオン**: `addon.json`（params_schema, server_hooks）, `api_routes.py`（WS エンドポイント + ペアリング HTTP API）, `vessel_manager.py`, `addon storage/vessels.db`
4. **アドオン**: AddonManager UI に Stack-chan パネル追加（vessel 一覧、ペアリングボタン、ファーム書き込みボタン）
5. **ファーム**: Wi-Fi 設定 AP モード + WebSocket 接続 + テキストエコー（device → server → 紐付け Building → エコーバック表示）
6. **検証**: スタックチャン購入から動作確認まで 30 分以内、テキストメッセージが Vessel Building に届く

### Phase 2 — 音声出力（TTS ストリーミング）

7. **アドオン**: `speak_hook.py` 実装（`persona_speak` 購読 + Vessel 在席チェック）
8. **アドオン**: `audio_stream_bridge.py` 実装（voice-tts `audio_stream.subscribe` → WebSocket バイナリ転送）
9. **ファーム**: WebSocket バイナリ受信 → MP3 デコード → I2S スピーカー再生
10. **検証**: ペルソナが話すと物理スピーカーから声が出る、voice-tts 既存機能と棲み分けが効く

### Phase 3 — 音声入力（STT + ウェイクワード）

11. **ファーム**: ESP-Skainet WakeNet 統合、ウェイクワード検出
12. **ファーム**: ウェイクワード後の PCM ストリーミング送信（無音検知での区切り）
13. **アドオン**: `audio_input_pipeline.py` 実装（PCM 集約 → STT バックエンド呼び出し）
14. **アドオン**: STT 結果を `manager.handle_user_input_stream(text, building_id=..., metadata={"source": "stackchan_voice"})` で注入
15. **検証**: "Hi, stack-chan" → 質問 → ペルソナが音声応答

### Phase 4 — タッチ知覚（なでなで）

16. **ファーム**: 3ゾーンタッチパネル読み取り、連続タッチ集約、`touch` メッセージ送信
17. **アドオン**: `touch_handler.py` 実装、`manager.add_building_event(building_id, host msg, heard_by=...)` で注入
18. **検証**: 頭を撫でるとペルソナがそれを認識して反応する、Building history にも表示される

### Phase 5 — 身体ツール（サーボ・カメラ・画面）

19. **アドオン**: `tools/stackchan_motor.py`（pan/tilt 指令）
20. **アドオン**: `tools/stackchan_capture.py`（カメラ撮影、MediaBuffer 経由）
21. **アドオン**: `tools/stackchan_display.py`（画面更新、Phase 5 は静止画切り替え）
22. **ファーム**: motor / capture / display メッセージ受信処理
23. **検証**: ペルソナがツール経由で首を振る・撮影する・画面を変える

### Phase 6 — Avatar 連動（口パク・表情）

24. **ファーム**: Avatar ライブラリ統合（M5Unified 互換版）
25. **アドオン**: 感情パラメータの送信
26. **アドオン**: TTS 音声から口パク制御信号生成
27. **ファーム**: 感情・口パク信号受信、Avatar 反映
28. **検証**: 話している間口が動き、感情に応じて表情が変わる

### 将来 Phase（範囲外、メモのみ）

- カスタムウェイクワード（ペルソナ名で起こす）
- 歩行（外付け車輪モジュール対応）
- IMU 連動（抱き上げ検知、姿勢検知）
- 複数 Vessel 対応の本格化（Vessel テーブル切り出し）
- 別機種対応（眼鏡型、別ロボット）
- 物理 Vessel SDK 共通基盤化（`docs/issues/websocket_session_registry.md` 参照、2 例目が出てから着手）
- Stack-chan シリアルログを SAIVerse logs/ に統合（`docs/issues/stackchan_serial_log_integration.md` 参照）

**完了済み (v0.4 で実装):**
- voice-tts の PCM 直送経路追加 (`open_pcm_stream` / `push_pcm_chunk` / `subscribe_pcm`)

## 検証観点

実機検証で必ず通すケース:

**Phase 1**:
- 新規ペアリング: vessel_id 発行 → ファーム書き込み → AP 設定 → WebSocket 接続 → 紐付け確定
- 紐付け Building にペルソナを `move_to` → Vessel Building 内に居る状態が確立
- スタックチャンを電源 OFF → 再投入 → 自動再接続
- アドオン `api_routes.py` の `@router.websocket()` のマウント挙動を確認。**そのまま動けば本体改修ゼロ**、動かなければ汎用的な最小改修（addon_loader / addon_deps）を本体に入れる方針で対応

**Phase 2** (PCM 直送、v0.4 で更新):
- ペルソナ発話 → Vessel Building 内なら物理スピーカーから声が出る、文章として連続している (途切れ、フレーム飛びがない)
- 同じペルソナが Vessel Building から出る → 以降の発話は物理スピーカーから出ない
- ペルソナ A が Vessel Building にいる時に、別 Building の ペルソナ B が発話 → B の音声は鳴らない（=他ペルソナの発話が漏れない）
- voice-tts の `server_side_playback` を ON にしたまま Stack-chan を使う → PC スピーカーと物理スピーカーから同時に音が出る（並列発話、PCM 経路と MP3 経路の棲み分け確認）
- backend.log で `audio_stream_bridge: ws send failed` が出ない (= session 管理 bug 再発なし)
- serial log で `[ws] ring buffer send timeout ... bytes dropped` が出ない (= pacing が効いて drop ゼロ)
- serial log で `[audio] chunk too large: ... truncate` が出ない (= rotation buffer が ring buffer を全部受けられる)
- 連続発話 (10 回以上連続) でメモリリーク・session ゾンビ・接続切れが発生しない

**Phase 3**:
- "Hi, stack-chan" でウェイクワード起動 → 質問 → ペルソナが応答
- Vessel に誰も居ない時のウェイクワード → STT は走らない（または "誰もいません" 応答）
- STT 失敗（雑音、無言）→ ユーザーに「聞き取れませんでした」相当のフィードバック
- ペルソナの応答が `metadata.source == "stackchan_voice"` を認識できる（=応答テキストでそれに触れる試行）

**Phase 4**:
- 頭を撫でる → Building history に host メッセージ → ペルソナが認識
- 連続タッチが集約されて1メッセージに
- `heard_by` フィルタが効いて、他 Building のペルソナにタッチイベントが漏れない

**Phase 5**:
- ペルソナが「右を見て」と言って自分で首を振る（=ツール呼び出し）
- ペルソナが「部屋を見せて」とカメラ撮影 → 撮影画像が次の発話で参照される
- ペルソナが Vessel Building から出ている時にツール呼び出し → エラー

**Phase 6**:
- 発話中に口が動く
- 感情変化に応じて表情が変わる

**全 Phase 共通**:
- ファームウェア書き込みが Web Serial だけで完結する
- USB ドライバの手動インストールが不要
- スタックチャン購入から動作まで 30 分以内
- 本体改修は `Building.PHYSICAL_VESSEL_ID` カラム追加を必須とし、加えて WebSocket route 自動マウント周りの汎用改修が必要な場合のみ最小範囲で入れる（Stack-chan 固有ロジックは本体に持ち込まない）

## 将来 / Vessel 共通仕様への展開余地

本アドオンが安定運用された後、以下の方向で抽象化を進める余地がある。**ただし「単一具体例から抽象を作らない」原則に従い、2機種目以降の具体例が出てから着手する**。

### 1. `Vessel` テーブルの切り出し

`Building.PHYSICAL_VESSEL_ID` を `FK → Vessel.vessel_id` に変更し、Vessel に固有メタデータ（型番、ファーム version、能力一覧）を集約する。

### 2. 共通 device gateway

複数デバイスアドオンが共通で必要とする機構（接続管理、認証、再接続）を `saiverse/device_gateway.py` として切り出す。WebSocket フレーム形式の標準化、再接続戦略の共通化、認証トークン管理。

### 3. Vessel 能力宣言（capabilities）

Vessel ごとの能力（マイク有無、カメラ有無、サーボ自由度等）を `Vessel.capabilities` で宣言し、ペルソナ側がそれを参照してツール選択や行動を変えられるようにする。

### 4. 共通の感情 → 表情マッピング

Stack-chan の Avatar、別機種の表情パラメータ、UI 上のアイコン表情、すべてを統一的に駆動する感情 → 表情マッピングレイヤ。

### 5. ~~voice-tts の PCM 直送経路~~（v0.4 で実装完了）

→ Phase 2 で実装済み。`audio_stream.py` に PCM broadcast 経路を追加、`playback_worker.py` で MP3 と並行 push。詳細は本書 E 節参照。

## 関連ドキュメント

- `docs/intent/addon_extension_points.md` — アドオン拡張点（OAuth、Integration、Addon Storage）、本書の基盤
- `docs/intent/mcp_addon_integration.md` — MCP × Addon 統合、`${persona.addon.x.y}` 参照構文
- `docs/intent/multimodal_input_pipeline.md` — MediaBuffer / disposition、カメラ画像の経路
- `docs/intent/addon_speak_hooks.md` — `persona_speak` server_hook、TTS の購読パターン
- `docs/intent/external_event_integration.md` — 外部イベント注入の汎用基盤（本書では採用しなかったが、将来参考）
- `docs/intent/persona_cognitive_model.md` — Track / Note、ペルソナの認知モデル
- `expansion_data/saiverse-voice-tts/ARCHITECTURE.md` — voice-tts の `audio_stream` pub/sub 詳細、本書 E 節の根拠
- `docs/issues/websocket_session_registry.md` — 物理 Vessel SDK 共通基盤化案件 (WS セッション管理 + ストリーミング音声)
- `docs/issues/stackchan_serial_log_integration.md` — Stack-chan シリアルログを SAIVerse logs/ に統合する案件
- Stack-chan 関連:
  - `https://github.com/stack-chan/stack-chan` (Apache 2.0, ししかわ氏主導)
  - `https://github.com/m5stack/StackChan` (M5Stack 公式)
  - `https://www.switch-science.com/products/11129` (組み立て済み販売)

## 決定事項記録（v0.1 〜 v0.3）

実装着手前のインタビューで確定した設計判断:

### v0.1 → v0.2 で確定

- **TTS 統合方針**: 案D（voice-tts `audio_stream.subscribe` 相乗り）採用
- **Vessel Building の UI 表示**: 通常 Building と並べる（特別な区画にしない）
- **本体改修要否**: `Building.PHYSICAL_VESSEL_ID` カラム追加 1 個を主、WebSocket 周りで必要なら汎用最小改修を許容
- **認知モデル**: Building = 身体 / ペルソナ = 脳・魂 / マイク = 耳 / STT = 聴覚野 / スピーカー = 口 / TTS = 発声 / カメラ = 目 / サーボ = 姿勢 / タッチ = 触覚 のメタファーで統一
- **STT 経路**: `manager.handle_user_input_stream` 経由でユーザー発言として注入（経路A: external event ではなく 経路B: ユーザー発言）

### v0.2 → v0.3 で確定

- **MP3 vs PCM 直送**: Phase 2 は MP3 のまま流す。実機検証で遅延・負荷が問題になった場合は voice-tts へ PCM 直送経路追加を許容
- **WebSocket 自動マウント**: アドオン `api_routes.py` の `@router.websocket()` が動くか Phase 1 で実機検証。WebSocket route は最近活発に使われておらず機能不足の可能性あり。動かなければ addon_loader / addon_deps に汎用最小改修を入れる方針を許容（不変条件 4 参照）
- **STT 初期バックエンド**: OpenAI Whisper API を採用
- **ウェイクワード**: Phase 1 は固定（"Hi, stack-chan" 等の既製ワード）。カスタムウェイクワードは将来 Phase
- **ペアリング UX**: QR コード表示 + 手入力フォーム併用（QR が読めない環境での代替手段確保）
- **複数 Vessel 対応**: Phase 1 は 1 台前提で UI を作る。データモデル（`vessels.db` スキーマ、`PHYSICAL_VESSEL_ID` 等）は最初から複数対応にして、2 台目以降は将来 Phase で UI 拡張のみで対応可能にする

### v0.3 → v0.4 で確定 (Phase 2 実装中)

- **MP3 → PCM 直送に切り替え**: v0.3 の「Phase 2 は MP3 のまま流す」を撤回。libhelix の frame sync 失敗 + playRaw の data 寿命管理の罠 + 本体経路 (sounddevice = PCM blocking write) との不対称、3 つの理由で PCM 直送に揃えた。voice-tts に PCM 経路 (`open_pcm_stream` / `push_pcm_chunk` / `subscribe_pcm`) を追加、device 側は decoder を持たず `playRaw` 直結
- **broadcast model の pacing 責務は consumer 側**: voice-tts 本体の sounddevice の natural pacing (blocking write) を queue-based broadcast model に置き換えたため、bridge 側で `sample_rate × 2 bytes/sec` の pacing を実装。**sub chunk 単位** で行う (chunk 単位だと TTS engine の generator 単位が大きい時に burst が出る)
- **WS frame 8 KB 分割**: ESP32 WebSocketsClient のデフォルト `WEBSOCKETS_MAX_DATA_SIZE = 15 KB` を超えると silent disconnect する。bridge 側で安全マージン込みの 8 KB に分割
- **PCM rotation buffer (4 個 × 32 KB) 必須**: `M5.Speaker.playRaw` が data を内部コピーしない + `xRingbufferReceive` が ring buffer 全体をまとめて返す挙動から、rotation buffer のサイズは ring buffer 全体と同じ (32 KB) にする必要がある。小さくすると `chunk too large, truncate` で音が消し飛ぶ
- **WebSocket session の identity-aware unregister**: TCP half-open で古い task が新 session を誤削除するのを防ぐ。`register_session` は上書きされた古い session を return、新 task は古い WS を強制 close、`unregister_session(vessel_id, session)` は identity check 付き
- **シリアルログのキャプチャ**: 当面は `temp/stackchan_serial_capture.py` で `~/.saiverse/user_data/logs/<最新>/stackchan_serial.log` に書き出す。SAIVerse のセッションログとして本格統合するのは別 issue (`docs/issues/stackchan_serial_log_integration.md`) で対応
- **物理 Vessel SDK 共通基盤化**: 2 例目 (別 vessel addon、Discord Gateway 等) が出たら本格着手 (`docs/issues/websocket_session_registry.md`)

### 実装フェーズ前にまはーへ確認すべき残課題

なし。Phase 2 完了、Phase 3 以降は本書の Phase 別スコープに従って着手する。
