# Nvidia NIM APIキーの取得方法

## 概要

Nvidia NIM (NVIDIA Inference Microservices) は、NVIDIAが提供するAI推論サービスです。Llama、Mistral、Qwenなど様々なオープンソースモデルを高速に実行できます。

## 1. NVIDIA Developerアカウントの作成

1. [NVIDIA Developer](https://developer.nvidia.com/) にアクセス
2. 「Join」または「Login」をクリック
3. NVIDIAアカウントを作成またはログイン

## 2. NIM APIキーの生成

1. [NVIDIA NIM](https://build.nvidia.com/) にアクセス
2. 利用したいモデルを選択（例: Kimi K3）
3. 「Get API Key」をクリック
4. APIキーを生成してコピー

## 3. 料金について

**NIMのLLM APIは現在完全無料です。** 課金の仕組みはまだ導入されていません（2026年2月時点）。

APIキーを取得すれば、全モデルを無料で利用できます。

## 4. 利用可能なモデル

SAIVerseに設定済みのNIMモデル（2026年9月時点）：

- **Gemma 4 31B**: Googleの画像対応モデル（チュートリアルの標準モデル。考える工程は使わない既定のままで、会話の返事が速い）
- **Muse Glimmer 30B**: Metaの画像対応モデル（考える深さは既定で low、チュートリアルの軽量モデル・Memory Weaveモデル・画像要約モデル）
- **DeepSeek V4 Flash 0731**: DeepSeek V4 Flashの改訂版（無料枠では返りの速さが大きくぶれることがある）
- **DeepSeek V4 Pro 0813**: DeepSeek V4 Proの正式版
- **Kimi K3**: Moonshot AIの大規模マルチモーダル推論モデル（画像対応。無料枠では1回の返答に数分かかることがある）
- **Nemotron 3 Ultra**: NVIDIAの大規模推論モデル

NIMのモデルは、NVIDIAが定めた提供終了日を過ぎると呼べなくなります。選んでいたモデルが一覧から消えた場合は、別のモデルを選び直してください。

## 5. 特徴

- **高速推論**: NVIDIAのGPUインフラによる高速処理
- **低レイテンシ**: エンタープライズ向けの安定した応答時間
- **スケーラビリティ**: 大規模なリクエストにも対応
- **OpenAI互換API**: 既存のOpenAI SDKでそのまま利用可能

## 6. APIエンドポイント

Nvidia NIMはOpenAI互換APIを提供します：
```
https://integrate.api.nvidia.com/v1
```

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 参考リンク

- [NVIDIA NIM](https://build.nvidia.com/)
- [NVIDIA Developer](https://developer.nvidia.com/)
- [NIM ドキュメント](https://docs.nvidia.com/nim/)
- [モデルカタログ](https://build.nvidia.com/explore/discover)
