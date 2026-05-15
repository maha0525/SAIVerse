# セッションハンドオフ 2026-05-15 (= Pipeline Streaming 設計確定)

前セッション (= `handoff_2026-05-15.md`) は Phase 1 (= voice_tts_playback_queue) 完了 + Stack-chan 流量制御 + bubble1 早期 emit まで。

本セッションは LLM → TTS Pipeline Streaming の **設計確定 + 下層 building block 3 commit** までで、 残実装は次セッションへ。 議論を 6 周経て設計が大幅に変わった (= 1 message 統合 + bubble1/bubble2 撤廃) ため、 次セッション着手前に intent doc を必読:

**`docs/intent/voice_tts_pipeline_streaming.md`** ← 設計確定版、 残実装プラン Phase 2-B-step3 以降を全部記載

## 本セッションで完了した実装

下層 building block。 単体ではユーザ体感に変化なし、 Phase 2-B-step3 以降と組み合わせて初めて Pipeline Streaming が動く。

| commit | 場所 | 内容 |
|---|---|---|
| `613be6f` | voice-tts addon (`feature/subscribe-before-open`) | _Job/`_MessageState` に sub_seq + is_final、 同 message_id 連続 enqueue 機構 (= audio_stream 連結合成) |
| `49299e2` | 本体 (`feature/memory-notes-and-organize`) | `HistoryManager.update_building_message` 追加 (= placeholder の content/metadata 後追い update) |
| `5141649` | 本体 | `emit_speak_start` / `emit_sub_speak` / `emit_speak_finalize` の 3 段階 API + sea/runtime.py wiring |

検証 (tests/sea/ 33 件 + voice-tts/tests/ 67 件 + tests/test_addon_hooks.py 8 件) 全 pass。

## 設計議論の経緯 (= まはー インタビュー 6 周の要約)

intent doc 草稿で 「sub-speak emit + voice-tts 連結機構」 を提案 → 以下の指摘を経て大改訂:

1. **「メッセージ分割の話に飛ぶな、 1 message が前提」** → メッセージ単位の話だと再認識
2. **「UI bubble 表現と内部処理を混同するな」** → 設計対象は内部処理経路、 UI / 保存形式は維持
3. **「メッセージ分割は UI 都合だが内部処理を侵食している」** → bubble1/bubble2 別 record 化を撤廃する方向
4. **「中間表現 1 message + sink 分配は既存通り、 voice-tts hook だけ 1 回」 で OK か** → ただし Pipeline Streaming で sub-speak 連発の話を私が落としてた、 修正
5. **「message_id を UI 表示単位から分離する純粋 ID 化」 を まはー が提案** → response_id 共存案を起こしたが、 まはー が更にシンプル化を志向
6. **「bubble1/bubble2 自体撤廃」 → response_id 不要、 message_id がそのまま機能** → 確定
7. **「`<user_only>` 廃止は他ペルソナへの spell 結果露出のリスクで NG」** → 私の見落とし、 訂正して `<user_only>` ラップは維持

最終設計 (= intent doc 参照): bubble1/bubble2 を 1 record に統合、 `<user_only>` ラップ維持、 spell 起動行は LLM 生 text のまま、 spell 結果は `<details class="spellResult">` で wrap、 message_id 1 つで全部足りる。

## 次セッションでやること

intent doc の Phase 2-B-step3 以降を順に実装:

### Phase 2-B-step3: spell loop return + caller 統合

- `_run_spell_loop` の return を `(full_merged_text, loop_count)` に変更
- `_build_spell_details_html` を 「`<user_only alt="...">/spell ...\n<details class="spellResult">[結果]</details></user_only>`」 を生成する関数に置換
- `runtime_llm.py` の 3 caller (= tool / streaming / non-streaming) で `_emit_say(bubble1)` + `_emit_say(bubble2)` の 2 回呼び出しを `_emit_say(full_merged_text)` 1 回に統合
- `wrap_spell_blocks` は spell loop が既に `<user_only>` 込みで構築するので emit_say 内では no-op になる、 ただし他経路用に残す
- ここまでで bubble1/bubble2 統合は ON/OFF 関係なく適用される (= UI bubble は 1 つに統合表示)

### Phase 2-C: Pipeline Streaming streaming loop 組み込み

- `runtime_llm.py` の streaming loop に `_emit_speak_start` + 句読点検出 + `_emit_sub_speak` 連発 + `_emit_speak_finalize` を組み込む
- 既存 Phase 1 (`_emit_bubble1_early` / `_extract_first_text_before`) は撤去 (= sub-speak が代替)
- 機能フラグ `SAIVERSE_LLM_TTS_PIPELINE=1` で gate
- spell 行を含む sub-text の境界判定は `<user_only>` ブロック完了まで待つ実装

### Phase 2-D: 中断時 close_stream

- LLM cancellation 時の voice-tts audio_stream リソースリーク防止
- `_emit_speak_finalize` を中断時にも呼ぶ or 専用 abort hook 発火経路追加

### Phase 2-E: UI 側の spell 結果表示

- ReactMarkdown / sanitizeSchema で `<details class="spellResult">` を rendering 対象に追加
- 既存 `.spellBlock` CSS を `.spellResult` 用に流用 or リネーム
- 起動行 (= `/spell foo(bar)`) の表示は raw text、 必要なら code styling 追加

## 既存システムとの整合 / 注意点

- **既存 record の互換性**: 過去保存済の bubble1/bubble2 形式 record は放置 (= 過去 record は 2 record のまま、 今後の応答は 1 record)。 UI 表示は混在しても read-only なので問題なし。 マイグレーション不要
- **構造化出力 (response_schema あり)**: Pipeline Streaming スキップ (= JSON token streaming に文区切り概念が成立しない)。 既存の 「全文完了後 emit」 経路で動かす
- **bubble1/bubble2 統合と Phase 1 (bubble1 早期 emit) の関係**: Phase 1 は spell 待ち中に bubble1 を先に音声化する目的だった。 Phase 2-C で sub-speak 連発が同じ役目を担うので Phase 1 コードは撤去
- **既存 commit 5141649 の emit_speak_finalize 中の `final_sub_seq=None` 互換**: voice-tts 側は sub_seq=None で 「1 message=1 job」 の従来動作にフォールバックする (= Pipeline Streaming OFF 時もこの API は動作する)

## push 状況

| repo | branch | push 済み? |
|---|---|---|
| voice-tts addon | `feature/subscribe-before-open` (origin = Nature109) | `613be6f` までは未 push (= 本セッション中の commit、 まはー判断で push 必要) |
| 本体 | `feature/memory-notes-and-organize` | local only (= push 不要) |
| stackchan addon | `feature/migrate-to-stackchan-mcp` | 本セッションで commit なし |

voice-tts の `613be6f` を origin (Nature109) に push する場合、 [memory: voice-tts remote layout](../../.claude/projects/C--Users-shuhe-workspace-SAIVerse/memory/project_voice_tts_remote_layout.md) 参照 (= origin = Nature109、 fork = maha0525 は使ってない)。

## 罠注意

- 議論で 「`<user_only>` 廃止」 と私が一度書いて まはー に訂正された (= 他ペルソナへの spell 結果漏洩リスク)。 次セッションで実装する時に `<user_only>` ラップを忘れないこと
- `_run_spell_loop` の return signature 変更は呼び出し側 3 箇所 (= line 1465, 1800, 2079 周辺) 全部に影響するので、 1 箇所ずつ追跡せず diff を一気に取る
- spell HTML 構造変更で既存 frontend CSS (= `.spellBlock`) との対応が変わる、 既存表示が崩れないか実機確認必要
