# tool spec の形式判定がクラス名の一覧で行われ、Codex と wrapper が漏れる

**状態**: 検証待ち (develop に取り込み済み、実機確認待ち)。2026-08-01 起票、Codex レビュー二巡目の指摘2。使用量帰属の修正とは独立。

## 起票時の現象

`sea/runtime.py` の `_build_tools_spec` は、LLM クライアントの**具象クラス名の一覧**で tool spec の形式を選ぶ。

```python
client_class_name = type(llm_client).__name__
if client_class_name in ("OpenAIClient", "AnthropicClient", "OllamaClient", "NvidiaNIMClient"):
    # OpenAI 互換の spec
else:
    # Gemini の types.Tool
```

この一覧に入っていないクライアントは、OpenAI 互換であっても Gemini 形式の tool spec を渡される。起票時に漏れを確認したのは:

- **`OpenAICodexClient`** — Codex 経由のモデル全般
- **`LlamaCachedClient`** — `llama_slot_save_path` を設定したモデル (中身が `OpenAIClient` でも wrapper のクラス名で判定される)

## 影響

`OpenAICodexClient` の場合、`_to_responses_tools` が dict でない tool を読み飛ばすため、**リクエストに tools が一つも入らない**。ペルソナから見ると「ツールを持っていない」状態になる。

`LlamaCachedClient` の場合、inner の OpenAI API に Gemini 形式のスキーマが渡る。

いずれも機構から読んだ帰結であって、**実機で Codex ペルソナの tool call を確認したわけではない**。ただし判定が名前の一覧である以上、新しいクライアントや wrapper を足すたびに同じ漏れが起きる構造になっている。

## 修正の方向

具象クラス名ではなく、クライアント側が申告する **tool protocol / capability** で分岐する。`LLMClient` に「どの形式の tool spec を受け取るか」を持たせ、wrapper は inner の申告を委譲する。

失敗類型としては「振る舞いの分岐条件を、目的 (どの形式を受け取れるか) ではなく種類 (クラス名) で書いた」もの。同じ形が他にも無いか、`type(...).__name__` による分岐を横断して確認する価値がある。

## 実装 (2026-10-02)

- `LLMClient.tool_spec_format()` が受け取る形式 (`openai` / `gemini`) を申告する。未申告は `NotImplementedError`、未知の形式は SEA 側の `ValueError` とし、Gemini へ暗黙に落とさない。
- `OpenAIClient` / `AnthropicClient` / `OllamaClient` / `OpenAICodexClient` / `XAIClient` は `openai`、`GeminiClient` は `gemini`。`NvidiaNIMClient` は実際の serializer と同じく `OpenAIClient` から継承する。
- `LlamaCachedClient` は内側のメソッドを委譲する。SEA は `_inner` や具象クラス名を見ず、申告だけで既存カタログを選別する。派生クラス・多段 wrapper も同じ経路になる。
- 全体の契約と責任境界は [モデル＆プロバイダ intent §9](../intent/model_provider_management.md#9-モデル固有の-api-契約をモデル定義からプロバイダ境界まで保つ) に追記した。ツール権限、実行、モデル選択、wire serializer 自体は変更しない。

## 検証

- `tests/test_tool_spec_dispatch.py` (29 件): 合成 `ToolSchema` → 実 adapters → SEA 選別 → Codex Responses body / Anthropic・xAI serializer / Gemini SDK 型まで確認。通常・streaming とも、`LlamaCachedClient` を通した OpenAI SDK 呼び出しに選択済み dict が届く。SDK の送信口と slot cache は fake、socket 接続は禁止する。
- 全具象クライアント、派生クラス、0〜2 段 wrapper、クラス名と申告が食い違う client、空・不一致の選別、未申告・未知形式の拒否を回帰化した。既存 wrapper 回帰も名前を借りた代役から capability を申告する代役へ変更した。
- `SAIVERSE_HOME` / `SAIVERSE_USER_DATA_DIR` を一時ディレクトリに固定して実行。本番ペルソナ・推論 API・永続履歴への接触はない。実 API の tool call 応答は未検証で、レビュー・実機確認待ちのまま置く。
- 変更 Python の `ruff check` と `scripts/check_in_flight.py` は通過 (台帳の既存猶予1行だけ警告)。関連回帰の実行結果・制約は下の経緯に残す。

## 経緯

- 2026-10-02 調査: 起票後、別件の [wrapper 委譲修正](archive/llama_cached_client_state_delegation_missing.md) で `_inner` を1段だけ剥がす処置は入っていた。ただし名前の一覧は残り、Codex・派生クラス・多段 wrapper は漏れたままだった。
- 同日の serializer 確認で `llm_clients/xai.py::_convert_tools` も OpenAI dict を受け取るのに名前の一覧に無いと判明。同じ契約の欠落なので今回の宣言に含めた。
- `sea/` / `llm_clients/` / `saiverse/` / `persona/` / `manager/` / `tools/` / `api/` の条件式と `__name__` 参照を横断確認し、この tool format 判定以外に client クラス名で形式を選ぶ分岐は見つからなかった。ログ用の型名表示は維持する。
- 最初の関連回帰は SOCKS proxy に必要な `socksio` 不足で SDK 初期化が失敗した。テスト環境に追加後、6 ファイルで 215 passed / 11 failed / 7 subtests passed。残る11件は既存 LLM クライアントテストの外部ホスト DNS 解決失敗 (`provider_security`、OpenRouter 等) で、今回の dispatch に到達する前に止まる。新規29件と既存 runtime helper の計52件は全通過 (4 subtests passed)。
- SEA 回帰一式・新規 dispatch・runtime helper・Codex stream・Anthropic request builder・llama-server を合わせた隔離検証は 168 passed / 4 subtests passed。Google SDK の既存 `TUPLE` 警告と `ast.Num` 非推奨警告のみ。
- 元の `_build_tools_spec` を `git show HEAD:sea/runtime.py` から隔離プロセス内だけに復元すると、Codex body・xAI serializer・新しい派生クラスの3回帰がすべて失敗することも確認。現行コードへ戻した別プロセスでは上記168件が通過した。

- 2026-10-03: PR #349 を develop へマージした (レビューはメティス、マージの判断はまはー)。台帳から移送した旧次アクション: 「実装と隔離回帰が揃い、レビュー待ち。次 = capability 宣言と serializer 境界をレビューし、承認した環境で Codex・wrapper の tool call を確認する。」(誰待ち: まはー (レビュー・実機確認))
