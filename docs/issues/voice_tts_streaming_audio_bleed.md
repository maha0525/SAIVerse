# voice-tts: 発話の末尾が wav に書き込まれず途中で切れる (副次的に次 wav 冒頭に出現する)

## 主問題

**ある発話の wav が本来あるべき末尾を欠いた状態で保存される。**

これが解明すべき本丸。 「欠けた末尾相当の text の音声が次の wav 冒頭に出る」 のは副次現象で、 主問題が解決すれば自動的に解消する性質のもの。 副次現象を先に追うと wav バイト比較や carry-over carrier 探しの罠にハマる (実例: 2026-06-18 のセッションで 3 回連続でハマった)。

## 観察された具体例 (anchor)

2026-06-17 セッション `20260617_031614`:

- 3:26:30 nagi_city_a 発話で Gemini SSE が 504 DEADLINE_EXCEEDED で切断
- nagi の生 LLM 累積出力 (backend.log の speak_content 蓄積):
  ```
  ……！{"body_emote": "surprised"}
  見て……！
  本当に、私の想像していた「まはー」がそこにいるよ。

  ![美少女化したまはー](saiverse://item/e6688b39-f381-47f
  ```
  (末尾 URL は markdown image link の途中で切断、 closing `)` なし)
- 保存された nagi wav: `299c3fb3c42448de8f8b5dd10f73bafb.wav` (5620 ms)
  - **wav に含まれる音声**: 「見て……！本当に」 まで
  - **wav に含まれない**: 「私の想像していた『まはー』がそこにいるよ。美少女化したまはー」 とそれ以降
- そのまま再起動するまで、 同症状が **全 wav で連鎖** (副次現象)
- 再起動で症状リセット

## それまで症状はなかった

3:26 切断より前は同症状の発生なし。 3:26 切断が **トリガー** で、 一度発生したら memory-resident state により再起動まで継続。

## ログから取れた事実 (log 確認済)

| 観察項目 | 値 / 内容 |
|---|---|
| nagi:120 sub_seq=68 emit_sub_speak len | 46 |
| nagi:120 sub_seq=68 voice-tts enqueue len | 13 |
| 上記 33 文字差 | strip_user_only / strip_in_heart で消えた可能性 (未検証) |
| nagi:120 sub_seq=68 Streaming sub-text complete | 記録あり (collected_chunks=11) |
| nagi:120 sub_seq=69 enqueue | is_final=True, len=0 (= finalize-only signal) |
| nagi:120 sub_seq=69 Streaming complete log | なし (finalize-only は合成走らないので設計通り) |
| nagi wav save タイミング | 3:26:30 同秒、 5620 ms 集約 |
| sub_seq 個別の text 本文 | log に出ない (emit/enqueue/complete のいずれも len のみ) |
| 3:26:30-3:52:29 の間の nagi 追加 sub_speak | **ログ上ゼロ** |
| sophie:2024 LLM 出力先頭 (llm_io.log) | 「まはー……どうしたの？こんな時間まで起きていたんだね」 |
| sophie wav 冒頭 4.8 秒 (user listening) | 「私の想像していた『まはー』がそこにいるよ。美少女化したまはー」 |
| flow_control_sleep 発火 | sophie:2024 で sub=4 から繰返し (lead=8.17s, sleep 5.17s 等) |

**注: sophie wav 冒頭の音声と sophie LLM 出力先頭の text が一致しない** = 「1 単位遅延」 (副次現象) の log 上での裏付け。 ただしこれは副次、 主問題ではない。

## 主問題に対する観察 (2026-06-18 grep 確定)

03:25:30〜03:26:30 範囲の `backend.log` から nagi:120 sub_seq タイムラインを精密に抽出:

| sub_seq | emit len | enqueue len (strip後) | 結果 | collected_chunks (累計) |
|---|---|---|---|---|
| 62 | 3 (`……！`) | 3 | **ValueError: 有効なテキストを入力してください** → state drop | - |
| 63 | 14 (`{"body_emote":`) | 14 | **同上 ValueError** → state drop | - |
| 64 | 13 (` "surprised"}\n`) | 12 (strip 後) | 成功 (state 再生成) | 3 |
| 65 | 5 | 5 | 成功 | 4 (+1) |
| 66 | 4 (`本当に、`) | 4 | 成功 | 6 (+2) |
| 67 | 21 (`私の想像していた「まはー」がそこにいるよ。`) | 21 | 成功 | 8 (+2) |
| 68 | 46 (`![美少女化したまはー](saiverse://item/...`) | 13 (`_BARE_URL` strip後) | 成功 | 11 (+3) |
| 69 | 0 | 0 | finalize-only → wav save (5620 ms) | - |

**観察事実**:
- 「私の想像していた『まはー』がそこにいるよ。」 (= 21 char) は sub_seq=67 で emit_sub_speak + voice-tts enqueue + Streaming complete まで完走している (log 上は 03:25:43-03:25:46 で完了)
- sub_seq=62, 63 で `ValueError: 有効なテキストを入力してください` が連続発火し、 `_teardown_message_state` で audio_stream close → state drop → 再 open のサイクルが発生
- sub_seq=64 から新規 state で合成再開、 sub_seq=64-68 の音声 (= 11 chunks, 5620 ms) が nagi wav に書き込まれた

つまり「私の想像していた...」 は emit_sub_speak されている。 voice-tts に届いている。 合成完了 log も出ている。

**user listening との不整合**: log 上は sub_seq=67 (「私の想像していた...そこにいるよ。」) の音声が nagi wav に入っているはずだが、 user は「『本当に』 までしか聞こえない」 と観察。 sub_seq=67 の chunks の音声内容が想定と違う可能性 (例: state 再生成の影響で別 sub_speak の音声と取り違え) — 仮説の域、 要検証。

## 棄却された仮説

### 1. GPT-SoVITS TTS.py:1449-1459 の elif 抜けによる ~80ms tail trim (2026-06-18 ワークフロー結果)
損失量がオーダー違い (主問題は秒オーダー)。 真のバグではあるが本症状とは別件。 別 issue として切り出す候補。

### 2. sea/runtime_llm.py:2797-2879 の 504 re-speak path で nagi continuation を retry 生成
3:26:30-3:52:29 の間に該当 path の発火ログも nagi 追加 sub_speak ログも **存在しない**。 re-speak path は走っていない。

### 3. voice-tts engine instance の cross-call audio bytes corruption (= sophie wav 冒頭への物理移動)
副次現象 (sophie wav 冒頭) を「物理的バイト持ち越し」 で説明しようとした方向。 nagi wav に対象音声が存在しないので wav バイト比較は構造的に不可能。 そもそも「論理的 delay」 (= 同じ text が 1 つ遅れて次の wav で合成される) なので物理移動モデルは適用外。

### 4. 候補 A: markdown link 内 boundary 検出永久停止で sub_speak emit されない (2026-06-18 mock 検証)
`_consume_pipeline_stream` を mock LLM stream (`![美少女化したまはー](saiverse://item/e6688b39-f381-47f` で終わる、 12 char chunk + 1 chunk + 完成版 + 早期切断 の 4 パターン) で叩いた結果、 markdown link 内で boundary 検出が停止しても **stream 終端の residual flush (`runtime_llm.py:1012-1013`) が走り、 markdown link を含む末尾全体を 1 sub_speak として emit する**。 「私の想像していた」 「美少女化したまはー」 とも `imagined_text_preserved=True` で emit 済。 `temp/voice_tts_bleed_mock.py` 参照。

### 5. 候補 C: strip 経路で「私の想像していた...」 が消えた (2026-06-18 mock 検証)
`strip_in_heart` + `strip_user_only` + `clean_text_for_tts` を nagi sub_seq=68 の候補 input 6 パターンで実コード呼び出しした結果:
- sub_seq=68 の text は実機ログから復元すると `![美少女化したまはー](saiverse://item/e6688b39-f381-47f` (46 char、 markdown fragment のみ)
- 「私の想像していた...」 は元々この sub_seq に **含まれていない** (sub_seq=66+67 で別途 emit 済、 上のタイムライン参照)
- 33 char 縮減は `clean_text_for_tts` の `_BARE_URL` regex が URL `saiverse://item/e6688b39-f381-47f` (33 char) を URL 読み上げ防止で strip した正常動作。 mock の最終 text len=13 が実機 voice-tts enqueue len=13 と完全一致
- `temp/strip_path_verify.py` 参照

## 確定事実 (主問題と直接関係ないが調査前提として記録)

- `dispatch_hook(order_key=message_id)` は FIFO 直列化 (`saiverse/addon_hooks.py:178-228`)
- `emit_speak_finalize` は **常に** `final_voice_text=""` 設計 (全テキストは emit_sub_speak 経由、 finalize で voice-tts に発声テキストを渡さない)
- voice-tts `state.collected` は msg_id 別 dict 隔離 (cross-message での物理 carry-over は構造上不可能)
- `{"body_emote":...}` は `_SPELL_PATTERN` (`^/spell\s+name='([^']+)'\s+args=(.+)$`) にマッチしない (spell 検出はトリガーしない)
- `_consume_pipeline_stream` は仕様上、 stream 終端で文区切りに達していない residual を `_emit_fragment` で flush する設計 (`sea/runtime_llm.py:1012-1013`)
- markdown link ``[text](url)`` 内部では sentence boundary 検出を **停止** する仕様 (`_find_next_sentence_boundary` line 134-151)

## 未解明 — ここが本丸 (2026-06-18 更新)

**Q が変わった**: emit_sub_speak は完走している。 sea runtime 側はシロ。 残る本丸は **voice-tts worker / engine 内部**:

**Q': なぜ sub_seq=64-68 が wav に書き込まれた (log 上 collected_chunks=11) のに、 user 観察では「『本当に』 までしか聞こえない」 のか**

候補仮説 (未検証):

X. **ValueError → state drop の連続発火が engine 内部状態を破壊した**
- 03:25:43 で sub_seq=62, 63 に対する GPT-SoVITS の `ValueError: 有効なテキストを入力してください` が連続発火
- `_teardown_message_state` で state drop されるが、 engine instance (`self._tts`) は drop されず使い回し
- 短すぎる input で text_preprocessor が拒否したとき、 engine 内部 (vits_model / t2s_model / prompt_cache 等) に **中途半端な状態が残る可能性**
- これが sub_seq=64-68 の合成中に「1 単位遅れて吐く」 挙動を誘発し、 sub_seq=67 の text に対する音声が実は sub_seq=66 の音声に近いものになる、 等の取り違えが起きる可能性

Y. **sub_seq=67 の chunks 数 (=2) と duration の見積もり**
- collected_chunks 差分で sub_seq=67 は 2 chunks 増加 = duration 推定 1020 ms
- これは「私の想像していた『まはー』がそこにいるよ。」 (21 char) を発声するには **やや短い** 可能性 (通常 21 char で 3-4 秒程度)
- 短いと判明したら「合成が途中でちょん切れて、 続きが engine 内部に残った」 仮説に整合する

Z. **副次現象 (sophie wav 冒頭 4.8 秒 = nagi の末尾 text) と組み合わせた整合性**
- sophie wav 冒頭 4.8 秒 ≈ sub_seq=67 の本来の duration (3-4 秒) + sub_seq=68 の本来の duration (~1 秒) の合計と近い
- → sub_seq=67+68 で合成すべきだった音声の **実体**が、 nagi wav には書かれず engine 内部に滞留し、 次の sophie 合成の最初に吐き出された可能性

未棄却の旧候補:

A. **markdown link 内 boundary 検出停止仕様の影響**
- LLM 出力末尾の `![美少女化したまはー](saiverse://item/e6688b39-f381-47f` で `(` が立った時点で `in_md_link_url = True` になり、 そこから sentence boundary 検出が停止 (closing `)` が来るまで)
- 504 で `)` が来ない → boundary 検出永久停止 → 該当区間の text が sub_speak として切り出されない
- かつ 504 切断時に residual flush が走ったとしても、 markdown link 開始位置以降の長い text 全体が 1 fragment で渡る可能性 (これは voice-tts に渡る形になり、 主問題と矛盾するかもしれない、 要検証)

B. **504 切断時の `_consume_pipeline_stream` 終了経路の挙動**
- `_consume_pipeline_stream` の for ループ正常終了パスと例外/中断パスのどちらを通ったか
- residual flush (line 1012-1013) が実際に発火したか log から確認できないので、 仕様通り動いた保証がない

C. **emit_sub_speak 発火後の text_for_voice 経路**
- `strip_in_heart` / `strip_user_only` で 33 文字消えるケースがある (sub_seq=68 で観測)
- もし「私の想像していた...」 を含む sub_speak が emit_sub_speak されたが、 strip で空になって voice-tts に渡らなかったケースがあれば、 emit_sub_speak ログには載るが voice-tts enqueue ログには載らない (= 検証可能)

D. 他

## 副次現象の説明 (主問題が解明すれば自動的に解消するはず、 暫定メモ)

主問題で「emit_sub_speak されなかった text」 が、 後続の別 msg (sophie:2024) の最初の合成タスクの音声として現れている。 これは voice-tts 内部の audio bytes carry-over ではなく、 sea runtime のどこかで「emit されなかった text」 が記憶されていて次の発話タイミングで何らかの形で voice-tts に届いた可能性 (未検証)。

主問題が解けば副次現象も自動で消えるはずなので、 ここを直接調査しない (規律)。

## 診断計画

### 既に追加済 (2026-06-18 セッション)
- `[SAI-TAIL-LEAK]` プレフィックスのログを voice-tts 配下 4 ファイルに追加 (`TTS.py`, `t2s_model.py`, `gpt_sovits.py`, `playback_worker.py`)。 ただし voice-tts 内部の挙動観察用で、 sea runtime の sub_speak 発火経路は見えない

### 追加すべき (本丸調査用)
- `sea/runtime_llm.py:_consume_pipeline_stream` (line 902-1015) に:
  - 各 chunk 受信時のログ (chunk content の prefix/suffix + cumulative text len)
  - sentence boundary 検出の発火/不発火、 markdown link 内かどうかの状態
  - `_emit_fragment` 発火時の fragment 内容
  - for ループ終了経路の判別 (正常終了 / 例外 / 中断)
  - residual flush 発火の有無と内容
- これにより主問題の候補 A〜D を log で切り分け可能

## 規律 (このバグを追う上での自戒)

1. **wav バイト比較を提案するな**: nagi wav に対象音声が存在しないので構造的に不可能。 同セッションで 3 回ハマった
2. **副次現象 (次 wav 冒頭出現) を主問題のように追うな**: 主問題が解ければ自動解消する性質
3. **「機械的裏付け」 衝動に注意**: user listening で既に確定している事実を log で裏付ける作業は解明に貢献しない
4. **emit_sub_speak されたか否かの確認は意味がある (上流の経路を絞れる)**: しかし wav バイト比較は意味がない (下流の機械裏付けにしかならない)

## 関連メモリ

- `feedback_no_wav_bytes_comparison_for_logical_delay.md` — wav バイト比較の罠
