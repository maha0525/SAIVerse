# Claude Extended Thinkingへの対応メモ

ClaudeをOpenAI互換エンドポイント経由で利用している場合、`extra_body` に
`{"thinking": {...}}` を付与してもレスポンスに思考ブロックは含まれない。
Anthropic公式の互換レイヤーは最終回答のみを返す仕様となっているため。

思考コンテンツをUIに表示したい場合は、以下のいずれかを行う。

1. **AnthropicネイティブAPI（/v1/messages）を使う**
   - `anthropic` Python SDK もしくはHTTPで `messages` エンドポイントを呼ぶ。
   - リクエストに `"thinking": {"type": "enabled", "budget_tokens": N}` を設定すると
     `content` に `{"type":"thinking","text":...}` が含まれる。
   - 返却された `thinking` ブロックを `llm_clients` パッケージの正規化ロジックに通し、
     建物ログ専用の折りたたみUIへ組み込む。

2. **Proxied APIを使う**
   - OpenRouter 等、reasoning情報を露出する仲介サービスを利用する。
   - レスポンス構造がOpenAI互換と完全に一致しない場合があるため、
     パーサやUI側で追加対応が必要。

将来的にネイティブAPIへ切り替える場合:

- `models.json` のClaudeエントリに `provider`: `anthropic_native` 等を追加し、
  新しいクライアント実装を `llm_clients/` 配下に用意する。
- `saiverse_manager` や `persona_core` は既存のReasoning表示処理を流用可能。
- 認証は `ANTHROPIC_API_KEY` をそのまま共有できるが、
  ヘッダーの `x-api-key` / `anthropic-version` に注意すること。

参考: [Anthropic公式ドキュメント — Claude API OpenAI互換レイヤー](https://docs.claude.com/en/api/openai-sdk)
