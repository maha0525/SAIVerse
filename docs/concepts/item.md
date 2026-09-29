# Item / 拡張中の存在論（Fixture / Observer / Vessel）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §2](../overview/landscape.md)、**設計意図**は intent [`observer.md`](../intent/observer.md) / [`stackchan_vessel.md`](../intent/stackchan_vessel.md) を参照。

## 一言で

持ち運べる物が **Item**。それに加えて、持ち運べない設置物 **Fixture**、定期観測する **Observer**、ペルソナの物理身体 **Vessel** という存在論が拡張中。

## Item（持ち運べる物）

`Item` テーブルで定義される。「どこに在るか」は **`ItemLocation` テーブルの多態**で管理され、`OWNER_KIND` が `building` / `persona` / `world` / `bag` のいずれかを取る。

- 同じ Item が異なる所有者に紐付くことで、建物に置かれているのか・ペルソナが手に持っているのかを表現する
- 配置の更新は [Tool](tool.md) の `item_move`（`builtin_data/tools/item_move.py`、`destination_type`: building/persona/bag）で行う。pickup/place/use の実体はマネージャの `SAIVerseManager.pickup_item_for_persona` / `use_item_for_persona`（→ `item_service`）
- 削除 (ユーザー操作・World Editor、`manager/admin.py`): `delete_item` は入れ物 (bag) を消すとき、直接の中身を入れ物があった場所 (部屋 / ペルソナの持ち物 / 外側の入れ物 / どこにも置かない) へ出してから消す。入れ子の入れ物は中身ごとそのまま出る。中身も一緒に消すときは、先に `delete_bag_contents` (入れ子まで全部消し、入れ物は残す) を呼ぶ。どちらも置き場所の行とアイテムの行だけを消し、参照しているファイルは消さない
- 建物の削除 (`AdminService.delete_building(item_policy=...)`): 建物に直接置かれたアイテムは、利用者が確認の欄で選んだとおりに扱う。`keep` (既定) は置き場所の行だけを消して「どこにも置かれていない」(`world`) 状態で残し、入れ物の中身は入れ物に付いたまま。`delete` は入れ物の中身を入れ子の底まで含めて消す (`delete_bag_contents` と同じ実装)。建物に置かれた Fixture は、ぶら下がる観測設定・観測値・フィード購読・記事・既読カーソル・配信設定ごと必ず一緒に消える (`saiverse/observer_manager.py::delete_fixture_rows`、`ObserverManager.delete_fixture` と同じ実装)。部屋の会話の記録は残る。以前の削除が残した「消えた建物を指す置き場所・設置物・建物のリアルタイムスペル」は、起動時に `saiverse/building_leftover_cleanup.py` が片付ける (アイテムは消さずに `world` へ移す)。経緯は [`building_delete_leaves_contents.md`](../issues/building_delete_leaves_contents.md)

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
- 操作: `SAIVerseManager.pickup_item_for_persona` / `use_item_for_persona`（`item_service` に委譲）、Tool は `item_move` / `item_view` / `item_annotate`（`builtin_data/tools/`）。※pre-SEA 期の `action_handler.py`（`::act` ブロック）は 2026-07-23 に撤去済み（landscape §9）
- Vessel フック: `Building.PHYSICAL_VESSEL_ID`

## 関連概念

- [Building / City](building-city.md) — Item / Fixture が置かれる場
- [Persona](persona.md) — Item を持つ主体 / Vessel が降ろす対象
- [Phenomena](phenomena.md) — Observer / センサーのイベント入口

## 参照

- intent: [`observer.md`](../intent/observer.md) / [`stackchan_vessel.md`](../intent/stackchan_vessel.md)
- 地図: [`landscape.md`](../overview/landscape.md) §2
