# tool spec の形式判定がクラス名の一覧で行われ、Codex と wrapper が漏れる

**状態**: 未解決 (2026-08-01 起票、Codex レビュー二巡目の指摘2)。既存欠陥。使用量帰属の修正とは独立。

## 現象

`sea/runtime.py` の `_build_tools_spec` は、LLM クライアントの**具象クラス名の一覧**で tool spec の形式を選ぶ。

```python
client_class_name = type(llm_client).__name__
if client_class_name in ("OpenAIClient", "AnthropicClient", "OllamaClient", "NvidiaNIMClient"):
    # OpenAI 互換の spec
else:
    # Gemini の types.Tool
```

この一覧に入っていないクライアントは、OpenAI 互換であっても Gemini 形式の tool spec を渡される。現在漏れているのは:

- **`OpenAICodexClient`** — Codex 経由のモデル全般
- **`LlamaCachedClient`** — `llama_slot_save_path` を設定したモデル (中身が `OpenAIClient` でも wrapper のクラス名で判定される)

## 影響

`OpenAICodexClient` の場合、`_to_responses_tools` が dict でない tool を読み飛ばすため、**リクエストに tools が一つも入らない**。ペルソナから見ると「ツールを持っていない」状態になる。

`LlamaCachedClient` の場合、inner の OpenAI API に Gemini 形式のスキーマが渡る。

いずれも機構から読んだ帰結であって、**実機で Codex ペルソナの tool call を確認したわけではない**。ただし判定が名前の一覧である以上、新しいクライアントや wrapper を足すたびに同じ漏れが起きる構造になっている。

## 修正の方向

具象クラス名ではなく、クライアント側が申告する **tool protocol / capability** で分岐する。`LLMClient` に「どの形式の tool spec を受け取るか」を持たせ、wrapper は inner の申告を委譲する。

失敗類型としては「振る舞いの分岐条件を、目的 (どの形式を受け取れるか) ではなく種類 (クラス名) で書いた」もの。同じ形が他にも無いか、`type(...).__name__` による分岐を横断して確認する価値がある。
