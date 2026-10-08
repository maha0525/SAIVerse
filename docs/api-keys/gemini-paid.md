# Google Gemini（有料版）APIキーの取得方法

## 概要

有料版の Gemini は、Google AI Studio で API キーのプロジェクトに支払い方法を設定すると使えるようになります。無料版と同じキーのまま、プロジェクトを有料版に切り替える形です。

企業向けには Google Cloud の [Gemini Enterprise Agent Platform](https://cloud.google.com/products/gemini-enterprise-agent-platform) という別の窓口もありますが、SAIVerse で使うのは AI Studio の API キーです。ここでは AI Studio の有料版について説明します。

## 1. APIキーの用意

1. [Google AI Studio](https://aistudio.google.com/) にアクセスし、Googleアカウントでログインします。
2. AI Studio の [API キーのページ](https://aistudio.google.com/api-keys) を開きます。
3. 初めて使う人には、プロジェクトとキーが1つずつ自動で作られています。新しく作りたいときは「Create API key」を押します。

## 2. 支払いの設定（有料版への切り替え）

1. AI Studio の [API キーのページ](https://aistudio.google.com/api-keys) か [プロジェクトのページ](https://aistudio.google.com/projects) で、有料版にしたいプロジェクトの「Set up billing」を押します。
2. 初めて Google の請求先アカウントを作る場合は、国を選んで利用規約に同意し、連絡先と支払い方法を入力します。すでに請求先アカウントがある場合は、その中から選びます。
3. 新しく始める人は、原則として「前払い（Prepay）」になり、最低 5 ドル分のクレジットを先に購入します。アカウントの状態によっては、後払い（Postpay）が選べたり、しばらく後払いが割り当てられたりします。

前払いでは、使った分がクレジットの残高から引かれていきます。残高が 0 ドルになると、その請求先アカウントにつながったすべての API キーが止まり、クレジットを追加するまでエラーになります。自動で残高を補充する設定（auto-reload）もあります。購入したクレジットの有効期限は 12 か月で、原則として返金されません。

残高と使用量は AI Studio の [請求のページ](https://aistudio.google.com/billing) と [使用量のページ](https://aistudio.google.com/usage) で確認できます。

（2026年10月時点、公式の請求ガイドで確認）

## 3. 料金について

### 文章を扱うモデルの料金（2026年10月時点、[公式の料金ページ](https://ai.google.dev/gemini-api/docs/pricing)で確認）

どれも 100 万トークンあたりの米ドルで、通常の（Batch や Priority ではない）呼び出しの値段です。キャッシュ入力は、コンテキストキャッシュから読み出した入力の値段です。

| モデル | 入力 | キャッシュ入力 | 出力 |
|--------|------|---------------|------|
| Gemini 3.8 Flash | $0.75（2027年1月から $1.50） | $0.075（2027年1月から $0.15） | $3.75（2027年1月から $7.50） |
| Gemini 3.7 Flash | $0.75（2027年1月から $1.50） | $0.075（2027年1月から $0.15） | $3.75（2027年1月から $7.50） |
| Gemini 3.6 Flash | $0.75（2027年1月から $1.50） | $0.075（2027年1月から $0.15） | $3.75（2027年1月から $7.50） |
| Gemini 3.5 Flash | $1.50 | $0.15 | $9.00 |
| Gemini 3.5 Flash-Lite | $0.30 | $0.03 | $2.50 |
| Gemini 3.1 Flash-Lite | $0.25 | $0.025 | $1.50 |
| Gemini 3 Flash Preview | $0.50 | $0.05 | $3.00 |
| Gemini 3.1 Pro Preview | $2.00 | $0.20 | $12.00 |
| Gemini 2.5 Pro | $1.25 | $0.125 | $10.00 |
| Gemini 2.5 Flash | $0.30 | $0.03 | $2.50 |
| Gemini 2.5 Flash-Lite | $0.10 | $0.01 | $0.40 |

- Gemini 3.8 / 3.7 / 3.6 Flash は、2026年12月31日までは導入価格で、2027年1月1日から括弧内の値段に上がります。
- 出力の値段には、モデルが答える前に考える「思考」のトークンも含まれます。
- 音声を入力する場合は、モデルによって入力の値段が上の表より高くなります。
- Gemini 3.1 Pro Preview と Gemini 2.5 Pro は、1回の入力が 20 万トークンを超えると値段が上がります。3.1 Pro Preview は入力 $4.00・キャッシュ入力 $0.40・出力 $18.00、2.5 Pro は入力 $2.50・キャッシュ入力 $0.25・出力 $15.00 になります。
- コンテキストキャッシュには、読み出しの値段とは別に、キャッシュを置いておく時間に応じた保管料がかかります。Flash 系は 100 万トークン・1時間あたり $1.00（3.8 / 3.7 / 3.6 Flash は 2026年中は $0.50）、Pro 系は $4.50 です。

### 画像生成の料金（2026年10月時点、[公式の料金ページ](https://ai.google.dev/gemini-api/docs/pricing)で確認）

SAIVerse の `generate_image` ツールで Nano Banana 系を選ぶと、次のモデルが使われます。1枚あたりの値段は、Google が公式ページに載せている換算値です。

| ツールでの名前 | 実際のモデル | 1K | 2K | 4K |
|--------------|------------|----|----|----|
| `nano_banana_2_1` | Nano Banana 2.1（`gemini-nano-banana-2.1`） | $0.0336 / 枚 | $0.0504 / 枚 | $0.113 / 枚 |
| `nano_banana_2` | Nano Banana 2（`gemini-3.1-flash-image`） | $0.067 / 枚 | $0.101 / 枚 | $0.151 / 枚 |
| `nano_banana_pro` | Nano Banana Pro（`gemini-3-pro-image`） | $0.134 / 枚 | $0.134 / 枚 | $0.24 / 枚 |

- Nano Banana 2.1 は 2026年10月6日に正式リリースされた、Nano Banana 2 の後継モデルです。Google は同じ日に Nano Banana 2（`gemini-3.1-flash-image`）を非推奨にしました（終了日はまだ発表されていません）。
- `nano_banana_2_1` と `nano_banana_2` では、ツール側の品質が `low` なら 1K、`medium` なら 2K、`high` 以上なら 4K で生成します。
- 画像生成には有料版のキー（`GEMINI_API_KEY`）が必要です。

### 特徴
- **従量課金制**: 使った分だけ支払います（前払いの場合は、先に買ったクレジットから引かれます）。
- **コンテキストキャッシュ**: 同じ長い入力を繰り返し送るとき、読み出しの値段が通常の入力の10分の1になります。
- **データの扱い**: 有料版で送った内容は、Google の製品改善に使われません。

## 4. SAIVerse で使える有料版のモデル

SAIVerse のモデル選択では、名前に「(有料Tier)」が付いているものが有料版のキーで動きます。

- **Gemini 3.8 Flash(有料Tier)**: Google が「いちばん賢い Flash モデル」と位置づけている最新のモデルです。迷ったらこれを選んでください。
- **Gemini 3.7 Flash(有料Tier)** / **Gemini 3.6 Flash(有料Tier)**: ひとつ前の世代の Flash モデルです。2026年中は 3.8 Flash と同じ値段です。
- **Gemini 3.5 Flash(有料Tier)**: さらに前の世代の Flash モデルです。いまは 3.8 Flash より高い値段になっています。
- **Gemini 3.5 Flash-Lite(有料Tier)** / **Gemini 3.1 Flash-Lite(有料Tier)**: 安くて速い軽量モデルです。
- **Gemini 3 Flash(有料Tier)**: 旧世代のプレビュー版モデルです。
- **Gemini 3.1 Pro Preview(有料Tier, <200k)**: Pro 系のプレビュー版モデルです。無料枠では使えません。
- **Gemini 2.5 Pro (有料Tier, <200k)** / **Gemini 2.5 Flash(有料Tier)** / **Gemini 2.5 Flash Lite(有料Tier)**: 旧世代のモデルです。2026年9月から、Google は Gemini 2.5 系を「過去に使っていた人」に限って提供しています。これから始める人は、3.5 Flash-Lite か 3.8 Flash を使うよう Google が案内しています。

## 5. レート制限（有料版）

有料版の上限は、使った金額と期間に応じて Tier 1 から Tier 3 まで自動で上がっていきます（2026年10月時点、公式のレート制限ページで確認）。

| Tier | 条件 | 請求の上限 |
|------|------|-----------|
| Tier 1 | 請求先アカウントを設定してつなぐ | $250 |
| Tier 2 | 合計 $100 を支払い、最初の支払いから3日経つ | $2,000 |
| Tier 3 | 合計 $1,000 を支払い、最初の支払いから30日経つ | $20,000〜$100,000 以上 |

モデルごとの細かい上限は、AI Studio の [レート制限のページ](https://aistudio.google.com/rate-limit) で確認できます。

### 無料版のキーも設定している場合

SAIVerse では、「(無料枠)」のモデルは無料版のキー（`GEMINI_FREE_API_KEY`）を優先して使い、無料枠の上限に達したときやタイムアウトしたときは、有料版のキーに自動で切り替えて送り直します。このとき料金が発生します。

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
GEMINI_API_KEY=ここに取得したキーを貼り付け
```

## 参考リンク

- [Google AI Studio](https://aistudio.google.com/)
- [Gemini API のモデル一覧](https://ai.google.dev/gemini-api/docs/models)
- [Gemini API の料金](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API の請求ガイド](https://ai.google.dev/gemini-api/docs/billing)
- [Gemini API のレート制限](https://ai.google.dev/gemini-api/docs/rate-limits)
