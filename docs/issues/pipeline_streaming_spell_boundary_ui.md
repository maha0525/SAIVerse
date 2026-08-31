# Issue: Pipeline Streaming 中の spell 区切り表示

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-16
**関連**: [`docs/intent/voice_tts_pipeline_streaming.md`](../intent/voice_tts_pipeline_streaming.md)、 commit `e848b63` / `f26b2fb` 周辺の Pipeline Streaming 実装

## 背景

Pipeline Streaming (= LLM streaming chunk と並行で文区切り sub-speak を発火する経路) で、 スペル入りの応答が UI 上では下記のように表示される:

1. ストリーミング中: 1 つの assistant バブルに文字が積み上がる
2. 途中で出た `/spell name='...' args=...` 行が **そのまま raw text として表示** される
3. スペル実行中 (= 数秒〜数十秒) は表示が止まる
4. 次のラウンドの LLM 応答 chunk も **同じバブルに改行なく append** される
5. 全部終わると `streaming_discard` で streaming バブルを破棄、 `say` イベントで整形済み (= スペル結果が `<details class="spellResult">` 折り畳みになった) バブルが新規に出る

問題点:
- スペル行が生で見えるのは UI として汚い (= 折り畳みブロックで隠したい)
- スペル結果は完了するまで途中表示されない (= 出来た時点で見せたい)
- 次ラウンドの発言が前ラウンドと改行なく繋がるので 1 つの文に見える
- 完了時に streaming バブルがいったん消えて 整形済みバブルに置き換わる (= 軽く一瞬チラつく)

## 期待する挙動

1. ストリーミング中: 1 つの assistant バブルに文字が積み上がる
2. スペル行が出てきたら、 該当行を 「スペル起動中」 の折り畳みブロックに即時置き換える
3. スペル実行が終わった時点で、 折り畳みブロックに結果を入れる
4. 続きのラウンドの発言は **同じバブルの中で** 改行を入れて積み上がる
5. 全部終わった時、 バブルの内容が整形済み全文に **滑らかに置き換わる** (= 別バブルを追加しない、 既存バブルを更新する)

## 解決案候補

frontend と backend の協調設計:

- backend: `streaming_chunk` と `say` イベントに `message_id` (= `_emit_speak_start` で発番した placeholder ID) を載せる
- frontend: `message_id` 一致で既存バブルを更新するロジックに変える (= 現状の 「最後の `_streaming=true` バブルに append」 から)
- backend: スペル行を検出した時点で、 frontend に 「ここから先はスペル折り畳み」 を伝える別イベント (例: `streaming_spell_invoke`) を発火
- backend: スペル結果が出た時点で 「スペル折り畳みに結果を入れる」 イベント (例: `streaming_spell_result`) を発火
- frontend: それらを受けて、 バブル内に折り畳みブロックを動的に挿入・更新する

## 関連リソース

- `sea/runtime_llm.py:_consume_pipeline_stream` (= 現状の chunk 消費 + sub-speak 発火 + spell 検出ロジック)
- `frontend/src/app/page.tsx` の `streaming_chunk` / `streaming_discard` / `streaming_complete` / `say` ハンドラ (line 1475〜)
- 旧コード (= Pipeline Streaming 導入前) では `_emit_bubble1_early` + `_emit_say(bubble1)` + `_emit_say(bubble2)` の 2 段 emit でやってた挙動。 これを 1 バブルに集約しつつ滑らかに切り替える方向

## ログ

- 2026-05-16: 作成。 Pipeline Streaming の D + B 修正後、 A だけ残置で後回し決定
