# Intent: Voice-TTS Playback Queue / Pulse-Aware Preempt

**ステータス**: Phase 1 / Phase 2 / Stack-chan 流量制御 すべて実装完了 + 実機検証済 (2026-05-15)

**関連**: `stackchan_avatar_pipeline.md` (= Stack-chan Vessel)、 `stackchan_vessel.md`、 `persona_cognitive_model.md` (= pulse 概念)

## 実装サマリ

| Phase | commit | ファイル |
|---|---|---|
| 1: voice-tts revert (sleep gate 撤去) | `abee140` (voice-tts) | `tools/speak/playback_worker.py` |
| 1: stackchan speak_hook FIFO wait | `72d1ba2` (stackchan-addon) | `speak_hook.py` |
| 1: frontend audio queue | `46849c4` (本体) | `frontend/src/lib/clientActions/playAudio.ts` |
| 2: voice-tts audio_ready に pulse_id | `9de5557` (voice-tts) | `tools/speak/playback_worker.py` + `speak_hook.py` |
| 2: frontend pulse_id 比較 preempt | `0c1a7af` (本体) | `frontend/src/lib/clientActions/playAudio.ts` |
| 2: stackchan pulse_id 比較 preempt | `7f9af54` (stackchan-addon) | `speak_hook.py` |
| 流量制御: chunk push 高水位/低水位 | `79bfdbf` (voice-tts) | `tools/speak/playback_worker.py` |

実機検証 (session 20260515_154058 系) で:
- 同 pulse 連発 (= web UI / Stack-chan 両方) で 1 つも切れずに順次再生
- 別 pulse 着信時 (= 別ペルソナ応答) で旧再生即中断 + 新再生即時開始
- Stack-chan 長尺発話で flow control が `lead=8.84s > 8.0s` 等で正常発動、 完走確認

## 背景

ペルソナの 1 pulse 内で複数 `persona_speak` イベントが発火するケース (= 例: スペル呼び出し直前の応答 + スペル結果後の応答) が観測される。 現状の voice-tts + stackchan-mcp gateway + SAIVerse web UI の経路では:

- voice-tts の `playback_worker` は単一 FIFO、 ジョブ間で realtime 待ちをしない (= synthesis 速度のまま次に進む)
- 各 `persona_speak` は独立した HTTP audio stream として subscriber (= web UI、 Stack-chan gateway) に届く
- web UI は新しい `audio_ready` event を受信したら即座に `<audio>` の src を差し替えて autoplay (= 前再生を切る)

= 結果として、 「2 つ目以降の発話 audio が届いた瞬間に 1 つ目の再生が途切れる」 = ユーザー体感「音声が途中で切れる」。

## 試行した経験則アプローチと放棄理由

1. **stackchan speak_hook の preempt 機構**: 旧 POST を中断して新 POST 開始 (= a19c430, 019d3c8)。 stack-chan 側の cut は止まったが web UI 側の cut は別レイヤなので解決せず。 さらに 「同 pulse 内連発」 と 「別 pulse での割り込み」 を区別できないため、 AI 自身の続き発話まで切ってしまう副作用
2. **voice-tts playback_worker での sleep gate**: synthesis 完了後に `audio_duration - synthesis_elapsed` 分 sleep して subscriber が再生し切るのを待つ (= e6148a6, b338325, 6d6377d)。 経験定数 margin で +0.2s / +2.0s と試したが、 subscriber の canplay startup delay が累積して 2-3 秒の不足が残った
3. **N 秒 pre-buffer 案**: server で N 秒分 chunk を貯めてから audio_ready emit。 数式が「margin の置き場所を移しただけ」 で本質的な経験則性は変わらず、 synthesis が遅い環境では別の不確定性 (= subscriber 側の playback stall) を持ち込むので却下

= **server だけで subscriber 側の再生タイミングを正確に推定するのは原理的に不可能**。 server が知らない情報 (= subscriber の canplay 閾値、 audio element の startup latency) を計算で出そうとする限り、 必ず経験定数が残る。

## 確定設計: subscriber 自身が pulse_id ベースで queue/preempt 判定

各 subscriber が独立に再生 queue を持ち、 新 `audio_ready` event を受け取った時に **pulse_id 比較** で挙動を決定する:

```
新 audio_ready 受信時:
  if 新 pulse_id == 現再生中の pulse_id:
    → queue 末尾に積む (= 同 pulse 連続発話、 順次再生)
  else:
    → queue を全廃棄 + 現再生を即中断 + 新発話を即再生
       (= 別 pulse、 ユーザー新ターン等の割り込み)

現再生 audio の `ended` event:
  → queue から次を pop して再生
```

この判定を **subscriber ごとに独立に**実装する設計 (= subscriber 間同期は不要):

- **web UI (Next.js frontend)**: `<audio>` element の queue
- **Stack-chan speak_hook**: gateway への POST queue (= 既存 `_ActivePostState` を pulse_id 込みに拡張)
- **voice-tts playback_worker**: synthesis job の queue (= 既存 FIFO 構造を pulse-aware abort 込みに拡張)

`pulse_id` は既に `persona_speak` event payload に含まれている (= 観測実証済、 session 20260515_002403 のログで `pulse_id=525267d0-...` を確認)。 `audio_ready` event にも load する必要あり (= 既存 event に field 追加で済む)。

## 不変条件

1. 同 pulse 内の連続発話は **全部** 順次再生される (= 1 つも切れない)
2. 別 pulse の発話到着時、 進行中 (および queue 内) の旧 pulse 発話は **即座に** 中断される
3. 各 subscriber の再生状態は subscriber 間で同期しない (= web UI と Stack-chan が瞬間的に異なる pulse を再生していてもバグではない)
4. 経験定数 (= margin、 K、 timeout 値等) を「正常系」 の判定に使わない (= 不発火フォールバック等の安全弁としてのみ使う)

## 段階的実装プラン

### Phase 1: 経験則機構を撤去 + 同 pulse 順次再生

目的: 現状の「音声途切れ」 を解消、 経験則コードを除去

**stackchan-mcp gateway** (= `temp/stackchan-mcp/`):
- 触らない (= gateway の `tts_lock` は per-call serialization で OK)

**voice-tts** (= `expansion_data/saiverse-voice-tts/`):
- `playback_worker._play_streaming` の sleep gate を撤去 (= 私が今セッションで入れた `time.sleep(remaining + 2.0)` の塊)
- `drain-diag:` の DEBUG log も撤去
- FIFO 単純化 (= ジョブを synthesis 完了次第順次処理、 自然な完了を待たない)

**stackchan addon speak_hook** (= `expansion_data/saiverse-stackchan-addon/`):
- 既存 preempt 機構 (= `_ActivePostState`、 `_preempt_and_register` 等) を「常時 preempt」 から「FIFO wait」 に変更
- 新 POST は旧 POST の完全完了を `state.completed.wait(timeout)` で待ってから開始
- abort_event を iterator に渡す経路は残す (= Phase 2 で再利用)

**SAIVerse frontend** (= `frontend/`):
- audio element の `ended` event listener を追加
- 再生 queue (`Array<{message_id, audio_url}>`) を実装
- 新 `audio_ready` event 受信時、 queue 末尾に push
- 現再生終了時 (`ended` event) に queue から pop して次を再生
- queue 内アイテムには pulse_id を保存 (= Phase 2 で使用)
- Error / timeout 安全弁: `<audio>` の `error` event でも次に進む、 audio_duration + 安全マージン経過で強制 dequeue

検証: 同 persona / 同 pulse での連続 2 発話で、 1 つも切れずに順次再生されること。 別 pulse 発話は (Phase 1 では) 単純に queue 末尾に積まれて旧 pulse 完了後に再生される (= preempt は未実装、 Phase 2 で対応)。

### Phase 2: pulse_id ベースの preempt 判定

目的: 別 pulse 着信時に旧 pulse を中断 (= ユーザー新ターンの即時応答性)

**SAIVerse event 配信**:
- `audio_ready` / `audio_completed` event payload に `pulse_id` を追加 (= 既に `persona_speak` には載っているので伝搬経路を整える)

**SAIVerse frontend**:
- 現再生 audio の pulse_id を state として保持
- 新 `audio_ready` 着信時に pulse_id 比較
- 同 pulse → Phase 1 通り queue 末尾
- 別 pulse → queue 全クリア + 現 audio を `pause()` & `src=""` で停止 + 新 audio 即再生

**stackchan speak_hook**:
- `_ActivePostState` に `pulse_id` field を追加
- 新 persona_speak で:
  - 既存 state あり、 同 pulse → 既存 state.completed.wait() で FIFO 順番待ち
  - 既存 state あり、 別 pulse → 既存の preempt 経路発動 (= abort_requested set + 待たずに即新 POST 開始)
  - 既存 state なし → そのまま新 POST

**voice-tts playback_worker**:
- ジョブ enqueue 時に「現ジョブ + queue 内全ジョブ」 の pulse_id を確認
- 別 pulse なら現ジョブの synthesize_stream を abort (= abort flag をループ内で check して chunk push 停止)、 queue を全クリア
- 同 pulse なら FIFO 末尾に積む
- 現ジョブ abort 時、 audio_stream は `close_stream` で即終了 → subscriber 側の Phase 2 ロジックで preempt 処理

### Phase 3: 不発火フォールバックの整備 + 観測

目的: 例外系で system が hang しないことを保証

**SAIVerse frontend**:
- `<audio>` の `error` event listener (= 破損 audio 等で `ended` が出ない場合)
- audio_duration + 5s の wall-time timeout (= 何らかの理由で `ended` 不発、 queue が進まなくなる回避)

**voice-tts**:
- abort flag 立て後、 synthesize_stream が `StopIteration` を返す保証が無いエンジンの場合用、 ループ側でも abort check
- engine 側 abort API があれば優先利用 (= GPT-SoVITS の場合は調査要)

**観測**: 1 セッション内で複数 pulse / 複数発話を含む対話を流して:
- 各 layer の queue depth ログ出力
- preempt 発動回数 / timeout 発動回数を log で確認
- 想定外の hang / leak を early-detect

## Stack-chan 流量制御 (= chunk push rate-limiting)

Phase 1/2 とは独立した、 voice-tts ↔ subscriber 間の **pipe 流量** の話。
2026-05-15 のセッション 131732 で観測した症状をきっかけに、 まはー の発案で
追加した。

### 観測された症状

session 20260515_131732 の `stackchan_room:177` (eris 発話、 同 pulse の 2
連発の 1 つ目) で:

- POST 開始 13:53:08 → **84.6 秒後 (13:54:32) に client 側 `TimeoutError(timed out)` で死亡**
- 続く 178 (同 pulse 2 連発の 2 つ目) も連鎖して 5.0 秒後に同型 TimeoutError
- gateway 側 (stackchan-mcp) は 177 の `ConnectionResetError` を 13:54:42 まで
  検出できず (= client timeout から +10s 遅れ)。 その間 `tts_lock` 残り、 178
  の POST request body が読まれず client write timeout で死亡

### 根本原因

GPT-SoVITS の GPU 推論は realtime より速い (= 1 秒の音声を 0.5 秒で生成可)。
voice-tts の chunk yield ループは合成完了次第 chunk を audio_stream に push する
ので、 subscriber (= speak_hook の HTTP POST → gateway → ESP32) が realtime で
しか消費できないと **buffer overflow → socket write block → write timeout**。

### 設計: 高水位 / 低水位 + α 補正

各 chunk push 直前に以下を計算:

```
total_sent_seconds = sum(chunk_seconds for chunk in collected)
elapsed = now() - first_chunk_at
actual_played ≈ elapsed - α    # α は subscriber 再生開始ラグ
lead = total_sent_seconds - actual_played
```

`lead` が高水位 A を超えたら、 低水位 B まで戻すよう sleep:

```
if lead > A:
    sleep(lead - B)
```

### 閾値の根拠 (Stack-chan 観測ベース)

room:177 の chunk 別 lead 推移:

| chunk# | elapsed | total_sent | lead |
|---|---|---|---|
| #20 | +5.92s | 2.48s | **−3.44s** (= 1st-pass 推論遅、 buffer 空っぽ) |
| #40 | +24.33s | 32.06s | **+7.73s** |
| #60 | +44.79s | 52.48s | **+7.69s** |
| #80 | +61.18s | 70.87s | **+9.69s** |
| #100 | +79.59s | 97.41s | **+17.82s** |
| timeout | +84.61s | ? | **約 18s で死亡** |

→ subscriber 全体 (gateway + ESP32 含む) の許容 buffer は **17 秒前後**。

| 閾値 | 値 | 根拠 |
|---|---|---|
| **A (高水位)** | **8.0s** | 観測死亡値 17s に対して半分以下 = 安全マージン 2x |
| **B (低水位)** | **3.0s** | #20 で -3.44s 観測 → realtime キャッチアップ遅れ吸収マージン |
| **α (subscriber 再生済み補正)** | **1.0s** | speak_hook の `_wait_first_chunk` + gateway → ESP32 forward + ESP32 内 buffer の startup 推測値。 実観測より小さく見積もって安全側に振る |

### 全 subscriber 統一値の理由

stack-chan は subscriber 中で最も buffer 容量が小さい想定 (= ESP32 内蔵 RAM)。
web UI (HTML5 audio) や他のクラウド連携 subscriber は通常もっと余裕がある
ので、 stack-chan 基準の保守値で全環境統一しても副作用なし。 subscriber 種別
ごとに閾値を分けるのは将来必要になったら検討 (= YAGNI)。

### 経過

- まはー の指摘 「N バイト push したら N バイト分の音声時間 sleep は雑、
  生成時間 + sleep 時間 > 再生時間 で必ず追い越されない」 → 高水位/低水位
  パターンに修正 (= 先行 buffer は維持しつつ過剰先行のみ抑制)
- intent doc 設計時の経験則アプローチ (= sleep gate) は **subscriber 側の
  ended timing を server で推定** が原理的不可能で却下されたが、 今回の流量
  制御は **物理的な buffer 容量で決定論的** なので別の話 (= 経験定数なし、
  観測データから算出)

## トレードオフと既知の限界

- subscriber 間同期しない設計のため、 web UI と Stack-chan が瞬間的に異なる pulse を再生する状態は「仕様」 (= ユーザー側に違和感を与えない範囲という前提)
- queue 上限を設けるかどうかは Phase 1 では決め打ちしない (= 通常想定で発話が無限に詰まることは無いはず、 必要なら Phase 3 で max-depth 入れる)
- voice-tts の synthesizer 側 abort API 不在の場合、 abort 後も GPU リソースは少しの間消費される (= chunk push を止めれば実害無し)

## 関連経過

実装してダメだった経験則アプローチの試行ログ:

- 2026-05-15 セッション 015816 (= +0.2s margin) : 23s/45.6s audio で cut 観測
- 2026-05-15 セッション 011917 (= +0.2s + stream is not None gate) : `server_side_playback=False` で sleep 経路に入らず、 観測値で 4 つの発話の synthesis 時間が audio duration の 1/4 〜 1/2、 cut 多発
- 2026-05-15 セッション 002403 (= 初期実装): msg 156 audio=83.2s に対し synthesis 完了 22.91s で audio_completed → cut 確定

詳細根拠は SAIVerse session log + 当該セッションの作業履歴 (= ハンドオフ doc) 参照。
