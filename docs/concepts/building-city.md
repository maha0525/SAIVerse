# Building / City

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §2](../overview/landscape.md) を参照。旧版は [`legacy/city-building.md`](legacy/city-building.md)。

## 一言で

会話・活動が生じる場（= ユーザーから見えるチャットUI）が **Building**、複数の Building を束ねる運行インスタンス（一つの「世界」）が **City**。

## 役割

### Building = 共有メッセージ場

Building は**ユーザーから見えるチャットUI そのもの**であり、[Persona](persona.md) の発言（[Beat](beat.md) の表示用）もユーザーの発言も、すべてここに積まれることで**そこに居る他者（他ペルソナ・ユーザー）に感知される**——いわば**複数主体の共有メッセージ場（共有黒板）**。

各 occupant は Building の未読メッセージを自分の [Session](session.md)（短期記憶）に読み込む。これにより:

> **Building（公共の場） ⇄ Session（各自の私的な短期記憶）** が対をなす。

### City = 一つの世界

City は User が運営する一つの「世界」。複数の Building を束ね、UI / API を公開するポートを持つ。

## 仕組み

### Building の属性（`Building` テーブル）

- 所属 City
- `CAPACITY`（収容数上限。[OccupancyManager](../overview/landscape.md) が enforce）
- `SYSTEM_INSTRUCTION`（システムプロンプト）
- `AUTO_INTERVAL_SEC`（自動 pulse 間隔）
- `PHYSICAL_VESSEL_ID`（物理デバイスに紐付く Vessel Building の場合 → [Item](item.md)）

### City の属性（`City` テーブル）

- `UI_PORT` / `API_PORT`（UI / API 公開ポート）
- オンラインモードフラグ
- `LAST_KNOWN_VERSION`（バージョン認識機構。アップデート時の状態移行を追跡）

### 入退室の管理

エンティティ（ユーザー・ペルソナ・訪問者）の移動は **OccupancyManager**（`saiverse/occupancy_manager.py`）が一元管理し、`BuildingOccupancyLog` に記録する。ペルソナの移動は `OccupancyManager.move_to(persona, building_id)` を使う（PersonaCore のメソッドを直接呼ばない）。

## 実装

- DB: `Building` / `City` / `BuildingOccupancyLog` テーブル（`database/models.py`）
- 移動制御: `saiverse/occupancy_manager.py`（`OccupancyManager`）
- 建物履歴ログ: `~/.saiverse/cities/<city>/buildings/<building>/log.json`

## 関連概念

- [Persona](persona.md) — Building に住む主体
- [Session](session.md) — Building の未読を取り込む各自の短期記憶
- [Beat](beat.md) — Building に積まれる発言
- [Item](item.md) — Building に置かれる物 / Fixture / Vessel

## 参照

- 地図: [`landscape.md`](../overview/landscape.md) §2
- DB 設計: [`reference/database-schema.md`](../reference/database-schema.md)
