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

### 主なモデルの料金（参考）
| モデル | 入力 | 出力 |
|--------|------|------|
| GPT-4o | $2.50/1M tokens | $10.00/1M tokens |
| GPT-4o mini | $0.15/1M tokens | $0.60/1M tokens |
| o1 | $15.00/1M tokens | $60.00/1M tokens |

## 4. 利用可能なモデル

- **GPT-4o**: 最新のマルチモーダルモデル（推奨）
- **GPT-4o mini**: 軽量・低価格版
- **o1 / o1-mini**: 推論特化モデル
- **o3**: 最新の推論モデル

## 5. 使用量の確認

[Usage ページ](https://platform.openai.com/usage) で使用量とコストを確認できます。

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```
