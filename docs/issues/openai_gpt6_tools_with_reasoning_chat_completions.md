# Issue: GPT-6 系 (Sol / Luna / Astra) は Chat Completions で、推論エフォートとネイティブのツール呼び出しを同時に使えない

**ステータス**: 🔲 未着手 (低優先 — 現行の組み込みプレイブックは踏まない)
**優先度**: low (通常運用では発火しない。ユーザーが自作のプレイブックで LLM ノードに `available_tools` を宣言したときだけ当たる)
**作成日**: 2026-09-30
**関連**: `llm_clients/openai.py` (Chat Completions 専用。`OPENAI_ALLOWED_REQUEST_PARAMS` に `verbosity` が無い)、`builtin_data/models/gpt-6-{astra,sol,luna}.json`、`sea/runtime_llm.py` (`available_tools` の LLM ノード)

## 症状

GPT-6 Astra / Sol / Luna の API 版 (`provider_ref: openai`) で、ツールを渡した呼び出し (`generate(..., tools=[...])`、`generate_stream(..., tools=[...])`) が HTTP 400 で落ちる。

```
Function tools with reasoning_effort are not supported for gpt-6-sol in /v1/chat/completions.
To use function tools, use /v1/responses or set reasoning_effort to 'none'.
```

## 確認した事実 (2026-09-30、実機。隔離した単体スクリプトで、モデル設定 → ファクトリ → OpenAIClient → API の経路)

- Sol / Luna (今回追加) と Astra (既存) の 3 本すべてで同じ 400。UI の既定エフォート (Sol・Luna は medium、Astra は high) が付いたまま送られるため。
- 同じ 3 本で、ツールなしの通常応答・構造化出力・ストリーミングは成功する。
- OpenAI 公式のモデル一覧ページにも同じ制約が書いてある (Chat Completions は推論エフォート `none` のときだけ関数ツール可、Responses API は全エフォートで可)。
- ネイティブのツール呼び出しを宣言している LLM ノード (`available_tools`) は、現行の組み込みプレイブックには無い (`builtin_data/playbooks/archive/` の 4 本だけ)。ペルソナの道具はスペルで動くので、通常の会話・自律行動・スルース・記憶整理はツールなしで呼ばれ、この制約に当たらない。
- 発話のストリーミングは 3 箇所すべて `tools=[]` を明示して呼んでいる (`sea/runtime_llm.py`)。`generate_stream` は `tools` を省略すると内蔵の `OPENAI_TOOLS_SPEC` を勝手に付ける作りなので、`tools` を省略した呼び出しを本線に足すと GPT-6 系では落ちる。

## 直す方向の候補 (未決)

1. ツールを付けるリクエストでだけ `reasoning_effort` を落とす (または `none` にする)。ツールと深い推論が両立しなくなるので、ユーザーに黙ってやらない。
2. OpenAIClient に Responses API の経路を足す。`verbosity` (モデル定義に `client_support: ["responses"]` と書いてあるつまみ) もここで初めて効く。範囲が大きい。
3. 何もしない。理由は上の「踏まない」。自作プレイブックでこの組み合わせを使うユーザーが出たら 1 か 2 を決める。

## 補足

- GPT-6 系のコンテキストは API 版が 272K (これ以上は長文割増)。Codex 版はバックエンドの一覧が `context_window: 272000` (`max_context_window: 872000`) を返す。
