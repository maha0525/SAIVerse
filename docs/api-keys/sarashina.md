# Sarashina APIキーの取得と設定

1. [Sarashina API コンソール](https://chat.sarashina.ai/) にログインする。
2. 「API Keys」で新しいキーを作成してコピーする。
3. SAIVerse の「グローバル設定 → モデル管理 → プロバイダ」で
   **Sarashina (SB Intuitions)** のキー設定を開き、キーを保存する。
4. 会話モデルまたは軽量モデルに **Sarashina3 mini** / **Sarashina3 nano** を選ぶ。
   キーを保存すると設定済みの表示になり、モデルを選択できる。

画面からのキー保存はその場で反映され、再起動は不要。
ファイルで設定する場合はリポジトリルートの `.env` に次を記入し、
SAIVerse を再起動する。

```dotenv
SARASHINA_API_KEY=取得したキー
```

両モデルはテキスト、ツール呼び出し、構造化出力に対応する。
コンテキストは128kトークン、出力上限は8000トークン。
画像の直接入力には対応しない。

2026-10-15 に確認したレート制限は mini が毎分6回、nano が毎分60回。
軽量モデルや判断処理で繰り返し呼ぶ用途には nano を勧める。
429 が返る場合は次の1分のカウント期間まで待つ。
提供条件と利用料金はコンソール・公式案内で確認する。

プロバイダの「接続テスト」は互換APIの `/models` を照会する。
Sarashina の公開仕様にはこのエンドポイントの記載がないため、
接続テストの404だけでキーや会話APIが使えないとは判断しない。

- [キー取得・認証](https://docs.sarashina.ai/introduction)
- [Chat Completions仕様](https://docs.sarashina.ai/api-reference/chat-completions)
- [モデル一覧](https://docs.sarashina.ai/models)
- [レート制限](https://docs.sarashina.ai/rate-limits)
