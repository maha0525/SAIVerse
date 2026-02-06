# OpenRouter APIキーの取得方法

## 概要

OpenRouterは複数のAIプロバイダー（OpenAI、Anthropic、Google、Meta等）のモデルに統一APIでアクセスできるサービスです。1つのAPIキーで様々なモデルを利用できます。

## 1. OpenRouterアカウントの作成

1. [OpenRouter](https://openrouter.ai/) にアクセス
2. 右上の「Sign In」をクリック
3. Google、GitHub、またはメールでアカウント作成

## 2. APIキーの生成

1. ログイン後、[Keys ページ](https://openrouter.ai/keys) に移動
2. 「Create Key」をクリック
3. キーに名前を付けて作成
4. 表示されたAPIキーをコピー

## 3. クレジットの追加

1. [Credits ページ](https://openrouter.ai/credits) に移動
2. 希望の金額を選択（$5〜）
3. クレジットカードまたは暗号通貨で支払い

## 4. 料金について

OpenRouterは各プロバイダーの料金に少額のマージンを追加した価格設定です。

### 人気モデルの例（参考）
| モデル | 入力 | 出力 |
|--------|------|------|
| GPT-4o | $2.50/1M | $10.00/1M |
| Claude Sonnet 4 | $3.00/1M | $15.00/1M |
| Llama 3.3 70B | $0.40/1M | $0.40/1M |
| Gemini 2.5 Pro | $1.25/1M | $10.00/1M |

### 無料モデル
一部のオープンソースモデルは無料で利用可能です（レート制限あり）。

## 5. 利点

- **統一API**: 複数プロバイダーを1つのAPIで利用
- **フォールバック**: あるモデルが利用不可の場合、自動で別モデルに切り替え可能
- **コスト管理**: 使用量の詳細な追跡
- **モデル比較**: 同じプロンプトで複数モデルを比較

## 6. 利用可能なモデル

OpenRouterでは200以上のモデルが利用可能です：
- OpenAI (GPT-4o, o1, etc.)
- Anthropic (Claude Opus, Sonnet, Haiku)
- Google (Gemini Pro, Flash)
- Meta (Llama 3.3)
- Mistral (Large, Medium)
- その他多数

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 参考リンク

- [OpenRouter](https://openrouter.ai/)
- [モデル一覧](https://openrouter.ai/models)
- [ドキュメント](https://openrouter.ai/docs)
- [料金](https://openrouter.ai/docs/pricing)
