# SDS（SAIVerse Directory Service）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §8](../overview/landscape.md) を参照。**現状は冬眠中**。

## 一言で

複数の [City](building-city.md) プロセスを発見・追跡するインメモリ・レジストリ。

## 役割

inter-city travel（都市間のペルソナ移動）の前提機構として作られた。各 City が自分の存在を登録し、他 City が一覧を取得することで、複数の SAIVerse インスタンスが互いを見つけられるようにする。

## 仕組み

- `sds_server.py`（port 8080）
- 各 City が起動時に `/register`、`/heartbeat` で生存通知
- 他 City は `/cities` で一覧を取得

### inter-city travel との関係

都市間移動は**直接 API コールではなく DB 仲介**で行われる:

1. 出発都市が `VisitingAI` レコードを status='requested' で書く
2. 到着都市が DB をポーリングして要求を見つけ、RemotePersonaProxy を作成し status='accepted'/'rejected' に更新
3. 出発都市がポーリングして受理を確認、ペルソナの `IS_DISPATCHED=True` をセット
4. Proxy が思考要求を home 都市の API（`/persona-proxy/{id}/think`）へ転送

## 現状（冬眠中）

**デフォルト無効**（`--sds-url` 明示 + 別プロセス手動起動が必要）で、単一 City 運用に止まっているため**実質冬眠中**。将来 multi-city を復活させる際に再起動する想定。

## 実装

- サーバー: `sds_server.py`（port 8080）
- 移動仲介: `VisitingAI` / `ThinkingRequest` テーブル、`saiverse/remote_persona_proxy.py`
- 起動: `python sds_server.py` + `python main.py city_a --sds-url http://127.0.0.1:8080`

## 関連概念

- [Building / City](building-city.md) — SDS が発見・追跡する対象
- [Persona](persona.md) — inter-city travel で移動する主体

## 参照

- 地図: [`landscape.md`](../overview/landscape.md) §8
