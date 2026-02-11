# Google Gemini（有料版）APIキーの取得方法

## 概要

有料版Geminiは2つの方法で利用できます：
1. **Google AI Studio（Pay-as-you-go）**: 簡単に始められる
2. **Vertex AI**: エンタープライズ向け、より高度な機能

ここではGoogle AI Studioの有料版について説明します。

## 1. Google Cloud請求先アカウントの設定

1. [Google Cloud Console](https://console.cloud.google.com/) にアクセス
2. 「お支払い」からクレジットカードを登録
3. 請求先アカウントを作成

## 2. APIキーの生成

1. [Google AI Studio](https://aistudio.google.com/) にアクセス
2. 左サイドバーの「Get API key」をクリック
3. 「Create API key」をクリック
4. 請求先アカウントが紐づいたプロジェクトを選択
5. 生成されたAPIキーをコピー

## 3. 料金について

### 主なモデルの料金（参考・2026年2月時点）
| モデル | 入力 | キャッシュ入力 | 出力 |
|--------|------|---------------|------|
| Gemini 3 Pro | $2.00/1M tokens | $0.20/1M tokens | $12.00/1M tokens |
| Gemini 3 Flash | $0.50/1M tokens | $0.05/1M tokens | $3.00/1M tokens |
| Gemini 2.5 Pro | $1.25/1M tokens | $0.125/1M tokens | $10.00/1M tokens |
| Gemini 2.5 Flash | $0.30/1M tokens | $0.03/1M tokens | $2.50/1M tokens |
| Gemini 2.5 Flash Lite | $0.10/1M tokens | $0.01/1M tokens | $0.40/1M tokens |

> **注意**: Gemini 3 Pro / 2.5 Proは200Kトークンを超えるプロンプトで料金が上がります（入力・キャッシュ・出力とも約1.5〜2倍）。

### 特徴
- **従量課金制**: 使った分だけ支払い
- **キャッシュ割引**: 繰り返しコンテンツは割引（入力料金の10分の1）
- **コンテキストキャッシュ**: 長いプロンプトの再利用でコスト削減

## 4. 利用可能なモデル

- **Gemini 3 Pro**: 最新・最高性能（推奨）
- **Gemini 3 Flash**: 最新の高速モデル
- **Gemini 2.5 Pro**: 高性能モデル
- **Gemini 2.5 Flash**: 高速・コスト効率（軽量タスク向け）
- **Gemini 2.5 Flash Lite**: 超軽量・超低価格版

## 5. レート制限（有料版）

モデルとTierによって変化します。AIStudio内でレート制限をご確認ください。
https://aistudio.google.com/usage


## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
GEMINI_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

## 参考リンク

- [Google AI Studio](https://aistudio.google.com/)
- [Gemini API Pricing](https://ai.google.dev/pricing)
- [Vertex AI（エンタープライズ向け）](https://cloud.google.com/vertex-ai)
