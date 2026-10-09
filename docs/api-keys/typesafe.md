# TypeSafe APIキーの取得方法

## 概要

TypeSafe は「System One モデル」と呼ばれる判断専用 AI (Jev) を提供するサービスです。文章を生成する普通の LLM とは違い、選択・採点・真偽のような小さな判断だけを高速・低価格で返します。

SAIVerse では**反射判断**（ペルソナが会話の裏で行う小さな判断の層）にモデル「Jev (TypeSafe)」を割り当てたときだけ、このキーが読まれます。会話モデルとしては使えません。

## 1. アカウントの作成

1. [TypeSafe Console](https://console.typesafe.ai/) にアクセス
2. サインインしてアカウントを作成

> **注意**: Jev は現在アーリーアクセス（先行提供）段階です。提供条件は変わることがあるため、最新の状況は公式サイトで確認してください。

## 2. APIキーの生成

1. ログイン後、[Keys ページ](https://console.typesafe.ai/keys) に移動
2. APIキーを作成してコピー

## 3. 料金について

- Jev の単価は入力 $0.042/1M トークン（2026年9月時点の参考値）
- 判断1回あたりの入出力はごく小さいため、通常の会話モデルよりも大幅に安く動きます
- 最新の料金は [公式ドキュメント](https://docs.typesafe.ai/) で確認してください

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
TYPESAFE_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 参考リンク

- [TypeSafe](https://typesafe.ai/)
- [TypeSafe Console](https://console.typesafe.ai/)
- [ドキュメント](https://docs.typesafe.ai/)
