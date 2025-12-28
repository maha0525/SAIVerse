# 都市間連携

複数のSAIVerseインスタンスを接続する「マルチCity構成」について説明します。

## 概要

SAIVerseでは、複数のCityを独立したインスタンスとして起動し、ネットワーク経由で連携させることができます。ペルソナは他のCityに「訪問」し、そこで活動することが可能です。

## アーキテクチャ

```
┌─────────────┐         ┌─────────────┐
│   City A    │◄───────►│   City B    │
│  main.py    │         │  main.py    │
│  port:8000  │         │  port:9000  │
└──────┬──────┘         └──────┬──────┘
       │                       │
       └───────────┬───────────┘
                   │
           ┌───────▼───────┐
           │      SDS      │
           │ Directory Svc │
           │  port:8080    │
           └───────────────┘
```

### コンポーネント

| コンポーネント | 役割 |
|----------------|------|
| SDS (sds_server.py) | ディレクトリサービス。City一覧を管理 |
| City API | 各Cityの外部公開エンドポイント |
| RemotePersonaProxy | 訪問者AIの軽量代理人 |

## SDSの起動

```bash
python sds_server.py
# デフォルト: http://127.0.0.1:8080
```

## マルチCity起動

```bash
# ターミナル1
python sds_server.py

# ターミナル2
python main.py city_a --sds-url http://127.0.0.1:8080

# ターミナル3
python main.py city_b --sds-url http://127.0.0.1:8080
```

## 訪問フロー

### ペルソナの派遣

1. City AのAIがCity Bへの移動を決定
2. City Aが `VisitingAI` テーブルにレコード作成（status: `requested`）
3. City Bがレコードを検出し、受け入れ処理
4. `RemotePersonaProxy` がCity Bに配置
5. City Aの元ペルソナは `IS_DISPATCHED=True` で待機

### 思考の委譲

訪問先の `RemotePersonaProxy` は自身では思考しません：

1. 訪問先で発話が必要になる
2. Proxy が故郷City AのAPI `/persona-proxy/{id}/think` を呼び出し
3. City AのPersonaCoreが実際に思考・応答を生成
4. 結果をProxyに返却、訪問先で発話

### 帰還

1. 訪問終了時、`VisitingAI.status` を更新
2. City Aが検出し、ペルソナをローカルに復帰
3. 記憶差分を同期

## ハートビート

各Cityは30秒ごとにSDSにハートビートを送信。

- アクティブなCityのみがリストに表示
- 一定時間応答がないCityは自動削除

## オンライン/オフラインモード

UIから切り替え可能：

- **オンライン**: SDS連携有効、他Cityと通信
- **オフライン**: ローカルのみで動作

## 次のステップ

- [アーキテクチャ](../concepts/architecture.md) - システム全体像
