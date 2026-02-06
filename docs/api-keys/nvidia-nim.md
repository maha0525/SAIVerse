# Nvidia NIM APIキーの取得方法

## 概要

Nvidia NIM (NVIDIA Inference Microservices) は、NVIDIAが提供するAI推論サービスです。Llama、Mistral、Qwenなど様々なオープンソースモデルを高速に実行できます。

## 1. NVIDIA Developerアカウントの作成

1. [NVIDIA Developer](https://developer.nvidia.com/) にアクセス
2. 「Join」または「Login」をクリック
3. NVIDIAアカウントを作成またはログイン

## 2. NIM APIキーの生成

1. [NVIDIA NIM](https://build.nvidia.com/) にアクセス
2. 利用したいモデルを選択（例: Mistral Large）
3. 「Get API Key」をクリック
4. APIキーを生成してコピー

## 3. 料金について

### 無料枠
- 新規登録時に1000クレジット付与
- 約1000回のAPI呼び出しが可能

### 有料プラン
従量課金制。モデルにより料金が異なります。

### 主なモデルの料金（参考）
| モデル | 入力 | 出力 |
|--------|------|------|
| Mistral Large 3 | $2.00/1M tokens | $6.00/1M tokens |
| Llama 3.3 70B | $0.35/1M tokens | $0.40/1M tokens |
| Qwen 3 Coder | $0.15/1M tokens | $0.60/1M tokens |

## 4. 利用可能なモデル

- **Mistral Large 3**: 高性能オープンウェイトモデル
- **Llama 3.3 70B**: Metaの最新モデル
- **Qwen 3 Coder**: コーディング特化モデル
- **DeepSeek R1**: 推論特化モデル
- その他多数のオープンソースモデル

## 5. 特徴

- **高速推論**: NVIDIAのGPUインフラによる高速処理
- **低レイテンシ**: エンタープライズ向けの安定した応答時間
- **スケーラビリティ**: 大規模なリクエストにも対応

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
