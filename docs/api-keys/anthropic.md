# Anthropic APIキーの取得方法

## 1. Anthropicアカウントの作成

1. [Anthropic Console](https://console.anthropic.com/) にアクセス
2. 「Sign up」をクリックしてアカウントを作成
3. メールアドレスの確認を完了

## 2. APIキーの生成

1. ログイン後、「API Keys」セクションに移動
2. 「Create Key」をクリック
3. キーに名前を付けて（例: "SAIVerse"）作成
4. 表示されたAPIキーをコピー

> **重要**: APIキーは作成時に一度だけ表示されます。必ずコピーして安全な場所に保管してください。

## 3. 料金について

### 主なモデルの料金（参考）
| モデル | 入力 | 出力 |
|--------|------|------|
| Claude Opus 4.5 | $15.00/1M tokens | $75.00/1M tokens |
| Claude Sonnet 4 | $3.00/1M tokens | $15.00/1M tokens |
| Claude Haiku 3.5 | $0.80/1M tokens | $4.00/1M tokens |

### プロンプトキャッシュ
Anthropicは「Prompt Caching」機能を提供しており、同じプロンプトの再利用でコストを90%削減できます。

| キャッシュ種別 | 料金 |
|--------------|------|
| キャッシュ書き込み | 入力料金の1.25倍 |
| キャッシュ読み取り | 入力料金の0.1倍 |

## 4. 利用可能なモデル

- **Claude Opus 4.5**: 最高性能、複雑なタスク向け
- **Claude Sonnet 4**: バランス型（推奨）
- **Claude Haiku 3.5**: 高速・低コスト、軽量タスク向け

## 5. 使用量の確認

[Usage ページ](https://console.anthropic.com/settings/usage) で使用量とコストを確認できます。

## 6. 支払い設定

初回利用時にクレジットカードの登録が必要です。
[Billing ページ](https://console.anthropic.com/settings/billing) で設定できます。

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

または
```
CLAUDE_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 参考リンク

- [Anthropic Console](https://console.anthropic.com/)
- [APIドキュメント](https://docs.anthropic.com/)
- [料金ページ](https://www.anthropic.com/pricing)
