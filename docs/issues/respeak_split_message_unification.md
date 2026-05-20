# Issue: re-speak 後の応答が複数メッセージに分裂する (UI / Building / SAIMemory)

**ステータス**: 🔲 未着手
**優先度**: medium (= 発火頻度は低いが、 UX / 履歴整合性に影響)
**作成日**: 2026-05-20
**関連**:
- `sea/runtime_llm.py:2415-2497` (= re-speak block)
- `sea/runtime_llm.py:2102-2113` (= `_stream_error` 検出 + state 格納)
- `llm_clients/gemini.py:1600-1608` (= SSE patch による server error 検出)
- commit `984860d` (2026-04-20、 re-speak 機構の初出 = Gemini 504 対策)
- `docs/handoff_2026-05-20_building_memory_db.md` (= 発生 session の経緯)

## 観測

2026-05-20 13:42、 sophie_city_a で 500 INTERNAL stream interruption が発生、 re-speak 機構が起動した結果、 **同一発話が 2 つのメッセージに分裂** して DB / SAIMemory / chat UI に記録された。

DB 状態 (`building_messages`):
- `seq=1970`: `gemini-3.1-flash-lite-preview` で 18 output tokens の partial response (「リファクタリング、」 で途切れ)
- `seq=1971`: llm_usage metadata なしの 377 文字の continuation 応答 (「本当にお疲れ様！...」)

backend.log の時系列:
1. `13:42:10` `emit_speak_start: placeholder msg_id=sophie_city_a_room:1970`
2. `13:42:14` `Stream interrupted by server: code=500 status=INTERNAL — will re-speak after storing partial response`
3. `13:42:14` `Normal-stream finalize: msg=sophie_city_a_room:1970 final_seq=4` (= partial を 1970 で確定)
4. `13:42:14` `Triggering re-speak after 504 stream interruption for persona=sophie_city_a`
5. `13:42:24` voice-tts speak_hook が `msg=sophie_city_a_room:1971` で発火 (= continuation の追加メッセージ)

## 設計上のズレ

re-speak 機構の意図は **「一つの発言が外部要因で中断された場合の再開」** であり、 中断と続行は **同一の発言** として扱われるべき。 にもかかわらず現状の実装は:

- partial を `_emit_speak_finalize` で **既存 placeholder (1970)** に確定保存
- partial を `_store_memory()` で **SAIMemory に独立 log**
- continuation を `_emit_say` で **新規 building message (1971)** として INSERT
- continuation を後段 compose/memorize node が SAIMemory に **別 log** として保存

→ 結果として:
- **Building log**: 2 行に分裂 (1970 + 1971)
- **SAIMemory**: 2 件に分裂 (partial + continuation)
- **chat UI**: 2 つの吹き出しに分裂 (= 個別の `streaming_complete` で別バブル化)
- **voice-tts**: 1971 で改めて speak_hook 発火 (= 二度発話)

これは「中断された一つの発言」 ではなく「2 つの独立した発言」 として扱われている。

## 解決方針

「1 つの中断された発言」 = 「1 つのメッセージ」 として一貫させる。 具体的には:

### A. Building (DB) 経路

- partial を `_emit_speak_finalize` で確定させない。 placeholder のまま保持
- continuation streaming 完了後に **同一 placeholder (1970)** に対して partial + continuation の結合 text で finalize
- 新規 INSERT (1971) は **作らない**
- `partial_due_to_stream_error: True` 等のフラグを `metadata` に残し、 経緯は記録 (= 後で経路の発火状況を追える)

### B. chat UI 経路

- partial の `streaming_complete` を **送らない**。 「中断通知」 (= 既存の `type: "info"` 経路) のみ表示
- continuation の `streaming_chunk` は **同じ message_id (= 1970)** 宛で発火させ、 1 つのバブルに追記される形に
- 最終的に `streaming_complete` は 1 度だけ発火

### C. SAIMemory 経路

- partial の独立 `_store_memory()` を **取りやめ** (= 現在 `sea/runtime_llm.py:2437-2443`)
- continuation 完了後に compose/memorize node が **partial + continuation の結合 text を 1 件** として log
- 再開に失敗した場合 (= continuation 空応答) の fallback として、 その時だけ partial を単独 log

### D. voice-tts 経路

- partial の `_emit_speak_finalize` で `final_voice_text=""` を渡す現挙動は、 「partial までは sub-speak 済」 を前提にしている
- continuation の text は新規 placeholder ではなく **partial と同じ placeholder の続きの sub_seq** として sub-speak → 同 placeholder で finalize
- → voice-tts addon 側で「同一発言の続き」 として連続再生される (= 二度発話にならない)

## 関連実装ポイント (要編集箇所)

```
sea/runtime_llm.py
  L2384-2413: pipeline_msg_id 経路の finalize (= partial 確定を保留する形に変更)
  L2415-2497: re-speak block (= continuation を同 placeholder に追記する形に変更)
  L2438-2443: partial の SAIMemory store (= 削除 or fallback 化)
  L2480-2495: continuation の _emit_say + state["speak_content"] 上書き (= same-placeholder update に変更)
```

## 検証シナリオ

1. **強制 500/504 mock で再現**: `llm_clients/gemini.py` の SSE patch を mock し、 stream 途中で `_last_stream_error` を立てて re-speak を発火させる
   - 期待: DB に 1 行のみ、 SAIMemory に 1 件のみ、 chat UI に 1 バブルのみ
2. **continuation が空応答**: re-speak で空応答が返るケース
   - 期待: partial のみで finalize、 「中断したが再開できなかった」 旨を `info` で通知、 partial は単独で SAIMemory に残る (= 既存 fallback 経路を活かす)
3. **voice-tts 連携**: 同一 placeholder への sub-speak 続きで自然に発話される (二度発話しない)
4. **過去経路**: 過去ログ範囲では発火痕跡は 1 例のみだが、 修正後も同経路が動く保証が要 (= unit test 化)

## 関連リソース

- 発生時の handoff: `docs/handoff_2026-05-20_building_memory_db.md`
- re-speak 導入 commit: `984860d` (2026-04-20)
- 残存 session log 61 件中、 `Stream interrupted by server` / `Triggering re-speak` が記録されているのは `20260520_133821` のみ (= 残存ログ範囲では本件が初発、 ただし 2026-04-20 〜 2026-05-15 のログは現存せず確定不可)

## ログ

- 2026-05-20: 発生確認 + 本 issue 作成。 機構自体は 1 ヶ月前から存在する既存コードで、 今回の Building Memory DB 化改修とは独立。 別ペルソナで同様事例が頻発するかをまはー側で確認中
