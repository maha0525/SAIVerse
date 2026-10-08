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

## Max / Team プランの月額 API クレジット (2026年10月〜)

Claude の Max プランか Team プランを使っているなら、毎月の API クレジットを受け取って、SAIVerse の Claude のペルソナの利用料に充てられます。SAIVerse 側の設定は、いつもどおり API キーを入れるだけです。

| プラン | 毎月のクレジット |
|--------|----------------|
| Max 5x | $100 |
| Max 20x | $200 |
| Team | 1 席あたり $20 (Standard) / $100 (Premium)、チーム全体で合算して上限 $500 |

### 受け取り方

1. [claude.ai](https://claude.ai/) の「設定 › 請求」(Team は「組織の設定 › 請求」) を開き、「API クレジット」の欄の「組織をリンク」を押す。
2. クレジットを受け取る Console の組織を選ぶ (無ければその場で作れます)。利用条件を確認してリンクする。
3. その組織で API キーを作り (上の「2. APIキーの生成」)、SAIVerse のグローバル設定の「環境」タブで `CLAUDE_API_KEY` に入れる。すでに入れているキーがその組織のものなら、何もしなくて構いません。

受け取れたかどうかは、Console の Settings › Billing の「Promotional credits」(画面のトップの「組織のクレジット」) で確かめられます。

### 知っておくこと

- **リンクできる組織は一つだけで、あとから自分では変えられません** (変えるにはサポートへの連絡が必要)。SAIVerse で使う API キーがある組織を選ぶのが確実です。
- **余ったクレジットは繰り越されません。** 請求の周期ごとに失効し、次の周期に新しく入ります。
- その組織の API キーなら、どれを使っても同じ残高から引かれます。
- 使う順番は、このクレジットが先、組織で購入したクレジットが後です。どちらも無くなると、次のクレジットが入るまで API が止まります (Claude のプランの料金に上乗せで請求されることはありません)。SAIVerse では、このとき「APIの利用料金が上限に達しました。APIキーの残高や支払い設定を確認してください。」というエラーになり、自動でやり直しはしません。
- 受け取るだけなら、Console にクレジットカードを登録する必要はありません。
- 対象は Claude API (Messages API と Message Batches API) などで、対話で使う Claude Code や、AWS・Google Cloud・Microsoft 経由の Claude には使えません。
- 新しく Max / Team を契約した場合は、7 日たってから受け取れます。

詳しい条件: [Claude Platform のドキュメント](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers) / [ヘルプセンター](https://support.claude.com/en/articles/17154008-monthly-api-credits-for-max-and-team-plans)

## 3. 料金について

### 主なモデルの料金（参考・2026年2月時点）
| モデル | 入力 | キャッシュヒット | 出力 |
|--------|------|----------------|------|
| Claude Opus 4.6 | $5.00/1M tokens | $0.50/1M tokens | $25.00/1M tokens |
| Claude Opus 4.5 | $5.00/1M tokens | $0.50/1M tokens | $25.00/1M tokens |
| Claude Sonnet 4.5 | $3.00/1M tokens | $0.30/1M tokens | $15.00/1M tokens |
| Claude Haiku 4.5 | $1.00/1M tokens | $0.10/1M tokens | $5.00/1M tokens |

### プロンプトキャッシュ
Anthropicは「Prompt Caching」機能を提供しており、キャッシュヒット時にコストを90%削減できます。

| キャッシュ種別 | 料金 |
|--------------|------|
| キャッシュ書き込み（5分TTL） | 入力料金の1.25倍 |
| キャッシュ書き込み（1時間TTL） | 入力料金の2倍 |
| キャッシュ読み取り（ヒット） | 入力料金の0.1倍 |

## 4. 利用可能なモデル

- **Claude Opus 4.6**: 最新・最高性能、エージェント・コーディング向け（推奨）
- **Claude Opus 4.5**: 高性能モデル
- **Claude Sonnet 4.5**: 速度と性能のバランス型
- **Claude Haiku 4.5**: 高速・低コスト、軽量タスク向け

## 5. 使用量の確認

[Usage ページ](https://console.anthropic.com/settings/usage) で使用量とコストを確認できます。

## 6. 支払い設定

初回利用時にクレジットカードの登録が必要です (Max / Team プランの月額クレジットだけで使う場合は不要。上の「Max / Team プランの月額 API クレジット」を参照)。
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
