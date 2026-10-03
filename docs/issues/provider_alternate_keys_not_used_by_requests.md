# 予備 API キーが可用性表示と実リクエスト・接続テストで一致しない

**状態**: 未着手
**発見**: 2026-10-03、[PR #350 再レビュー](https://github.com/maha0525/SAIVerse/pull/350#issuecomment-5966385486) の範囲外として分離
**関連設計**: [モデル＆プロバイダ管理](../intent/model_provider_management.md) §12

## 条件と影響

`api_key_env` と `api_key_env_alternates` を持つ認証必須の OpenAI 互換 provider で、主キーを未設定、予備の環境変数だけを設定する。provider 一覧の「キー設定済み」とモデルの可用性判定は予備キーを数える一方、実クライアントと接続テストへはその予備キーが渡らない。利用可能に見えても、クライアント初期化のキー不足や上流の認証エラーで失敗し得る。

## コード上の根拠

- `api/routes/providers.py` の `_api_key_env_names` / `_to_provider_info` と `saiverse/model_configs.py` の `_get_required_env_vars` は主キーと予備キーを列挙する。
- `llm_clients/factory.py` の `openai_compat` 分岐は `api_key_env` を渡すが、`api_key_env_alternates` を実クライアントへ引き渡さない。
- `api/routes/providers.py` の `_run_connection_test` は単一の `api_key_env` の値だけを読み、Authorization ヘッダーを作る。保存済み provider のテストも同じ経路。

これは「すべての native provider が予備キーを無視する」という主張ではない。Gemini は専用クライアント側で無料枠・有料枠のキーを扱う。完了済みの [キー入力欄の issue](archive/provider_key_panel_ignores_alternate_keys.md) は再開せず、任意の予備キー宣言と利用経路の一致を別件として扱う。

## 隔離再現の概要 (未実行)

1. 一時 HOME / SAIVERSE_HOME に synthetic provider/model を用意し、主キーと既定キーを未設定、予備キーだけをダミー値にする。
2. 一覧・可用性判定を読み、予備キーで設定済みとされることを確認する。
3. SDK / HTTP を fake に置き換え、factory の引数と接続テストのヘッダーを観測して、予備キーが選ばれないことを確認する。本物のキー・外部通信・推論は使わない。

## 修正前に決めること

キー選択の所有者と優先順位を、可用性・実送信・接続テストで揃える。予備キーを送る場合も既存の接続先・資格情報の信頼境界を守り、失敗時に無断で別のキーや課金先へ切り替える設計にしない。受け入れ条件は、主キーのみ・予備キーのみ・両方・未設定を各境界で比較すること。

本記録はコード読解のみ。実装修正・追加回帰・実機検証は未着手のため、進行中台帳には載せない。
