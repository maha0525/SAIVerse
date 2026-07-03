# Item / 拡張中の存在論（Fixture / Observer / Vessel）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §2](../overview/landscape.md)、**設計意図**は intent [`observer.md`](../intent/observer.md) / [`stackchan_vessel.md`](../intent/stackchan_vessel.md) を参照。

## 一言で

持ち運べる物が **Item**。それに加えて、持ち運べない設置物 **Fixture**、定期観測する **Observer**、ペルソナの物理身体 **Vessel** という存在論が拡張中。

## Item（持ち運べる物）

`Item` テーブルで定義される。「どこに在るか」は **`ItemLocation` テーブルの多態**で管理され、`OWNER_KIND` が `building` / `persona` / `world` / `bag` のいずれかを取る。

- 同じ Item が異なる所有者に紐付くことで、建物に置かれているのか・ペルソナが手に持っているのかを表現する
- `pickup` / `place` 操作で配置が更新される（[Tool](tool.md) の `item_*`、`saiverse/action_handler.py`）

## 拡張中の存在論

世界モデルは Persona / Item に加えて拡張が進行中（進捗は [`roadmap_status.md`](../overview/roadmap_status.md) §5）:

### Fixture（第三の存在論）

持ち運べない固定設置物（リンゴの木・センサー・掲示板）。[Building](building-city.md) 直結で `pickup` 不可。**`observer.md` v0.1、設計のみ・未実装**（テーブル未実装）。

### Observer

定期実行能力を持つ Fixture。EventScheduler に相乗りして定期観測 → 時系列蓄積（`observer_metrics`）→ 閾値/変化で通知（SGP30 等のステートフルセンサー）。**観測・通知だけ行い、判断はペルソナ側（[Pulse](pulse.md)）の仕事**。

### Vessel（物理身体）

ペルソナを物理デバイス（Stack-chan）の身体に「降ろす」機構。**Vessel Building にペルソナが居る間、その物理 I/O が身体感覚になる**（マイク=耳 / スピーカー=口 / カメラ=目 / タッチ=触覚）。

- 本体フック `Building.PHYSICAL_VESSEL_ID`（実装済み） + アドオン実装（`stackchan_vessel.md` v0.8）
- 本体の汎用 Vessel システムへの昇格が構想中

## 実装

- DB: `Item` / `ItemLocation` テーブル（`database/models.py`）
- 操作: `saiverse/action_handler.py`（`pickup` / `place` / `use_item`）、`builtin_data/tools/item_*.py`
- Vessel フック: `Building.PHYSICAL_VESSEL_ID`

## 関連概念

- [Building / City](building-city.md) — Item / Fixture が置かれる場
- [Persona](persona.md) — Item を持つ主体 / Vessel が降ろす対象
- [Phenomena](phenomena.md) — Observer / センサーのイベント入口

## 参照

- intent: [`observer.md`](../intent/observer.md) / [`stackchan_vessel.md`](../intent/stackchan_vessel.md)
- 地図: [`landscape.md`](../overview/landscape.md) §2
