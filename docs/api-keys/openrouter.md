# OpenRouter APIキーの取得方法

## 概要

OpenRouter は、いろいろな会社（OpenAI、Google、DeepSeek、Qwen、Z.ai、Moonshot AI など）のモデルを、1つのAPIキーでまとめて使えるようにする中継サービスです。SAIVerse では、OpenRouter のキーを1つ設定すれば、下の「SAIVerse設定済みモデル」に挙げたモデルをすべて選べるようになります。

## 1. OpenRouterアカウントの作成

1. [OpenRouter](https://openrouter.ai/) にアクセスします。
2. 右上の「Sign In」をクリックします。
3. Google、GitHub、またはメールアドレスでアカウントを作成します。

## 2. APIキーの生成

1. ログイン後、[Keys ページ](https://openrouter.ai/settings/keys) を開きます。
2. 「Create Key」をクリックします。
3. キーに名前を付けて作成します。
4. 表示されたAPIキーをコピーして、安全な場所に控えます。

## 3. クレジットの追加

有料モデルを使うには、先にクレジット（前払いの残高、単位は米ドル）を入れておく必要があります。

1. [Credits ページ](https://openrouter.ai/settings/credits) を開きます。
2. 入れたい金額を選びます。
3. クレジットカード、AliPay、または暗号資産（USDC）で支払います。

購入時には手数料がかかります。カードなどでの購入は 5.5%（最低 $0.80）、暗号資産での購入は 5% です（2026年10月時点、[公式のよくある質問](https://openrouter.ai/docs/faq) で確認）。

知っておくと安心な決まりも書いておきます。

- 使わなかったクレジットの返金は、購入から24時間以内に Credits ページの返金ボタンから申し込めます。24時間を過ぎると返金できません。手数料と、暗号資産での支払いは返金されません。
- 規約上、購入から1年たった未使用のクレジットは失効することがあります。

## 4. 料金について

OpenRouter はモデルの利用料金に上乗せをしません。各モデルを実際に動かしている提供元（クラウド会社など）の料金がそのまま使った分だけ差し引かれ、OpenRouter の取り分は上の購入手数料だけです。

同じモデルを複数の提供元が動かしていることが多く、提供元ごとに値段が違います。OpenRouter は特に指定がなければ、安い提供元を優先しつつ、止まっていない提供元に振り分けます。そのため、実際に払う単価はリクエストごとに少し変わることがあります。

### SAIVerse設定済みモデルの料金（目安）

2026年10月9日時点、OpenRouter の公式モデル一覧（[Models](https://openrouter.ai/models) と、その元になっている公開API）に表示されていた値です。単位は100万トークンあたりの米ドルです。

| モデル | 入力 | 出力 |
|--------|------|------|
| DeepSeek V3.2 | $0.259 | $0.42 |
| DeepSeek V4 Flash | $0.0057 | $1.28 |
| DeepSeek V4 Flash Latest | $0.011 | $0.6101 |
| DeepSeek V4 Pro | $0.2923 | $0.5846 |
| DeepSeek V4 Pro 0813 | $0.66 | $1.98 |
| GPT-4o 2024-11-20 | $2.5 | $10 |
| GPT-OSS 120B | $0.037 | $0.17 |
| GPT-OSS 20B | $0.018 | $0.09 |
| Kimi K2.5 | $0.45 | $2.25 |
| Kimi K2.7 Code | $0.6712 | $3.35 |
| Muse Spark 1.2 | $1.25 | $4.25 |
| MiniMax M2.1 | $0.3 | $1.2 |
| MiniMax M2.5 | $0.27 | $1.08 |
| MiniMax M3 | $0.3 | $1.2 |
| Mistral Medium 3.5 | $1.5 | $7.5 |
| Nemotron 3 Ultra（有料版） | $0.5 | $2.2 |
| Qwen3 Next 80B A3B | $0.09 | $1.1 |
| Qwen3.5 27B | $0.26 | $2.6 |
| Qwen3.5 35B A3B | $0.15 | $1 |
| Qwen3.5 122B A10B | $0.26 | $2.08 |
| Qwen3.5 397B A17B | $0.45 | $3 |
| Qwen3.5 Plus 2026-02-15 | $0.26 | $1.56 |
| Qwen3.6 27B | $0.3 | $2 |
| Qwen3.6 35B A3B | $0.15 | $1 |
| Qwen3.6 Flash | $0.1875 | $1.125 |
| Qwen3.7 Plus | $0.32 | $1.28 |
| Qwen3.7 Max | $1.475 | $4.425 |
| Qwen3.8 Max 0902 | $2 | $6 |
| Step 3.7 Flash | $0.2 | $1.15 |
| Z.ai GLM-4.6V | $0.3 | $0.9 |
| Z.ai GLM-4.7 Flash | $0.0605 | $0.4 |
| Z.ai GLM-4.7 | $0.6 | $2.2 |
| Z.ai GLM-5 | $0.6 | $1.92 |
| Z.ai GLM-5.1 | $0.966 | $3.036 |

表の値は、一覧に代表として載っている1つの提供元の値段です。提供元による差が大きいモデルもあります。たとえば DeepSeek V4 Flash は、同じ日の提供元ごとの値段が入力 $0.0046〜$0.44、出力 $0.17〜$1.32 と大きく開いていました。正確な値段は、各モデルのページの「Providers」の欄で確認してください。

> **注意**: 値段は数週間単位で変わります。最新の料金は [OpenRouter Models](https://openrouter.ai/models) で確認してください。

### 無料モデル

モデルIDの末尾に `:free` が付いているものは、無料で使える版です。無料版には次の回数制限があります（2026年10月時点、[公式の制限のページ](https://openrouter.ai/docs/api/reference/limits) で確認）。

| これまでに買ったクレジットの合計 | 1分あたり | 1日あたり |
|------|------|------|
| $10 未満（一度も買っていない場合も含む） | 20回 | 50回 |
| $10 以上 | 20回 | 1000回 |

- 1日の回数は、すべての無料モデルを合わせた回数です。キーやアカウントを増やしても回数は増えません。
- SAIVerse のペルソナは、1回の返事の中で複数回モデルを呼ぶことがあります。クレジットを一度も買っていない場合の「1日50回」は、すぐに使い切る可能性があります。
- 無料版は、混み具合や提供されている期間が有料版と違うことがあります。
- アカウントの [プライバシー設定](https://openrouter.ai/settings/privacy) では、「入力をモデルの学習に使う可能性がある提供元」に振り分けてよいかを、有料モデルと無料モデルで別々に決められます。許可していない場合、そうした提供元しか動かしていないモデルは呼べません。無料モデルが呼べないときは、この設定も確認してください。

SAIVerse に設定済みの無料モデル（2026年10月9日時点）：

- **Nemotron 3 Ultra（無料）**: 一覧に載っていて、提供元が1つあります。チュートリアルの無料設定の標準モデルです。決まった形の答えを返す機能（構造化出力）には対応していません。
- **Free Models Router（無料モデルを自動選択）** (`openrouter/free`): 決まったモデルではなく、OpenRouter がそのときの無料モデルの中から一つを選んで送る仕組みです（[公式の説明](https://openrouter.ai/docs/guides/routing/routers/free-router)）。SAIVerse の設定では、リクエストに必要な機能（決まった形の答え・画像・入力の長さ）を満たすモデルだけから選ばれるようにしてあります。無料モデルが入れ替わっても選び直す必要がないので、チュートリアルの無料設定では軽量・Memory Weave・画像要約に使っています。毎回ちがうモデルが答えるため、答えの書きぶりは呼ぶたびに変わります（小さいモデルが選ばれると、日本語で頼んでも英語で返ることがあります）。ペルソナの口調を一定にしたい会話には向きません。

以前設定していた Nex N2.5 Pro（無料）は、2026年10月9日の時点で OpenRouter から無料版が消え（呼べる提供元が0件）、SAIVerse の組み込みから外しました。このモデルを選んでいた場合は、別のモデルを選び直してください。

無料版は提供元の都合で終了することがあります。選んでいた無料モデルが一覧から消えた場合は、別のモデルを選び直してください。

## 5. 利点

- **1つのキーで多くのモデル**: いろいろな会社のモデルを、1つのAPIキーとまとめた残高で使えます。
- **提供元の自動切り替え**: あるモデルの提供元が混雑していたり止まっていたりすると、同じモデルを動かしている別の提供元に自動で回してくれます。
- **使った量の確認**: [Activity ページ](https://openrouter.ai/activity) で、いつどのモデルにいくら使ったかを確認できます。
- **無料モデル**: 一部のモデルは回数制限つきで無料で試せます。

## 6. SAIVerse設定済みモデル

（2026年10月9日時点の SAIVerse 同梱設定です。「画像対応」は、SAIVerse の設定で画像を渡せるようにしてあるモデルです。）

**DeepSeek**
- DeepSeek V3.2
- DeepSeek V4 Flash（OpenRouter では「DeepSeek V4 Flash 0423」という名前で載っています）
- DeepSeek V4 Flash Latest（DeepSeek V4 Flash の一番新しい版へ自動で転送される名前です）
- DeepSeek V4 Pro（OpenRouter では「DeepSeek V4 Pro 0423」という名前で載っています）/ DeepSeek V4 Pro 0813

**Qwen**
- Qwen3 Next 80B A3B
- Qwen3.5 27B / 35B A3B / 122B A10B / 397B A17B（それぞれ「考える版」と「考えない版」の2つの設定があります。画像対応）
- Qwen3.5 Plus 2026-02-15（画像対応）
- Qwen3.6 27B / 35B A3B / Flash（画像対応）
- Qwen3.7 Plus（画像対応）/ Qwen3.7 Max
- Qwen3.8 Max 0902（画像対応）

**Z.ai**
- GLM-4.6V（画像対応）
- GLM-4.7 Flash / GLM-4.7（GLM-4.7 は OpenRouter の一覧で 2026年12月31日に提供終了予定と表示されています）
- GLM-5 / GLM-5.1

**Moonshot AI**
- Kimi K2.5（画像対応）/ Kimi K2.7 Code（画像対応）

**MiniMax**
- MiniMax M2.1 / MiniMax M2.5 / MiniMax M3（M3 は画像対応）

**その他**
- GPT-4o 2024-11-20（画像対応）/ GPT-OSS 120B / GPT-OSS 20B
- Muse Spark 1.2（Meta、画像対応）
- Mistral Medium 3.5（画像対応）
- Step 3.7 Flash（StepFun、画像対応）
- Nemotron 3 Ultra（NVIDIA、有料版）

**無料モデル**
- Nemotron 3 Ultra（無料）（チュートリアルの無料設定の標準モデル）
- Free Models Router（無料モデルを自動選択）（チュートリアルの無料設定の軽量・Memory Weave・画像要約）

**判断専用モデル**
- Jev（TypeSafe）: 会話には使わないモデルです。状況を渡すと、「はい・いいえ」の確率、選択肢、点数といった決まった形の判断を素早く返します。SAIVerse では反射判断の役割に割り当てて使います。OpenRouter の公開APIに載っている Jev 1.13 の値段は、入力100万トークンあたり $0.042、出力は無料です（2026年10月9日時点）。

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
  "api_key_env": "OPENROUTER_API_KEY"
}
```

**2.** SAIVerse を再起動する。稼働中のプロセスは起動時に読み込んだ設定で動き続けるため、**ファイルを置いただけでは申告は止まりません**。

`.env` の API キーはそのままで構いません。接続情報を書き写す必要があるのは、ユーザー設定が同梱設定とのマージではなく**丸ごと差し替え**になるためです。アプリ名のヘッダーだけを書かないことで、申告が止まります。

この編集は今のところファイルを置く形でしか行えません。グローバル設定 > モデル管理 > プロバイダの編集画面にはアプリ名ヘッダーの項目がなく、そこから保存すると同梱のヘッダーがそのまま引き継がれます。

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 参考リンク

- [OpenRouter](https://openrouter.ai/)
- [モデル一覧](https://openrouter.ai/models)
- [無料モデルの一覧](https://openrouter.ai/models?max_price=0)
- [ドキュメント](https://openrouter.ai/docs/quickstart)
- [よくある質問（料金・手数料・返金）](https://openrouter.ai/docs/faq)
- [回数制限とクレジットの制限](https://openrouter.ai/docs/api/reference/limits)
