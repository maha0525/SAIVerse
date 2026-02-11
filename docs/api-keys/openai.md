# OpenAI APIキーの取得方法

## 1. OpenAIアカウントの作成

1. [OpenAI Platform](https://platform.openai.com/) にアクセス
2. 「Sign up」をクリックしてアカウントを作成
3. メールアドレスの確認を完了

## 2. APIキーの生成

1. ログイン後、右上のアカウントアイコンをクリック
2. 「View API keys」または「API keys」を選択
3. 「Create new secret key」をクリック
4. キーに名前を付けて（例: "SAIVerse"）「Create secret key」をクリック
5. 表示されたキーをコピー

> **重要**: APIキーはこの画面を閉じると二度と表示されません。必ずコピーして安全な場所に保管してください。

## 3. 料金について

- **従量課金制**: 使った分だけ支払い
- **新規登録時**: 無料クレジットが付与される場合あり（$5程度）
- **詳細**: [OpenAI Pricing](https://openai.com/pricing)

### 主なモデルの料金（参考・2026年2月時点）
| モデル | 入力 | キャッシュ入力 | 出力 |
|--------|------|---------------|------|
| GPT-5.2 | $1.75/1M tokens | $0.175/1M tokens | $14.00/1M tokens |
| GPT-5.1 | $1.25/1M tokens | $0.125/1M tokens | $10.00/1M tokens |
| GPT-5-mini | $0.25/1M tokens | $0.025/1M tokens | $2.00/1M tokens |
| GPT-5-nano | $0.05/1M tokens | $0.005/1M tokens | $0.40/1M tokens |
| GPT-4o (2024-11-20) | $2.50/1M tokens | $1.25/1M tokens | $10.00/1M tokens |
| o3 | $2.00/1M tokens | $0.50/1M tokens | $8.00/1M tokens |

## 4. 利用可能なモデル

- **GPT-5.2**: 最新のフラッグシップモデル（推奨）
- **GPT-5.1**: GPT-5系の高性能モデル
- **GPT-5-mini**: 軽量・低価格版
- **GPT-5-nano**: 超軽量・超低価格版
- **GPT-4o (2024-11-20)**: マルチモーダル対応モデル
- **o3**: 推論特化モデル

## 5. 使用量の確認

[Usage ページ](https://platform.openai.com/usage) で使用量とコストを確認できます。

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```
