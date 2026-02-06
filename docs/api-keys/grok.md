# Grok (xAI) APIキーの取得方法

## 1. xAIアカウントの作成

1. [xAI Console](https://console.x.ai/) にアクセス
2. アカウントを作成（X/Twitterアカウントでログイン可能）
3. 利用規約に同意

## 2. APIキーの生成

1. ログイン後、「API Keys」セクションに移動
2. 「Create API Key」をクリック
3. キーに名前を付けて作成
4. 表示されたAPIキーをコピー

> **重要**: APIキーは作成時に一度だけ表示されます。必ずコピーして安全な場所に保管してください。

## 3. 料金について

### 主なモデルの料金（参考）
| モデル | 入力 | 出力 |
|--------|------|------|
| Grok-3 | $3.00/1M tokens | $15.00/1M tokens |
| Grok-3-mini | $0.30/1M tokens | $0.50/1M tokens |
| Grok-2 | $2.00/1M tokens | $10.00/1M tokens |

## 4. 利用可能なモデル

- **Grok-3**: 最新・最高性能モデル
- **Grok-3-mini**: 軽量・高速モデル（推奨：軽量タスク向け）
- **Grok-2**: 旧世代モデル

## 5. 特徴

- **リアルタイム情報**: X(Twitter)との統合により最新情報にアクセス可能
- **長いコンテキスト**: 最大128Kトークンのコンテキスト長
- **マルチモーダル**: 画像理解機能（一部モデル）

## 6. 無料クレジット

新規登録時に$25の無料クレジットが付与される場合があります。

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
XAI_API_KEY=xai-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 参考リンク

- [xAI Console](https://console.x.ai/)
- [xAI API ドキュメント](https://docs.x.ai/)
- [料金ページ](https://x.ai/pricing)
