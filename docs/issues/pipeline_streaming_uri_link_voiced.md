# Issue: ストリーミング sub-speak で URI のリンクアドレスまで読み上げてしまう

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-16
**関連**: [`docs/intent/voice_tts_pipeline_streaming.md`](../intent/voice_tts_pipeline_streaming.md)、 [`saiverse/content_tags.py`](../../saiverse/content_tags.py) (= `strip_user_only` / `strip_in_heart` 周辺の voice-safe テキスト生成)

## 背景

ペルソナが Markdown のリンク記法 `[表示テキスト](saiverse://item/.../content)` のような形でリンク付きの文を出力したとき、 Pipeline Streaming の sub-speak で **URI のアドレス部分まで TTS に流して読み上げてしまう**。

旧 (= ストリーミング前) は emit_speak の経路で全文を受け取ってから voice 用 text を作っていたため、 何かしらの処理 (= 確認待ち) で 「UI に出る部分だけ」 が TTS に渡される形になっていた。 Pipeline Streaming では文区切りで chunk を流す途中に URI が出ると、 そのままの形で sub-speak に渡されてしまう。

## 期待する挙動

ストリーミング chunk の中で URI 形式 (例: `(saiverse://...)`、 `(https://...)`) を検出したら、 sub-speak の発火を **URI が完全に閉じるまで待機** する。 完全な Markdown リンクが得られたら、 UI 表示テキスト (= 角括弧の中身) だけを抽出して voice-tts に渡し、 URI 自体は voice-tts に流さない。

## 解決案候補

`_consume_pipeline_stream` の chunk 消費ループに以下を追加:

1. 文区切りを検出した時点で、 当該 sub-text の中に **未完了の Markdown リンク** が含まれていないかチェック
   - `[` が出てきたが対応する `](URI)` がまだ閉じてない場合は sub-speak emit を保留 (= last_emit_pos を進めず次の chunk を待つ)
2. リンクが完全に閉じてから sub-speak emit
3. sub-speak に渡す前に Markdown リンクを 「表示テキスト」 のみに置換する処理 (= `[表示テキスト](URI)` → `表示テキスト`) を入れる
   - これは `saiverse.content_tags` に新規 helper として追加 (= 例: `strip_markdown_links_for_voice(text)`)
4. 既存の `strip_user_only` / `strip_in_heart` と同じく、 emit_sub_speak と emit_speak_finalize の voice 経路で適用

URI を含むファイルパスや plain URL もペルソナが普通に出力する可能性があるので、 Markdown リンク記法だけでなく、 plain URL も含めるか別途検討。

## 関連リソース

- `sea/runtime_llm.py:_consume_pipeline_stream` (= chunk 消費 + sub-speak 発火)
- `sea/runtime_emitters.py:emit_sub_speak` (= `text_for_voice = strip_user_only(strip_in_heart(sub_text))`)
- `saiverse/content_tags.py` (= 既存の voice-safe 変換 helper)
- 旧コード (= Pipeline Streaming 導入前) で URI が読み上げられなかった経路は、 emit_speak が全文を受けてから何かしらの処理で除去していた可能性。 該当処理を特定して Pipeline Streaming にも適用する

## ログ

- 2026-05-16: 作成。 Pipeline Streaming の副作用として、 URI リンクアドレスが TTS で読み上げられる現象を まはー が観測。 修正には chunk 境界 + URI 完了待ち + Markdown リンク表示テキスト抽出の組み合わせが要るので後回し決定
