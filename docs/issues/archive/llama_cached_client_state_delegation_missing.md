# LlamaCachedClient が state を委譲せず、思考の記録が消える

**状態**: 解決 (2026-08-01 起票、Codex レビュー二巡目の指摘1 / 2026-09-21 実機実測のうえ修正)。

## 現象

`llm_clients/llama_cache.py` の `LlamaCachedClient` は任意の `LLMClient` を包む wrapper だが、
inner へ委譲しているのは `configure_parameters` / `consume_usage` / `config_key` /
`response_token_limit` / `generate` / `generate_stream` だけだった。

`LLMClient` が持つ他の state — `consume_tool_detection`、`consume_reasoning`、
`consume_reasoning_details`、`consume_thought_signature`、`consume_attachments`、
`model`、`supports_audio` / `supports_video` — は委譲されておらず、**wrapper 自身の
空の state** が返っていた。`sea/runtime_llm.py` はこれらを factory が返したオブジェクト、
つまり wrapper に対して呼ぶ。

## 実測 (2026-09-21、NEBULA の artemis-31b、隔離環境の合成ペルソナ)

`SAIVERSE_HOME` を分けた写しの世界で、合成ペルソナ `test_persona_a` を
`nebula-artemis-31b-q6` (`llama_slot_save_path` あり) に向け、`scripts/run_conversation.py`
で実チャット経路を 1 ターン流した。同じ台本を、モデル設定から `llama_slot_save_path` を
外した対照でも流した。

| 実行 | wrapper | スペルの実行 | 発言の記録に reasoning |
|---|---|---|---|
| 修正前 | 適用 | ✅ 成功 | ❌ 無し |
| 対照 (設定から外した) | 無し | ✅ 成功 | ✅ 有り |
| 修正後 | 適用 | ✅ 成功 | ✅ 有り |

client の継ぎ目で直接測った結果も同じだった — 修正前は
`wrapper.consume_reasoning()` が 0 件を返す一方で、直後に呼んだ
`inner.consume_reasoning()` には思考の全文が入っていた。

### 起票時の見立てのうち、実機では起きなかったもの

起票時は「**tool call が実行されない**」を筆頭の影響に挙げていたが、これは起きない。
理由は 2 つある。

1. **スペルはネイティブの tool call ではない**。ペルソナは応答本文に
   `/spell name='...' args={...}` の行を書き、runtime がその文字列を解析して実行する
   (`sea/runtime_llm.py` の `_run_spell_loop`)。`consume_tool_detection` はこの経路に
   関与しない。実測でもスペルは 3 回とも実行された。
2. **ネイティブ tool call の経路に生きた呼び出し元が無い**。`consume_tool_detection` を
   使うのは `node_def.available_tools` が空でないときの分岐だけで、builtin / DB の
   playbook を全数走査したところ `available_tools` を宣言しているノードは 0 件だった。

実機で起きていたのは **reasoning の欠落**だけである。ただし機構としての委譲漏れは
実在したので (下の「同時に見つかったもの」の通り、その経路は別の理由でも壊れていた)、
wrapper は完全な facade にした。

## 修正 (2026-09-21)

1. **`llm_clients/llama_cache.py`** — wrapper を完全な facade にした。`consume_*` 全種と
   対になる `_store_*`、`model` / `supports_*`、`generate_with_tool_detection`、
   `ensure_backend` / `backend_lease` を inner へ委譲する。`_store_*` も委譲が要る —
   runtime は tool 検出を「覗いて戻す」ので、片方だけ委譲すると戻した値が wrapper に
   埋もれる。委譲属性は inner を書き換える property なので、基底 `__init__` が配る
   既定値 (`model=""` 等) は配線中だけ素通しにして、**包むことが inner を変えない**
   ようにした (旧実装は `config_key` だけを退避・復元していた)。
2. **`sea/runtime.py` の `_build_tools_spec`** — client の class 名で送信形式を決めるので、
   facade で包まれていたら中身の class 名で判定するようにした。包みの名前
   (`LlamaCachedClient`) はどの分岐にも当たらず Gemini 形式へ落ちるため、OpenAI 互換の
   サーバーへ `google.genai` の `Tool` を送ることになっていた。
3. **`llm_clients/openai.py` の `_stream_tool_mode`** — 最後に来る「choices が空で usage
   だけの chunk」で `IndexError` になっていた。テキスト経路 (`_stream_text_mode`) は
   同じ形を既に弾いており、tool 経路だけ弾いていなかった。実測ではこの例外のせいで、
   ストリームは検出に辿り着く前に落ちていた。

回帰テスト: `tests/test_llm_clients.py` の `TestLlamaCachedClientIsACompleteFacade`
(fake inner で streaming tool 検出・reasoning・thought signature・添付・使用量・属性を固定。
基底に新しい `consume_*` が増えたら委譲漏れで落ちる検査も入れた) と
`test_openai_stream_tool_mode_survives_the_usage_only_chunk`、
`tests/test_runtime_llm_helpers.py` の `BuildToolsSpecSeesThroughTheCacheWrapperTest`。

## 同時に見つかったもの (別件)

NEBULA の llama-swap (`http://192.168.0.220:8092`) は llama.cpp の `/slots` API を
中継していない。save は毎回 HTTP 404 を返し、restore はローカルに写しが無いので
毎回スキップされる。つまり NEBULA のモデル設定では、この wrapper が存在する目的
(KV キャッシュの持ち越し) が働いていない。
→ [llama_slot_cache_inert_behind_llama_swap.md](../llama_slot_cache_inert_behind_llama_swap.md)
