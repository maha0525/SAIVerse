# Persona

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §2](../overview/landscape.md) を参照。旧版は [`legacy/persona.md`](legacy/persona.md)。

## 一言で

「自身が考え、選択し、行動する」AI 主体。SAIVerse の世界に住まう存在の中心。

## 役割

ペルソナは名前・アバター・システムプロンプト（人格）を持ち、いずれかの [Building](building-city.md) に所属して、[Pulse](pulse.md) による認知サイクルを回しながら自律的に生き続ける。ユーザーはペルソナと Building を介して対話する。

## 仕組み

- コード上は `PersonaCore`（`persona/core.py`）、DB では `ai` テーブル
- 文字列 ID（`AIID`）で識別。現在位置は `current_building_id`
- 自律性は **`ACTIVITY_STATE`** で外部に宣言される: **Stop / Sleep / Idle / Active** の4段階
- 使用モデルは `DEFAULT_MODEL`（通常）と `LIGHTWEIGHT_MODEL`（軽量・任意）の2系統
- 他都市へ渡っている間は `IS_DISPATCHED=True`（→ inter-city travel）

### 認知の全体像

ペルソナの「動き続ける」仕組みは複数の層に分かれる:

- 駆動: [Pulse](pulse.md) / [Track](track.md) / [Meta-Judgment](meta-judgment.md)
- 行動: [Beat](beat.md) / [Playbook](playbook.md) / [Spell](spell.md) / [Tool](tool.md)
- 長期記憶: [SAIMemory](saimemory.md) / [Chronicle](chronicle.md) / [Memopedia](memopedia.md)
- 短期記憶: [Session](session.md) / [Metabolism](metabolism.md)

認知モデルの詳細な設計は intent [`persona_cognitive_model.md`](../intent/persona_cognitive_model.md) と [`persona_cognition/`](../intent/persona_cognition/) にある。

## User との関係

**User** は SAIVerse を利用する人間（`User` テーブル、`CURRENT_CITYID` / `CURRENT_BUILDINGID` で現在地を保持）。ペルソナと並ぶ主体だが AI ではなく、**[Building](building-city.md) を介してペルソナと相互に感知し合う**。ユーザーの発言は Building 経由でペルソナの [Session](session.md)（短期記憶）に流入する外界入力の一つ。

## 実装

- コア: `persona/core.py`（`PersonaCore`、`current_building_id` 等）
- DB: `ai` テーブル（`database/models.py`）
- 生成テンプレート: `blueprint` テーブル（現状ほぼ未運用）
- 作成: フロントエンド UI、または「創造の祭壇」建物で Genesis に依頼

## 関連概念

- [Building / City](building-city.md) — ペルソナが住む場と世界
- [Pulse](pulse.md) — ペルソナの駆動
- [SAIMemory](saimemory.md) — ペルソナの長期記憶
- [Item](item.md) — ペルソナが持ち運べる物 / Vessel（物理身体）

## 参照

- intent: [`persona_cognitive_model.md`](../intent/persona_cognitive_model.md)
- 地図: [`landscape.md`](../overview/landscape.md) §2
