# Google Gemini（無料版）APIキーの取得方法

## 1. Googleアカウントでログイン

1. [Google AI Studio](https://aistudio.google.com/) にアクセスします。
2. Googleアカウントでログインし、利用規約に同意します。

初めて使う人には、ログインした時点でプロジェクトと API キーが1つずつ自動で作られます。

## 2. APIキーの取得

1. AI Studio の [API キーのページ](https://aistudio.google.com/api-keys) を開きます。
2. 自動で作られたキーがあれば、それをコピーします。
3. 新しく作りたいときは「Create API key」を押し、表示に従ってプロジェクトとキーを作ります。
4. 表示された API キーをコピーします。

## 3. 無料枠について

無料枠では、下の「SAIVerse で使える無料枠のモデル」に挙げたモデルを、料金を払わずに使えます。

### レート制限

無料枠には、1分あたりのリクエスト数、1分あたりのトークン数、1日あたりのリクエスト数の上限があります。上限の値はモデルごとに違い、Google のアカウントの状態によっても変わります。公式ドキュメントには固定の数値が載っておらず、自分の上限は AI Studio の [レート制限のページ](https://aistudio.google.com/rate-limit) で確認する仕組みになっています（2026年10月時点、公式のレート制限ページで確認）。

- 上限はプロジェクトごとに数えられます。同じプロジェクトでキーを増やしても、上限は増えません。
- 1日あたりの回数は、太平洋時間の深夜0時（日本時間の午後4時か午後5時）にリセットされます。

SAIVerse のペルソナは自律的に動くので、無料枠の上限にはすぐ届きます。試しに使ってみる用途に向いています。

### 注意事項
- 無料枠で送った内容と返ってきた内容は、Google の製品改善に使われます（有料版では使われません）。
- 無料枠では使えないモデルがあります（Gemini 3.1 Pro Preview など）。
- 画像生成（`generate_image` ツールの Nano Banana 系）は、SAIVerse では有料版のキー（`GEMINI_API_KEY`）が必要です。

## 4. SAIVerse で使える無料枠のモデル

SAIVerse のモデル選択では、名前に「(無料枠)」が付いているものが無料版のキーで動きます。

- **Gemini 3.8 Flash(無料枠)**: Google が「いちばん賢い Flash モデル」と位置づけている最新のモデルです。迷ったらこれを選んでください。
- **Gemini 3.7 Flash(無料枠)** / **Gemini 3.6 Flash(無料枠)**: ひとつ前の世代の Flash モデルです。
- **Gemini 3.5 Flash(無料枠)**: さらに前の世代の Flash モデルです。
- **Gemini 3.5 Flash-Lite(無料枠)** / **Gemini 3.1 Flash-Lite(無料枠)**: 速くて軽いモデルです。
- **Gemini 3 Flash(無料枠)**: 旧世代のプレビュー版モデルです。
- **Gemini 2.5 Flash(無料枠)** / **Gemini 2.5 Flash Lite(無料枠)**: 旧世代のモデルです。2026年9月から、Google は Gemini 2.5 系を「過去に使っていた人」に限って提供しています。これから始める人は、3.5 Flash-Lite か 3.8 Flash を使うよう Google が案内しています。

## 5. 有料版との違い

有料版では次のことができます（2026年10月時点、公式の料金ページで確認）。

- レート制限が高くなります。
- Gemini 3.1 Pro Preview など、無料枠では使えないモデルが使えます。
- 送った内容が Google の製品改善に使われません。
- コンテキストキャッシュ（同じ長い入力を繰り返し送るときの割引）が使えます。

### 無料版と有料版のキーを両方設定した場合

SAIVerse では、「(無料枠)」のモデルは無料版のキーを優先して使います。ただし有料版のキー（`GEMINI_API_KEY`）も設定してあると、無料枠の上限に達したときやタイムアウトしたときに、自動で有料版のキーに切り替えて送り直します。このとき料金が発生します。料金を一切払いたくない場合は、無料版のキーだけを設定してください。

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
GEMINI_FREE_API_KEY=ここに取得したキーを貼り付け
```

## 参考リンク

- [Google AI Studio](https://aistudio.google.com/)
- [Gemini API のモデル一覧](https://ai.google.dev/gemini-api/docs/models)
- [Gemini API の料金](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini API のレート制限](https://ai.google.dev/gemini-api/docs/rate-limits)
