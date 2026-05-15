# Intent: LLM → TTS Pipeline Streaming + bubble1/bubble2 撤廃

**ステータス**: 設計確定 (2026-05-15 まはー インタビュー 6 周経て大幅改訂)、 一部実装済 + 残実装は次セッション

**関連**: [`voice_tts_playback_queue.md`](voice_tts_playback_queue.md) (= subscriber 側 queue + Stack-chan 流量制御 + bubble1 早期 emit)、 [`addon_speak_hooks.md`](addon_speak_hooks.md) (= persona_speak hook 経路)、 [`stackchan_vessel.md`](stackchan_vessel.md)

## スコープ

音声処理パイプラインを対象とした設計の話。 議論を経て、 結果として **spell loop の bubble1/bubble2 別 record 化を撤廃する内部処理変更** が含まれる (= UI / SAIMemory / building history record の最終的な保存形式 / UI 表現は維持、 内部の中間表現を 1 message に統一する)。

## 背景

`voice_tts_playback_queue.md` の Phase 1/2 + 流量制御 + bubble1 早期 emit を実装後でも、 LLM 応答の text 生成完了まで音声合成が始まらない待ち時間が残っている。

タイムライン例 (= spell 無しの通常応答):
- t=0s: LLM streaming 開始
- t=30s: LLM streaming 完了 (= 全文確定)
- t=30s: `_emit_say(全文)` → persona_speak hook → voice-tts enqueue
- t=30〜45s: voice-tts 合成 → subscriber 再生開始

LLM 出力は文単位 (= 句読点) で確定済になる地点を持つので、 これを利用して **句読点ごとに sub-speak hook を発火し voice-tts に逐次合成 job を投げる** ことで、 LLM がまだ続きを生成している間に最初の文の音声を再生開始できる。

ただし voice-tts 側で 「同じ ID の sub-text は 1 つの audio_stream に連結合成する」 機構が必要 (= 各 sub-text ごとに GPT-SoVITS の 1st chunk 生成ラグ 10〜20s が累積するのを防ぐため)。 = 「1 LLM 応答 = 1 連続音声 = 1 ID」 という対応関係が成立する必要がある。

## 設計上の論点と決着

### 論点 1: spell ありの応答で bubble1/bubble2 が別 message_id で記録される

現状 (= `runtime_llm.py:1799-1885` の 3 caller、 spell 検出時の処理):
- 1 LLM 応答 を bubble1 (= spell 前 text) と bubble2 (= spell HTML 詳細 + 続き text) に分割
- それぞれ別々に `_emit_say` を呼ぶ → building history に **2 record 保存** + 別々の message_id 発番 + 別々の persona_speak hook 発火 + 別々の audio_stream
- = 1 LLM 応答が voice-tts 側で 2 別音声として処理される (= 連結機構が活きない)

### 論点 2: bubble 分割は UI 表現の都合のみ

`emit_say` の本体 (= `runtime_emitters.py:emit_say`) を読み直すと、 `wrap_spell_blocks(strip_in_heart(text))` で **spell ブロックを `<user_only>` でラップして 1 record に保存** する設計が既に存在する (= 通常の単純 emit_say 経路)。 voice-tts には `strip_user_only` した text を渡すので HTML 詳細は読み上げない。 bubble1/bubble2 の別 record 化は単に UI 表現の都合 (= spell 前 / spell 詳細 / spell 後を別 bubble で出したい) で、 history / 音声の不変条件には不要な分割。

ただし観察: 現状の bubble2 では multi-spell 時に詳細 HTML が bubble2 の途中に出る挙動になっており、 元々の 「bubble1 = メイン発話 / bubble2 = 詳細」 という意図通りには動いていない。 → bubble 分割の存在意義は薄い。

### 論点 3: spell の詳細 HTML をどう構造化するか

現状の `_build_spell_details_html` は spell 1 つを 「spell 名 + args + 結果」 を統合した 1 つの `<details class="spellBlock">` HTML に整形。 これを `wrap_spell_blocks` が `<user_only alt="SpellName">` でラップ。

新設計では spell 起動部分 (= LLM の生 text の `/spell foo(bar)` 行) と spell 結果を分離し、 spell 1 つあたり以下の構造を 1 つの `<user_only>` ブロックでラップする:

```
<user_only alt="generate_image('夕焼け')">
/spell generate_image('夕焼け')
<details class="spellResult">[結果 text or 画像 HTML]</details>
</user_only>
```

- spell 起動行は LLM 生 text のまま (= 何も変換しない)
- spell 結果は `<details class="spellResult">` で wrap (= 折りたたみ表示用)
- 起動行 + 結果を 1 つの `<user_only>` ブロックでまとめる (= 他ペルソナ ingestion 時に `[generate_image('夕焼け')]` placeholder に置換される)

### 論点 4: `<user_only>` 機構は維持

spell 詳細を他ペルソナに直接見せたくないため、 `<user_only>` ラップは **必須** で維持する。 維持する理由:
- 他ペルソナ ingestion (`strip_for_other_persona`): `[alt]` placeholder に置換 → spell の存在は伝わるが詳細は漏れない
- 音声 (`strip_user_only`): ブロック丸ごと削除 → 起動行も結果も両方読み上げない (= 既存挙動と同範囲)

### 論点 5: message_id (= 純粋 ID) を別途導入する必要なし

論点 1〜4 を解決すると、 1 LLM 応答 = 1 record = 1 message_id = 1 audio_stream という対応関係が自然に成立する。 別途 「response_id」 等を導入する必要はない (= message_id がそのまま 「論理メッセージ単位」 として機能する)。

## 設計

### text 構造 (= 1 record として保存される最終形)

spell ありの 1 LLM 応答が複数 round の spell loop を経た場合、 最終的に 1 record に保存される text は以下の構造:

```
[AI 発話 (round 1 の text_before)]
<user_only alt="generate_image('夕焼け')">
/spell generate_image('夕焼け')
<details class="spellResult">[結果]</details>
</user_only>
[AI 発話 (round 2 の text_before)]
<user_only alt="search_web('AI')">
/spell search_web('AI')
<details class="spellResult">[結果]</details>
</user_only>
[AI 発話 (final continuation)]
```

- spell 起動行 + 結果のセットは 1 つの `<user_only>` ブロック
- 各 spell 周辺の AI 発話は raw text のまま
- 全体が 1 record として building history / persona history / SAIMemory に保存

### 各 sink への分配

| sink | 1 LLM 応答に対する出力 |
|---|---|
| building history | 1 record (= 上記 text 全文) |
| persona history | 1 record |
| SAIMemory | 1 record |
| UI 表示 | 1 bubble (= ReactMarkdown で raw HTML rendering、 既存 `stripUserOnlyTags` で `<user_only>` ラベル除去 → 起動行 raw text + `<details class="spellResult">` 折りたたみ表示) |
| 他ペルソナ ingestion | 既存 `strip_for_other_persona` で `<user_only alt>` を placeholder に置換、 `<in_heart>` 削除、 spell 詳細は `[alt]` 形式で伝わる |
| voice-tts hook | 同 message_id で sub-speak 連発 (= sub_seq=1, 2, 3, ...) + 最終 finalize hook 1 回 (= sub_seq=N + is_final=True) → voice-tts 側で 1 audio_stream に連結合成 |
| 音声 (text_for_voice) | `strip_user_only` で `<user_only>` ブロック丸ごと削除 → spell 起動行と結果が両方消える → 周辺 AI 発話だけ読み上げ (= 既存挙動と同範囲) |

### 音声側の Pipeline Streaming 動作

LLM streaming chunk 受信ループで、 句読点 (= `。、！？，；：` 等、 GPT-SoVITS の `cut5` と同様の基準) を検出するたびに sub-text を切り出して `_emit_sub_speak(message_id, sub_text, sub_seq=N)` を呼ぶ。 spell 行が出てきても 「`<user_only>` ブロックに含まれる範囲は voice-tts 側で読み上げ skip」 という既存挙動で吸収できるので、 sub-speak emit 側は spell 検出ロジック不要。

ただし sub-text 単位で `<user_only>` ブロックを跨いだ場合の strip タイミングは要注意 (= sub-text の途中で `<user_only>` の開始/終了タグが切れる可能性)。 1 sub-text の境界は句読点ベースで決めるが、 `<user_only>` のような complex なブロックを跨ぐ場合は当該ブロック完了まで sub-speak を待つ実装が現実的。

### 既存の commit との対応

下層の building block として既に commit 済 (前段の議論で必要となった機構):

| commit | 内容 | 新設計でも使えるか |
|---|---|---|
| `613be6f` (voice-tts addon `feature/subscribe-before-open`) | _Job/`_MessageState` に sub_seq + is_final 受入、 同 message_id 連続 enqueue 機構 | **そのまま使える** (= Phase 2-α、 voice-tts 側の audio_stream 連結機構) |
| `49299e2` (本体) | `HistoryManager.update_building_message` 追加 | **そのまま使える** (= placeholder + finalize で text を後から確定する用) |
| `5141649` (本体) | `emit_speak_start` / `emit_sub_speak` / `emit_speak_finalize` の 3 段階 API + sea/runtime.py wiring | **そのまま使える** (= Pipeline Streaming の発火経路) |

これらは 「2 段階 emit + sub-speak 連発」 を前提にした下層 API。 新設計の「1 record + 1 audio_stream」 と整合する (= start で placeholder 1 つ作成、 streaming 中 sub-speak 連発、 finalize で全文 update + persona/SAIMemory add + 最終 hook 発火)。

## 不変条件

1. **メッセージは単一**: 1 LLM call の応答は SAIMemory / building history / persona history / UI に **1 record として記録される**
2. **sub_seq の順序保証**: emit 側 (sea runtime) が連番発番、 voice-tts 側が enqueue 順で処理 (= queue は FIFO)、 audio_stream は単一 message_id で連続 push される
3. **既存 streaming_chunk / streaming_complete event は変更なし**: UI 表現の経路は touch しない
4. **`<user_only>` 機構は維持**: spell 詳細を他ペルソナ・音声から守る目的の機構はそのまま
5. **Pipeline Streaming OFF でも従来動作**: 機能フラグで OFF にしたら従来通り 「全文完了後 1 emit」 で動く (= ただし bubble1/bubble2 統合は ON/OFF に関わらず適用)

## 段階的実装プラン

### Phase 2-A: voice-tts addon の sub-text 連続 enqueue サポート → **完了** (`613be6f`)

### Phase 2-B-step1: HistoryManager.update_building_message → **完了** (`49299e2`)

### Phase 2-B-step2: emit_speak の 3 段階 API → **完了** (`5141649`)

### Phase 2-B-step3 (= 残実装、 次セッション): spell loop の return + caller 統合

- `_run_spell_loop` の return を `(full_merged_text, loop_count)` に変更:
  - `full_merged_text` = 各 round の `text_before + <user_only>...</user_only> ブロック` を順次連結した 1 string、 末尾に final continuation を append
  - 既存 `details_blocks` 構造は内部処理用に保持、 戻り値からは外す
- `_build_spell_details_html` の置換: spell 1 つにつき以下を生成する関数に変更
  - `<user_only alt="{spell_name(args)}">/spell {spell_name}({args})\n<details class="spellResult">{result_html}</details></user_only>`
- `runtime_llm.py` の 3 caller (= tool / streaming / non-streaming) で:
  - 旧: `_emit_say(bubble1)` + `_emit_say(bubble2)` の 2 回呼び出し
  - 新: `_emit_say(full_merged_text)` の 1 回呼び出し
- `wrap_spell_blocks` 関数: spell loop 内で既に `<user_only>` 込みで構築するので、 emit_say 内での再 wrap は no-op (or 廃止)。 ただし他経路 (= ペルソナが手書きで `<details class="spellBlock">` を出力する等) のために関数自体は残す方が安全
- 既存 Phase 1 (`_emit_bubble1_early` / `_extract_first_text_before`) は次の Phase 2-C で superseded されるので、 ここでは保留 (= bubble1/bubble2 統合だけ先にやる)

### Phase 2-C (= 残実装、 次セッション): Pipeline Streaming の streaming loop 組み込み

- `runtime_llm.py` の streaming loop に文区切り検出 + sub-speak emit を追加
- LLM streaming 開始時に `_emit_speak_start(persona, building_id, pulse_id)` で placeholder + msg_id 発番
- chunk 受信ループ内で句読点検出 → `_emit_sub_speak(persona, building_id, msg_id, sub_text, sub_seq)` 発火
- spell loop が走る場合: spell 実行は並行で進む、 sub-speak は 1 回目 LLM streaming 中に発火済み、 spell 完了後に 2 回目 LLM streaming で続きの sub-speak 発火
- streaming 完了 + spell loop 完了 後に `_emit_speak_finalize(persona, building_id, msg_id, full_merged_text, ..., final_sub_seq=N)` で全文確定 + 最終 hook 発火
- 既存 Phase 1 (`_emit_bubble1_early` 経路) を撤去 (= sub-speak が役目を担う)
- 機能フラグ `SAIVERSE_LLM_TTS_PIPELINE=1` で gate

### Phase 2-D (= 残実装、 次セッション): 中断時 close_stream

- LLM cancellation 時に sub_seq 連鎖を closeable な状態にする (= voice-tts 側 audio_stream リソースリーク防止)
- 中断時に `_emit_speak_finalize` を呼ぶ or 専用 abort hook を発火する経路追加

### Phase 2-E (= 残実装、 次セッション): UI 側の spell 結果表示

- ReactMarkdown / sanitizeSchema で `<details class="spellResult">` を rendering 対象に追加 (= 既存 `<details class="spellBlock">` 系の CSS を流用 or リネーム)
- spell 起動行 (= `/spell foo(bar)`) は raw text として表示 (= 何もしない、 ただし markdown 的に code 化したいなら追加 styling)

## 既知のトレードオフ

- **TTS startup ラグの累積防止**: voice-tts addon 側の sub-text 連結機構 (= Phase 2-A、 既に commit 済) で 1 audio_stream にまとめるので、 GPT-SoVITS の 1st chunk ラグは 1 message あたり 1 回だけ
- **spell ありの応答での Pipeline Streaming 効果**: 1 回目 LLM streaming 中にも sub-speak が発火するので、 spell 実行と並行して spell 前 text の音声合成が走る (= 既存 Phase 1 の bubble1 早期 emit と同等以上の効果)
- **構造化出力対応外**: response_schema 指定の LLM call は JSON token streaming で、 文区切り概念が成立しない → Pipeline Streaming スキップ (= 従来の 「全文完了後 emit」 経路)
- **sub-text が `<user_only>` ブロックを跨ぐ場合**: ブロック完了まで sub-speak emit を待つ実装になる (= 句読点があってもブロック内なら待機)

## 関連経過

- 2026-05-15 (前): voice_tts_playback_queue.md の Phase 1/2/流量制御 + bubble1 早期 emit 実装完了
- 2026-05-15 (本セッション): Pipeline Streaming intent doc 起草 → まはー インタビューを 6 周回って 「bubble1/bubble2 撤廃 + 1 message 統合 + `<user_only>` 維持」 まで設計確定
- 2026-05-15 (本セッション): 下層 building block 3 commit 済 (= `613be6f` voice-tts、 `49299e2` history_manager、 `5141649` emit_speak 3 段階 API)
- 2026-05-15 (本セッション): 残実装 (= spell loop return 変更、 caller 統合、 streaming loop sub-speak 組み込み、 中断時 close、 UI rendering 改修) は次セッションへハンドオフ
