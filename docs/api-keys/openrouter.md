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

### SAIVerse設定済みモデルの料金（参考・2026年2月時点）
| モデル | 入力 | 出力 |
|--------|------|------|
| Z.ai GLM-4.7 | $0.40/1M | $1.75/1M |
| Z.ai GLM-4.6v | $0.30/1M | $0.90/1M |
| Z.ai GLM-4.7 Flash | $0.06/1M | $0.40/1M |
| DeepSeek V3.2 | $0.26/1M | $0.38/1M |
| MiniMax M2.1 | $0.27/1M | $0.95/1M |
| Kimi K2.5 | $0.45/1M | $2.25/1M |
| Qwen 3 Next 80B | $0.09/1M | $1.10/1M |
| GPT-OSS 120B | $0.039/1M | $0.19/1M |
| GPT-OSS 20B | $0.03/1M | $0.14/1M |

### 無料モデル
以下のモデルは無料で利用可能です（レート制限あり）：
- Step 3.5 Flash / Trinity Large Preview / Qwen 3 Next 80B / Qwen 3 Coder 480B / GPT-OSS 120B・20B

> **注意**: 最新の料金は [OpenRouter Models](https://openrouter.ai/models) で確認してください。

## 5. 利点

- **統一API**: 複数プロバイダーを1つのAPIで利用
- **フォールバック**: あるモデルが利用不可の場合、自動で別モデルに切り替え可能
- **コスト管理**: 使用量の詳細な追跡
- **無料モデル**: オープンソースモデルの一部が無料で利用可能

## 6. SAIVerse設定済みモデル

OpenRouterでは200以上のモデルが利用可能です。SAIVerseに設定済みのモデル：

**有料モデル**:
- Z.ai GLM-4.7 / GLM-4.7 Flash / GLM-4.6v（画像対応）
- DeepSeek V3.2 / Kimi K2.5（画像対応）/ MiniMax M2.1
- Qwen 3 Next 80B-A3B / GPT-OSS 120B・20B

**無料モデル**:
- Step 3.5 Flash / Trinity Large Preview
- Qwen 3 Next 80B-A3B / Qwen 3 Coder 480B-A35B
- GPT-OSS 120B・20B

## 7. アプリ名の申告について

SAIVerse は OpenRouter へのリクエストに「SAIVerse」というアプリ名を添えて送ります。これにより、SAIVerse 経由の利用量が [OpenRouter の公開アプリランキング](https://openrouter.ai/apps) に SAIVerse として合算されます。

集計されるのは**利用したトークン量とモデル名だけ**で、会話の内容やペルソナの情報がランキングに出ることはありません。

申告を止めたい場合は、次の2つを行ってください。

**1.** `~/.saiverse/user_data/providers/openrouter.json` を以下の内容で作成する（このファイルが同梱設定より優先されます）。

```json
{
  "id": "openrouter",
  "display_name": "OpenRouter",
  "protocol": "openai_compat",
  "base_url": "https://openrouter.ai/api/v1",
  "api_key_env": "SAIVERSE_PROVIDER_OPENROUTER_API_KEY"
}
```

**2.** `.env` の API キーを、この設定が参照する名前に変更する。

```
SAIVERSE_PROVIDER_OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

手順が2段になる理由は2つあります。ユーザー設定は同梱設定とのマージではなく**丸ごと差し替え**なので接続情報も書く必要があること。そして**利用者が作ったプロバイダ設定は、同梱設定と同じ環境変数名を使えない**こと（API キーが本来と無関係な送信先と結び付けられるのを防ぐ仕組みで、`SAIVERSE_PROVIDER_<プロバイダID大文字>_API_KEY` という名前だけが許されます）。

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
