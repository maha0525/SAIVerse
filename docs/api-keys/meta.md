# Meta (Muse) APIキーの取得と設定

1. [Meta Model API のコンソール](https://dev.meta.ai/) にログインし、チームを作成する。
2. API キーを新しく作成してコピーする。
3. SAIVerse の「グローバル設定 → モデル管理 → プロバイダ」で
   **Meta (Muse)** のキー設定を開き、キーを保存する。
4. 会話モデルまたは軽量モデルに **Muse Spark 1.3** / **Muse Wonder (Preview)** を選ぶ。
   キーを保存すると設定済みの表示になり、モデルを選択できる。

画面からのキー保存はその場で反映され、再起動は不要。
ファイルで設定する場合はリポジトリルートの `.env` に次を記入し、
SAIVerse を再起動する。

```dotenv
META_API_KEY=取得したキー
```

Meta の公式ドキュメントはキーを `MODEL_API_KEY` という名前で例示しているが、
SAIVerse が読むのは `META_API_KEY` だけ。

## 同梱しているモデル

| モデル | 用途の目安 | 料金 (100万トークンあたり、入力 / キャッシュ入力 / 出力) |
|---|---|---|
| Muse Spark 1.3 (`muse-spark-1.3`) | 通常の会話。Meta が新規利用に推奨する標準モデル | $1.25 / $0.15 / $4.25 |
| Muse Wonder (Preview) (`muse-wonder-1.0`) | 上位モデル (プレビュー版) | $4.00 / $0.20 / $20.00 |

どちらもコンテキストは約 100 万トークン (1,048,576)、出力上限は 131,072 トークン。
コンテキストの長さによる割増料金は無い。テキスト・画像の入力、ツール呼び出し、
構造化出力に対応する。1 回の要求に載せる画像は 50 枚まで (Meta の上限)。

- **推論は止められない**: Muse は常に推論してから答える。推論の深さ
  (Reasoning Effort) は minimal / low / medium / high / xhigh / max から選べ、
  既定は medium。minimal がいちばん浅いが、推論をゼロにはできない。
- **出力上限には推論の分も含まれる**: 推論に使ったトークンも出力として
  数えられ、出力の料金がかかる。既定の上限は 32,768 トークン。
- **キャッシュは自動**: 前回と先頭が一致する部分はキャッシュ入力の料金になる。
  設定は要らない。
- 価格の安い「Contributor」版 (送った内容を Meta の学習に使ってよい代わりに安くなる版)
  は同梱していない。

2026-10-27 に確認したレート制限は、両モデルともチームあたり毎分 3,000 リクエスト・
毎分 400 万トークン。モデルの提供状況は国によって異なり、Muse Wonder は韓国では
使えない。

プロバイダの「接続テスト」は `https://api.meta.ai/v1/models` を照会する。

- [クイックスタート (キー・認証)](https://dev.meta.ai/docs/quickstart)
- [モデル一覧](https://dev.meta.ai/docs/models)
- [料金とレート制限](https://dev.meta.ai/docs/pricing-rate-limits)
- [Chat Completions](https://dev.meta.ai/docs/protocols/chat-completions)
