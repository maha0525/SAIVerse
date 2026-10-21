# Sarashina API をプロバイダとして同梱する

**ステータス**: 完了（PR #402 マージ済み。2026-10-21〜22 にまはーが実サービスで mini・nano とも疎通を確認）

## 利用者が得るものと経路

Sarashina のキーをモデル管理のプロバイダタブで設定し、会話用の
Sarashina3 mini / nano を通常・軽量モデルとして選べるようにする。
同梱 provider → 3層ローダー → model の provider_ref 解決 → factory →
OpenAI SDK → Chat Completions の経路を使い、キーの保存・可用性表示・
構造化出力・ツール検出・ストリーミング・使用量の記帳を既存機構に載せる。
ペルソナの既存モデルや記憶は変更しない。

## 接続の責任と不変条件

- 接続先は `https://api.sarashina.ai/v1`、認証は `SARASHINA_API_KEY`。
- 会話モデルは `sarashina3-mini` と `sarashina3-nano`。文脈長は128k、
  出力の設定は公式の `max_completion_tokens`（1〜8000）を使う。
- Guard / Embeddings / Rerank / Translate は会話モデルとして登録しない。
- provider の `openai_compat_options` を model に継承する。OpenAI クライアントの
  SDK送信直前で全呼出経路に適用し、モデル名やホスト名で分岐しない。
  Sarashina は `supports_stream_options` / `supports_function_tool_choice` /
  `supports_message_name` が false。他の接続先の既定動作は変えない。
- 関数指定の tool_choice は、その関数だけを tools に残して `required` に変換し、
  強制する意味を保つ。該当関数が無ければ送信前にエラーにする。
- usage は最終の空 choices チャンクから読む。stream_options は送らない。
  SDK の extra_body に指定された項目にも同じ互換設定を適用する。
- provider の画面上書きでも互換設定を保持する。キー値をコード・ログに書かない。

## 検証

隔離 SAIVERSE_HOME と空の user/expansion 層から実ローダー・API・factoryを通し、
ダミーキーと httpx MockTransport を使って実SDKのJSON・認証・SSE処理を確認する。
通常生成、構造化出力、ツール検出、テキスト/ツールのストリーム、使用量、
関数の強制、他プロバイダの既定挙動、キー有無による可用性を検証する。
実サービスへの推論と本番ペルソナへの接触は行わない。

## 公式資料（2026-10-15確認）

- https://docs.sarashina.ai/introduction
- https://docs.sarashina.ai/api-reference/chat-completions
- https://docs.sarashina.ai/models
- https://docs.sarashina.ai/rate-limits

mini は6 RPM、nano は60 RPM。料金の恒久的なゼロ設定は公式の提供条件を
確認せずに決めない。レート制限には既存の指数バックオフとエラー処理を使う。

## 検証結果と経緯

- 2026-10-15: ローダー→モデル解決→factory→実OpenAI SDK→httpx2 MockTransportを通す
  Sarashina専用14テストが通過。通常/構造化出力/テキストとツールのSSE/usage/401/429/
  キー有無/プロバイダ上書き保存を確認した。合成入力とダミーキーだけを使用。
- 関連回帰は471 passed、2 skipped（Windowsのsymlink不可、テストプロセスのgit未検出）、
  38 subtests passed。ruff、frontend/scripts/test-provider-config-errors.cjs、
  check_in_flight.py、git diff --checkが通過。
- 実行: `.venv/Scripts/python.exe -m pytest tests/llm_clients/test_sarashina.py
  tests/test_provider_configs.py tests/test_provider_api_key_envs.py
  tests/test_provider_config_fallback.py tests/test_provider_connection_test_errors.py
  tests/test_openai_reasoning.py tests/test_llm_clients.py tests/test_tool_spec_dispatch.py -n 0 -q`。
  実行前に `SAIVERSE_HOME` をworktree内の `test_data/sarashina-home` へ隔離した。
- 実サービスへの推論は未実施。キーを設定した実サービス接続はPRレビュー後に確認する。
  本番ペルソナとの対話を使う場合は操作ごとの明示承認を要する。
- 台帳の旧次アクション: 「会話用mini・nanoの接続と互換設定を実装中。
  隔離HTTPテストとlintを通し、develop向けPRを作成する。」（誰待ち: 私）
