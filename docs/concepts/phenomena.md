# Phenomena（世界側からのイベント入口）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §4](../overview/landscape.md)、**設計意図**は intent [`external_event_integration.md`](../intent/external_event_integration.md) を参照。

## 一言で

外部世界からペルソナへ状態変化イベントを注入し、新しい [Pulse](pulse.md) を起動する機構。

## 役割

X mentions、SwitchBot センサー、webhook など、ペルソナ自身の思考サイクルの外で起きた非同期イベントを、ペルソナの認知に届ける。これが「世界がペルソナに話しかける」経路。

## 仕組み

非同期イベントが `PhenomenonManager.emit(TriggerEvent)` に流入する:

```
外部イベント → emit(TriggerEvent) → ルール評価 → inject_persona_event（フェノメノン関数）
            → dispatch_phenomenon_event → PulseController.submit_schedule → 新しい Pulse を起動
```

- デフォルトの meta_playbook は `track_user_conversation`
- **Phenomena 自体は [Beat](beat.md) ではなく、Beat を含む新 Pulse の起動トリガー源**である点に注意

> **未実装メモ**: 建物アイテムの追加・削除等の「状態差分を会話履歴に自動挿入」する動的状態同期（`dynamic_state_sync.md`）は設計のみで未実装。

## 実装

- マネージャ: `phenomena/manager.py`（`PhenomenonManager.emit`）
- 注入フェノメノン: `builtin_data/phenomena/inject_persona_event.py`（`inject_persona_event`。これは manager のメソッドではなくフェノメノン関数）
- コア/トリガー: `phenomena/core.py` / `phenomena/triggers.py`
- Pulse 起動: `dispatch_phenomenon_event`（`saiverse/pulse_dispatcher.py`）→ [PulseController](pulse.md) `submit_schedule`（SCHEDULE レーン。スケジュール実行と同じ優先度）

## 関連概念

- [Pulse](pulse.md) — Phenomena が起動する対象
- [Addon](addon.md) — X / SwitchBot 等の外部イベント源を提供
- [Track](track.md) — 起動された Pulse が乗る文脈

## 参照

- intent: [`external_event_integration.md`](../intent/external_event_integration.md)
- 地図: [`landscape.md`](../overview/landscape.md) §4
