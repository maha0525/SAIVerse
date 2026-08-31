# Issue: MCP 再接続時の anyio キャンセルスコープ違反による無限スピン → GIL 飢餓で全体劣化

**ステータス**: 🟡 進行中 (根本原因特定済み・修正未着手)
**優先度**: high
**作成日**: 2026-05-24
**関連**: `tools/mcp_client.py`、`expansion_data/saiverse-voice-tts/tools/speak/playback_worker.py`、anyio

## 現象

eris_city_a の発話の音声合成が「同じ発言を無限に合成し続けている」ように見え、止まらなくなった (1メッセージの合成に数時間)。

実際には:
- 1メッセージ (`stackchan_room:505`) が streaming で **125 サブチャンク**に分割され、単一パルス由来で正常に enqueue/終端 (`is_final=True` は1回のみ) されていた。再生成ループではない。
- 各チャンクの GPT-SoVITS 合成が **異常に遅い** (本来 16〜20 it/s → 1〜3 it/s)。125チャンク × 激遅 = 数時間。
- **GPU 9% / VRAM 2GB(/24GB)、CPU も非逼迫**。計算資源は空いているのに遅い。

## 根本原因

**stackchan デバイスのタイムアウト → MCP 再接続 → anyio キャンセルスコープのクロスタスク違反 → `_deliver_cancellation` の無限スピン → GIL 飢餓**。

連鎖:
1. stackchan デバイスが無応答になり、MCP の `load_avatar_set` が 30 秒タイムアウト → クライアントが**再接続のため旧接続を teardown**。
2. teardown が **「キャンセルスコープを、入った時と別のタスクで抜けようとした」**:
   ```
   cleanup error: Attempted to exit cancel scope in a different task than it was entered in
   ```
   anyio のスコープが壊れ、キャンセルが正常完了できない状態になる。
3. anyio の `_deliver_cancellation` が `call_soon` で**自分を毎ループ再スケジュールし続けてスピン** (健全なら I/O 待ちで idle のはずのループが `active+gil`)。
4. MCP 専用イベントループスレッド (`SAIVerse-MCP`) が **1コアを焼き続け、GIL を握り続ける**。
5. GPT-SoVITS は **SAIVerse 本体と同一プロセス内のワーカースレッド** (`voice-tts-worker`) で動くため、AR デコード (`decode_next_token`、トークンごとに Python 処理 = GIL 必須) が **GIL 飢餓**に陥り、1トークンあたりが約10倍遅くなった。
6. 結果、音声合成が realtime の数十倍遅くなり「止まらない」状態に見えた。

スピンは 16:46 (デバイスタイムアウト→再接続) から発生し、発話終了後 (17:4x) も**継続中**だった。16:48 以降の合成が最初から遅かったのはこのスピンが先に始まっていたため。タイミングが一致する。

### なぜ普段は平気で、今回だけ暴走したか (重要・稀バグ)

「別タスクで cancel scope を抜こうとした」事象には**2つの版**があり、過去ログ全セッション (約10日・約75セッション) を調べて切り分けた:

- **無害版 (常時)**: `disconnect()` の try/except が捕捉する `[DEBUG] cleanup error: Attempted to exit cancel scope...`。**5/15 以降ほぼ毎セッション 1回出ていた**が、捕捉されてログされるだけで実害ゼロ。接続の片付け自体は完了していた。
- **暴走版 (今日のみ)**: 捕捉されず、**detached なバックグラウンドタスク内で未処理 `RuntimeError` として落ちた版**:
  ```
  future: <Task finished name='Task-442' coro=<async_generator_athrow ...> exception=RuntimeError
  RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
  ```
  `async_generator_athrow` = transport のストリーム (async generator) を閉じるための `.athrow()`。これは `grep async_generator_athrow` で**今日のセッション (20260524_162222) にしか存在しない** (過去全セッションでゼロ)。

つまり**根っこは同じ「接続を開いたのと別タスクで閉じる」構造**だが、普段はエラーが try/except に飲み込まれて無害。今回だけ、**デバイスが完全無応答 (30秒タイムアウト) になり、そのキャンセルが再接続の teardown と衝突**した結果、エラーが捕捉経路の外 (transport 内部の async generator 片付けタスク) に逃げ、宙ぶらりんの cancel scope を残した → `_deliver_cancellation` が永久スピン。

**= 低頻度・高ダメージの構造的地雷**。発生は稀 (悪条件のタイミング一致が必要) だが、踏むと再起動までバックエンド全体が劣化。引き金 (実機デバイスの応答断) は物理ハードでは日常的に起きるため「いつか踏む」類い。無害版が毎回出ている時点で危険な構造は常時露出している。

## 影響範囲 (stackchan 固有ではない)

- スピン機構は `tools/mcp_client.py` の**接続ライフサイクル (再接続/teardown) = 全 MCP サーバ共通のコード**にある。
- MCP アドオンは stackchan と **elyth** の2つが接続中。stackchan は「デバイスタイムアウトが再接続を誘発した」**引き金**にすぎず、elyth や将来の MCP でも再接続/切断時に同じスコープ違反 → 同じ無限スピンを踏みうる。
- 被害は TTS に限らない: スピンは本体プロセスの GIL/1コアを奪い続けるため、**API・pulse・他アドオンを含むバックエンド全体が劣化**し、再起動するまで止まらない。「デバイスが切れるとバックエンド全体が悪化し続ける」汎用の欠陥。

## 調査経緯 (どう判明したか)

事実確認のステップと、その過程で否定した仮説の記録 (同種調査の再現用):

1. **再生成ループを否定**: enqueue を集計 → 125 チャンクすべて単一パルス・単一 message_id、`is_final=True` は1回。チャンクのテキストもほぼ全てユニーク (= 1長文の細切れ、同一文の反復ではない)。
2. **「遅さ」が本体と特定**: 非ストリーミングフォールバック (フロー制御 sleep なし) が 4.26 秒の wav 合成に約2分。`engine.synthesize` 自体が realtime の数十倍遅い。
3. **GPU/CPU 競合を否定**: `nvidia-smi` で GPU 9%・VRAM 2GB/24GB。ユーザーのタスクマネージャー観測で CPU も非逼迫。モデル再ロードも1回のみ。フロー制御 sleep の発火は0回。→ 計算でもロードでも sleep でもなく「**待ち/飢餓**」と判断。
4. **py-spy でスレッドスタック取得**: `voice-tts-worker` が `decode_next_token` (AR デコードループ) に常駐。2サンプルで行が動く = ハングでなく低速進行。
5. **GIL 保持者を特定**: py-spy 8サンプル中6回、`active+gil` は `SAIVerse-MCP` スレッド。本体ループは idle。
6. **スピンの正体**: `SAIVerse-MCP` 最上位フレームを20連続サンプリング → `_deliver_cancellation` (anyio) が `_call_soon` で再スケジュールされ続けるビジースピンと判明。
7. **トークン数 vs 速度の切り分け** (ユーザー提供のコンソール): 遅いチャンクも `semantic_tokens [1,18]` 等で**正常に EOS** (1500 上限への暴走ではない)。一方 `it/s` が 1〜3 (正常時 16〜20)、`first_package_delay` 8.7s (正常 1.2s)。= EOS 失敗(A型)ではなく**1トークンあたり低速(B型)** = GIL 飢餓と確定。
8. **トリガー特定**: backend.log に 16:46 の `load_avatar_set` 30s タイムアウト → 再接続 → `Attempted to exit cancel scope in a different task than it was entered in`。スピンの起点と一致。
9. **共有層と確認**: MCP サーバは stackchan/elyth の2つ。スコープ違反は共通の接続ライフサイクルコード。
10. **無害版 vs 暴走版の切り分け** (「毎回壊れるはずでは?」という矛盾の解消): 全セッションを grep。`exit cancel scope in a different task` は 5/15 以降ほぼ毎日出ているが捕捉済み無害。一方 `async_generator_athrow` (未処理タスク版) は**今日のセッションにしか出ていない**。→ 構造は常時露出・普段は無害、今回だけ悪条件で捕捉外に逃げて暴走、と確定。

**注記**: GPT-SoVITS の推論詳細 (`n/1500` 進捗、`semantic_tokens shape`、`it/s`、`first_package_delay`) は **stdout のみに出力され backend.log には残らない**。当初これを見落とし content 起因の仮説 (URI 非除去/発話長/ref 不一致) に何度か寄ってしまった。これらはいずれもユーザーの「普段から同条件」という指摘で否定された。**推論レベルの一次情報は stdout (ライブターミナル) にしかない**点に注意。

## 修正方針

### A. 根本修正 (必須): MCP 接続ライフサイクルのキャンセルスコープを修正

- **原則**: anyio のキャンセルスコープ/タスクグループは**入ったタスクと同じタスクで抜ける**。現状は再接続の teardown が別タスク (`run_on_mcp_loop` / `run_coroutine_threadsafe` 経由等) から `__aexit__` 相当を呼び、スコープを破壊している疑いが濃い。
- **方向**: 各 MCP 接続を**単一の長命タスクが所有**し、`async with stdio_client(...) as ...: async with ClientSession(...) as session: <稼働>` をそのタスク内で完結させる。終了/再接続はそのタスクに**シグナル**を送って同一タスク内で `async with` を巻き戻す。外部タスクからスコープを抜かない。
- **次の一手**: `tools/mcp_client.py` の接続生成箇所と `reconnect_server` (1058行付近)、`_runner` (1540行付近) を読み、別タスクからスコープを抜いている箇所を特定する。
- これで「再接続/切断 → スコープ破壊 → 無限スピン」が消え、デバイス切断が全体劣化に波及しなくなる。

### B. 多重防御 (推奨): GPT-SoVITS TTS を別プロセス化

- 現状 TTS は本体と同一プロセスのスレッドで動くため、本体側の **どんな** GIL 占有 (今回の MCP スピンに限らず) でも飢える。
- TTS エンジンを**独立プロセス**に切り出し、本体とは「合成依頼 → 音声(wav/PCM)返却」だけを socket/queue でやりとりする境界にする。別プロセス = 別 GIL = OS が独立に CPU/GPU を割り当てるため、本体負荷に左右されない。
- A が原因をピンポイントで潰すのに対し、B は原因不問で TTS を守る構造的堅牢化。着手前に voice-tts アドオンの `ARCHITECTURE.md` と設計意図を確認し、プロセス境界とプロトコルを設計する。

**優先**: A が先 (発火条件がデバイス切断という日常イベントで、被害が本体全体に及ぶため)。B はその後の堅牢化。

## 関連リソース

- `tools/mcp_client.py`: `_runner` (≈1540)、`reconnect_server` (≈1058)、`run_on_mcp_loop` (≈1561)
- `expansion_data/saiverse-voice-tts/tools/speak/playback_worker.py`: 単一ワーカースレッド `_run` (≈817)、`_play_streaming` (≈499)、フロー制御 sleep (≈562)
- `expansion_data/saiverse-stackchan-addon/`、`expansion_data/saiverse-elyth-addon/`: MCP サーバ定義 (`mcp_servers.json`)
- anyio: `_deliver_cancellation` (`anyio/_backends/_asyncio.py`)、エラー文 `Attempted to exit cancel scope in a different task than it was entered in`
- 観測セッションログ: `~/.saiverse/user_data/logs/20260524_162222/backend.log`

## ログ

- 2026-05-24: 発生・調査・根本原因特定。py-spy による GIL 保持者特定と anyio スコープ違反の確認まで完了。
- 2026-05-24: 過去ログ全セッションを調査し「無害版 (常時) vs 未処理タスク版 (今日のみ・稀)」を切り分け。低頻度・高ダメージの構造的地雷と確定。修正 A (所有タスク方式) の実装に着手。
- 2026-05-24: **修正 A 実装完了**。`MCPConnection` を所有タスク方式に変更 (`tools/mcp_client.py`): `connect()` は専用の長命タスク `_run_connection()` を起動して ready を待つだけにし、transport/session の `async with` をその単一タスク内で開閉。`disconnect()` は `_exit_stack.aclose()` を自前で呼ばず、shutdown イベントを set して所有タスクの自己巻き戻しを待つ (15s タイムアウト→cancel フォールバック付き)。stdio/sse/streamable_http の3経路すべて統一。ruff・ast parse クリーン、既存 MCP テスト 21件パス。
  - **検証の限界**: 既存テストはマネージャ層をモックしており、所有タスク方式の anyio 実挙動 (クロスタスク回避) そのものは未カバー。真の検証には実 MCP サーバ＋切断誘発が必要 (= 本 issue の稀シナリオ自体)。所有タスク方式は「開いたタスクと同じタスクで閉じる」を構造的に保証するため、原理的にクロスタスク違反は起きない設計。
  - **残**: B (TTS 別プロセス化、多重防御) は未着手。A の anyio 挙動を直接検証する回帰テスト (偽 transport で cross-task 条件を再現) も未作成。
