# Anthropic APIキーの取得方法

## 1. Anthropicアカウントの作成

1. [Claude Console](https://platform.claude.com/) にアクセス (以前の `console.anthropic.com` は、いまはこのアドレスへ転送されます)
2. 「Sign up」をクリックしてアカウントを作成
3. メールアドレスの確認を完了

## 2. APIキーの生成

1. ログイン後、「API Keys」セクション ([API Keys ページ](https://platform.claude.com/settings/keys)) に移動
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

### SAIVerse で選べるモデルの料金 (2026年10月時点、[公式の料金ページ](https://platform.claude.com/docs/en/about-claude/pricing)で確認)

金額はすべて 100 万トークンあたりの米ドルです。「キャッシュ書き込み」と「キャッシュ読み出し」は、下の「プロンプトキャッシュ」で説明する仕組みの料金です。

| モデル | 入力 | 出力 | キャッシュ書き込み (5分) | キャッシュ書き込み (1時間) | キャッシュ読み出し |
|--------|------|------|----------------------|------------------------|------------------|
| Claude Fable 5.1 | $10 | $50 | $12.50 | $20 | $0.25 |
| Claude Opus 5.5 | $4 | $20 | $5 | $8 | $0.20 |
| Claude Sonnet 5.5 | $2 | $10 | $2.50 | $4 | $0.10 |
| Claude Haiku 5.5 (プロンプトが 10 万トークン以下) | $0.10 | $0.50 | $0.125 | $0.20 | $0.01 |
| Claude Haiku 5.5 (プロンプトが 10 万トークン超) | $0.50 | $2.50 | $0.625 | $1 | $0.05 |
| Claude Fable 5 | $10 | $50 | $12.50 | $20 | $1 |
| Claude Opus 5 | $5 | $25 | $6.25 | $10 | $0.50 |
| Claude Sonnet 5 | $2 | $10 | $2.50 | $4 | $0.20 |
| Claude Opus 4.8 | $5 | $25 | $6.25 | $10 | $0.50 |
| Claude Opus 4.7 | $5 | $25 | $6.25 | $10 | $0.50 |
| Claude Opus 4.6 | $5 | $25 | $6.25 | $10 | $0.50 |
| Claude Sonnet 4.6 | $3 | $15 | $3.75 | $6 | $0.30 |
| Claude Opus 4.5 | $5 | $25 | $6.25 | $10 | $0.50 |
| Claude Sonnet 4.5 (提供終了予定) | $3 | $15 | $3.75 | $6 | $0.30 |
| Claude Haiku 4.5 | $1 | $5 | $1.25 | $2 | $0.10 |

- **Claude Haiku 5.5 だけは、プロンプトの長さで単価が変わります。** 1 回に送るプロンプトが 10 万トークンを超えると、そのリクエストは表の下の段の単価になります。ほかのモデル (Claude 4.6 以降) は、長いプロンプトでも単価は変わりません。
- **Claude 4.7 以降のモデルは、トークンの数え方 (トークナイザー) が新しくなっています。** 公式の説明では、同じ文章でもおよそ 3 割多くトークンが数えられます。古いモデルと料金を比べるときは、単価だけでなくこの差も考えに入れてください。
- **Claude Sonnet 4.5 は提供終了が予定されています。** 公式の告知では、2026年11月30日に API から外れ、後継には Claude Sonnet 5.5 が推奨されています ([モデルの提供終了の一覧](https://platform.claude.com/docs/en/about-claude/model-deprecations))。

### プロンプトキャッシュ

Anthropic には「Prompt Caching」という仕組みがあり、前のリクエストと同じ部分 (ペルソナの設定や会話履歴の前半など) を保存しておいて、次のリクエストで安く読み直せます。保存するとき (書き込み) は入力の単価より高く、読み直すとき (読み出し) は入力の単価よりずっと安くなります。

| キャッシュの操作 | 料金 |
|--------------|------|
| キャッシュ書き込み (5分間有効) | 入力の単価の 1.25 倍 |
| キャッシュ書き込み (1時間有効) | 入力の単価の 2 倍 |
| キャッシュ読み出し | 入力の単価の 0.1 倍 (Claude Fable 5.1 は 0.025 倍、Claude Opus 5.5 と Claude Sonnet 5.5 は 0.05 倍) |

## 4. 利用可能なモデル

公式がいま「現行のモデル」として並べているのは次の 4 つです。迷ったら、公式の案内どおり Claude Opus 5.5 から始めるのがおすすめです。

- **Claude Fable 5.1**: 難しい推論や、長時間かかるエージェント的な作業向けの最上位モデル
- **Claude Opus 5.5**: 長時間のコーディングや知的作業向け。公式が「多くの用途ではまずこれ」と勧めているモデル (推奨)
- **Claude Sonnet 5.5**: 速さと賢さのバランスが一番よいモデル
- **Claude Haiku 5.5**: 分類や振り分けのような、量が多く速さが要る作業向けの、最速・最安のモデル

この 4 つは、どれも一度に 100 万トークンまで読めます。

SAIVerse では、このほかに一つ前までの世代 (Claude Fable 5、Opus 5、Sonnet 5、Opus 4.8、Opus 4.7、Opus 4.6、Sonnet 4.6、Opus 4.5、Sonnet 4.5、Haiku 4.5) も選べます。料金は上の表のとおりです。

## 5. 使用量の確認

[Usage ページ](https://platform.claude.com/usage) で使用量とコストを確認できます。

## 6. 支払い設定

初回利用時にクレジットカードの登録が必要です (Max / Team プランの月額クレジットだけで使う場合は不要。上の「Max / Team プランの月額 API クレジット」を参照)。
[Billing ページ](https://platform.claude.com/settings/billing) で設定できます。

## 環境変数

SAIVerse は次の環境変数名だけを読みます。
```
CLAUDE_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> **注意**: Anthropic 公式の SDK などで使われる `ANTHROPIC_API_KEY` という名前は、SAIVerse では読まれません。キーは必ず `CLAUDE_API_KEY` に入れてください。

## 参考リンク

- [Claude Console](https://platform.claude.com/)
- [APIドキュメント](https://platform.claude.com/docs/en/home)
- [モデルの一覧](https://platform.claude.com/docs/en/models/overview)
- [料金ページ (API)](https://platform.claude.com/docs/en/about-claude/pricing)
