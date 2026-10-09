# OpenAI APIキーの取得方法

## 1. OpenAIアカウントの作成

1. [OpenAI Platform](https://platform.openai.com/) にアクセス
2. 「Sign up」をクリックしてアカウントを作成
3. メールアドレスの確認を完了

## 2. APIキーの生成

1. ログイン後、左サイドバーから「API keys」を選択 ([API keys ページ](https://platform.openai.com/api-keys))
2. 「Create new secret key」をクリック
3. キーに名前を付けて（例: "SAIVerse"）「Create secret key」をクリック
4. 表示されたキーをコピー

> **重要**: APIキーはこの画面を閉じると二度と表示されません。必ずコピーして安全な場所に保管してください。

## 3. チャージ方法・料金について

OpenAI の API は、先にクレジットを買っておき、使った分だけそこから引かれる「前払い」の仕組みです。新しく作ったアカウントは、この前払いになります。

1. ログイン後、[API の請求の概要ページ (Billing)](https://platform.openai.com/account/billing) を開く
2. 「Add payment details」を押して、支払い情報を登録する
3. 最初に買うクレジットの金額を選ぶ (最低 $5、初期値は $10)
4. 「Use auto-reload」(残高が減ったら自動で買い足す設定) を確認する。**最初は オン になっています。** 自動で買い足したくない場合は オフ にしてから購入を確定する

残高が無くなったら、同じページの「Buy credits」か「Add to credit balance」から買い足します。

- **買ったクレジットの有効期限は 1 年です。** 期限は延ばせません。
- 残高が尽きてもすぐには止まらないことがあり、その間の利用はマイナスの残高として次の購入から差し引かれます。
- 詳しくは公式ヘルプの [前払いの請求について](https://help.openai.com/en/articles/8264644-setting-up-and-managing-prepaid-api-billing) を参照してください。

### データ共有で毎日もらえる無料トークン

組織の [データ共有の設定ページ](https://platform.openai.com/settings/organization/data-controls/sharing) で、API に送った入力と出力を OpenAI と共有する設定にすると、条件を満たす組織は毎日決まった量のトークンを無料で使えます (2026年10月時点、[公式ヘルプ](https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai)で確認)。

- **対象になるかどうかは組織ごとに決まります。** 設定ページに「You're eligible for free daily usage on traffic shared with OpenAI」と出ていれば対象です。出ていなければ、いまは対象外です。
- 対象のモデルは 2 つのグループに分かれていて、無料になる量はグループごとに別々に数えられます (同じグループの中のモデルは、一つの枠を分け合います)。1 日に無料になる量は、組織の段階によって次のとおりです。

| 対象のモデルのグループ | 段階が Build の組織 | 段階が Launch / Grow の組織 |
|--------------------|------------------|------------------------|
| 1 つ目のグループ (GPT-6 Astra、GPT-6.1 Sol、GPT-6 Sol、GPT-6 Luna、GPT-5.6 Sol、GPT-5.5、GPT-5.4、GPT-5.2、GPT-5.1、GPT-5、GPT-4.1、GPT-4o (2024-11-20)、o3 など) | 1 日 25 万トークン | 1 日 100 万トークン |
| 2 つ目のグループ (GPT-5.6 Terra、GPT-5.6 Luna、GPT-5.4 mini、GPT-5.4 nano、GPT-5 mini、GPT-5 nano など) | 1 日 250 万トークン | 1 日 1000 万トークン |

- 無料枠を使うにも、残高がプラスである必要があります。枠を超えた分は通常の料金になります。
- **共有した入力と出力は、OpenAI のモデルの評価や今後の学習に使われます。** ペルソナとの会話の中身がそのまま OpenAI に渡ることになるので、共有してよいかをよく考えてから設定してください。
- 対象のモデルの正確な一覧 (公式には日付つきの版の名前で載っています) は、上の公式ヘルプで確認してください。

### SAIVerse で選べるモデルの料金 (2026年10月時点、[公式の料金ページ](https://developers.openai.com/api/docs/pricing)で確認)

金額はすべて 100 万トークンあたりの米ドルで、通常の処理 (Standard) の単価です。「—」は、公式の料金表でその欄に値が載っていないことを示します。

| モデル | 入力 | キャッシュ読み出し | キャッシュ書き込み | 出力 | 27.2万トークン超の入力 | 27.2万トークン超の出力 |
|--------|------|-----------------|-----------------|------|---------------------|---------------------|
| GPT-6 Astra | $10 | $1 | $12.50 | $50 | $20 | $75 |
| GPT-6.1 Sol | $2 | $0.10 | $2.50 | $10 | $4 | $15 |
| GPT-6 Sol | $2 | $0.20 | $2.50 | $10 | $4 | $15 |
| GPT-6 Luna | $0.10 | $0.01 | $0.125 | $0.50 | $0.20 | $0.75 |
| GPT-5.6 Sol | $4 | $0.40 | $5 | $20 | $8 | $30 |
| GPT-5.6 Terra | $2 | $0.20 | $2.50 | $12 | $4 | $18 |
| GPT-5.6 Luna | $0.20 | $0.02 | $0.25 | $1.20 | $0.40 | $1.80 |
| GPT-5.5 | $5 | $0.50 | — | $30 | $10 | $45 |
| GPT-5.4 | $2.50 | $0.25 | — | $15 | $5 | $22.50 |
| GPT-5.4 Pro | $30 | — | — | $180 | $60 | $270 |
| GPT-5.4 mini | $0.75 | $0.075 | — | $4.50 | — | — |
| GPT-5.4 nano | $0.20 | $0.02 | — | $1.25 | — | — |
| GPT-5.2 | $1.75 | $0.175 | — | $14 | — | — |
| GPT-5.1 | $1.25 | $0.125 | — | $10 | — | — |
| GPT-5 | $1.25 | $0.125 | — | $10 | — | — |
| GPT-5 mini | $0.25 | $0.025 | — | $2 | — | — |
| GPT-5 nano | $0.05 | $0.005 | — | $0.40 | — | — |
| GPT-4.1 | $2 | $0.50 | — | $8 | — | — |
| GPT-4o (2024-11-20) | $2.50 | $1.25 | — | $10 | — | — |
| o3 | $2 | $0.50 | — | $8 | — | — |

- **一部のモデルは、プロンプトの長さで単価が変わります。** 1 回に送る入力が 27.2 万トークンを超えると、表の右の 2 列の単価になります (キャッシュの読み出しと書き込みも 2 倍になります)。SAIVerse のモデル一覧で表示名に「(< 272k)」と付いているモデルは、SAIVerse のモデル設定でコンテキスト長を 27.2 万トークンにしてあるものです。
- **GPT-5.6 Sol の単価はキャンペーン価格です。** 公式の説明では、入力を 20%、出力を 33% 値下げした価格で、少なくとも 2026年11月21日まではこの価格で提供するとしています。
- GPT-4o (2024-11-20) は、公式の料金表の「gpt-4o」の行の単価です。

### 提供終了が予定・告知されているモデル

公式の [提供終了の一覧](https://developers.openai.com/api/docs/deprecations) では、SAIVerse で選べるモデルのうち次のものが告知されています (2026年10月時点)。

- **GPT-5、GPT-5 mini、GPT-5 nano、o3**: 2026年12月11日に API から外れる予定です。
- **GPT-5.1、GPT-5.4 nano**: 2027年4月1日に API から外れる予定です。
- **GPT-5 Chat、GPT-5.1 Chat** (`gpt-5-chat-latest` / `gpt-5.1-chat-latest`): 2026年7月23日に提供が終わったと告知されています。
- **GPT-5.2 Chat、GPT-5.3 Chat** (`gpt-5.2-chat-latest` / `gpt-5.3-chat-latest`): 2026年8月10日に提供が終わったと告知されています。

提供が終わった GPT-5 / 5.1 / 5.2 / 5.3 Chat の 4 つは、2026年10月9日に SAIVerse の組み込みから外しました。選んでいた場合は、別のモデルを選び直してください。

## 4. 利用可能なモデル

公式がいま最初に勧めているのは次の 3 つです。

- **GPT-6 Astra**: 複雑な推論やコーディング向けの、いま一番性能の高いフラッグシップモデル
- **GPT-6.1 Sol**: Astra に近い性能を、より安く使えるモデル。賢さと料金のバランス型
- **GPT-6 Luna**: 量が多く料金を抑えたい作業向けの、一番効率のよいモデル

SAIVerse では、このほかに GPT-6 Sol、GPT-5.6 の 3 モデル (Sol / Terra / Luna)、GPT-5.5、GPT-5.4 の系列 (無印 / Pro / mini / nano)、GPT-5.2、GPT-5.1、GPT-5 の系列 (無印 / mini / nano)、GPT-4.1、GPT-4o (2024-11-20)、o3 も選べます。料金は上の表のとおりです。

### 反射判断用の「GPT-6 Luna (Decisions)」

SAIVerse には、**反射判断** (ペルソナが会話の裏で行う小さな判断の層) に割り当てられるモデル「GPT-6 Luna (Decisions)」があります。これは OpenAI の Decisions API という判断専用の API を使うもので、会話モデルとしては使えません。この API キー (`OPENAI_API_KEY`) がそのまま使われるので、追加の設定は要りません。

- 料金は**入力だけ**で、100 万トークンあたり $0.10 です。出力、キャッシュの読み出し、キャッシュの書き込みには料金がかかりません (2026年10月時点、[公式の Decisions のガイド](https://developers.openai.com/api/docs/guides/decisions)で確認)。
- Decisions API は公開ベータの段階です。いま使えるモデルは `gpt-6-luna` だけです。

### ChatGPT のサブスクリプションで使う「Codex」経由のモデル

SAIVerse のモデル一覧で表示名が「〜 Codex」で終わるモデル (GPT-6 Astra Codex、GPT-6.1 Sol Codex、GPT-6 Sol Codex、GPT-6 Luna Codex、GPT-5.6 Sol / Terra / Luna Codex、GPT-5.5 Codex) は、この API キーではなく、ChatGPT のアカウントでのログインで動きます。API の残高からは引かれず、ChatGPT のプランの利用上限の中で使われます。ログインのしかたは [プロバイダのリファレンス](../reference/providers.md) の「OpenAI Codex は API キーでなくログインで認証する」を参照してください。

- どのモデルが使えるかは、ChatGPT のプランによって違います。公式の [Codex のモデルの案内](https://developers.openai.com/codex/models) では、たとえば GPT-6.1 Sol は Plus、Pro、Business、Enterprise、Edu のプランが対象で、Free と Go は提供開始の時点では対象外です。
- **GPT-5.5 は、2026年10月14日に Codex (ChatGPT でのログイン) から外れます。** 公式は、Plus 以上のプランでは GPT-6 Sol、Free と Go のプランでは GPT-6 Luna への切り替えを勧めています。API キーで使う GPT-5.5 には影響しません。

## 5. 使用量の確認

[Usage ページ](https://platform.openai.com/settings/organization/usage) で使用量とコストを確認できます。

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 参考リンク

- [OpenAI Platform](https://platform.openai.com/)
- [料金ページ (API)](https://developers.openai.com/api/docs/pricing)
- [モデルの一覧](https://developers.openai.com/api/docs/models)
- [提供終了の一覧](https://developers.openai.com/api/docs/deprecations)
