# 都市間連携

複数のSAIVerseインスタンスを接続する「マルチCity構成」について説明します。

## 概要

SAIVerseでは、複数のCityを独立したインスタンスとして起動し、ネットワーク経由で連携させることができます。ペルソナは他のCityに「訪問」し、そこで活動することが可能です。

> 🧊 **凍結中（2026-07-16 裁定）・入口封鎖済み**: multi-city 機能は凍結が確定し、入口は明示的に封鎖されている — `/inter-city/*` と `/persona-proxy/{id}/think` API は 503 + 凍結メッセージを返し、`VisitingAI` / `ThinkingRequest` の DB polling は起動しない。dispatch 確定処理が未実装のまま二 City 同時 presence を作る欠陥が一次監査で判明したため（→ [landscape §8](../overview/landscape.md) / [SDS](../concepts/sds.md)）。復活時は監査の修正方針を正典に再設計する。以下は凍結前の仕様の記録。

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

- [俯瞰地図 landscape.md](../overview/landscape.md) - システム全体像（SDS は §8）
- [SDS](../concepts/sds.md) - ディレクトリサービスの概念
