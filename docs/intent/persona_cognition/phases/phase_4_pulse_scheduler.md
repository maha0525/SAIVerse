# Phase 4 — Pulse 階層 + Scheduler + メタ定期判断

**親**: [../README.md](../README.md)
**ステータス**: 🟡 約 60%
**旧称**: Phase C-3 (Pulse スケジューラ / 定期実行)

---

## 目的

メインライン Pulse / サブライン Pulse の 2 階層分離 + 各 Scheduler 実装 + メタレイヤーの定期実行入口。これにより「自律稼働ペルソナが勝手に走り続ける」状態を技術的に成立させる。

---

## タスク

### Handler 拡張属性 (Phase 4-a)

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| Handler に `pulse_completion_notice` 属性 | ✅ | `track_handlers/social_track_handler.py:48`, `autonomous_track_handler.py:43` |
| Handler に `post_complete_behavior` 属性 (`wait_response` / `meta_judge`) | ✅ | 同上 |
| Handler に `default_pulse_interval` / `default_max_consecutive_pulses` / `default_subline_pulse_interval` | ✅ | `autonomous_track_handler.py:44-46` |

### Scheduler (Phase 4-b / 4-c)

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| `SubLineScheduler` クラス | ✅ | `saiverse/pulse_scheduler.py:76-127` |
| `AutonomyManager` のメタレイヤー定期 tick タイマー化 | ✅ | `saiverse/autonomy_manager.py:228-` (Phase C-2 で純粋タイマー化済み、判断ロジックは meta_judgment Playbook に委譲) |
| `MainLineScheduler` 相当の責務分割 (MainLine 起動経路 vs AutonomyManager) | 🟡 | 当初予定の MainLineScheduler 新設は再考。AutonomyManager + alert observer (MetaLayer) で大部分を担っており、純粋な「サブライン → メインライン昇格」専用ロジックが残っているかは精査が必要 |
| `MainLineScheduler` ↔ `SubLineScheduler` の連携 (TTL 接近で main 起動) | 🔲 | 上記精査後に判断 |

### メタレイヤー定期実行 (Phase 4-c)

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| `MetaLayer.on_periodic_tick(persona_id, context)` 入口 | ✅ | `saiverse/meta_layer.py:192-` |
| `on_track_alert` と `on_periodic_tick` の判断ループ共通化 | ✅ | 両者とも `meta_judgment` Playbook を起動する共通経路に統一 |
| 環境変数 `SAIVERSE_META_LAYER_INTERVAL_SECONDS` (デフォルト 3000) | 🟡 | `AutonomyManager.DEFAULT_INTERVAL_MINUTES = 50` (= 3000s)。env による上書きは未確認、要検証 |
| メタ判断 Pulse の失敗時挙動 (LLM error / parse error / Lock 解放) | 🔲 | `_run_judgment` 失敗時のリカバリ経路が未定義。`waiting` Track のタイムアウト通知失敗時のフォールバックも空欄 |

### 7 制御点の実装場所明確化

| 制御点 | 実装場所 | 状態 |
|--------|---------|------|
| (1) Track 単位の Pulse 間隔 | `action_tracks.metadata.pulse_interval_seconds` | 🟡 metadata 構造のみ、TrackManager 経由読み書き API 未整備 |
| (2) Track 単位の連続実行回数上限 | `action_tracks.metadata.max_consecutive_pulses` | 🟡 同上 |
| (3) メタレイヤー定期実行間隔 | `SAIVERSE_META_LAYER_INTERVAL_SECONDS` | 🔲 |
| (4) モデル別キャッシュ TTL 同期 | `saiverse/model_configs.py` の `cache_ttl_seconds` 追加 | 🔲 |
| (5) メインライン Pulse のトリガ条件 | MainLineScheduler のロジック | 🔲 |
| (6) サブライン Pulse のメインライン 1 呼び出しあたり最大回数 | メインライン LLM 出力 → state 経由 | 🔲 |
| (7) サブライン Pulse の間隔 | Handler の `default_subline_pulse_interval` クラス属性 | ✅ |

### 環境別デフォルト値の自動推定

| 項目 | 状態 |
|------|------|
| Pattern A/B/C のテンプレート定義 | 🔲 |
| ペルソナ作成時に DEFAULT_MODEL から自動推定 → metadata 書き込み | 🔲 |
| 手動調整 UI (Memory Settings 等) | 🔲 |

---

## 残タスクの詳細

### `MainLineScheduler` クラス

メインライン Pulse の起動を管理する background loop。

**責務**:
- **対象**: `ACTIVITY_STATE=Active` なペルソナ
- **トリガ条件**:
  - メインモデルのキャッシュ TTL 接近 (`SAIVERSE_META_LAYER_INTERVAL_SECONDS` 経過、または cache_ttl_seconds 経過の早い方)
  - 外部イベント駆動 (alert 発生時、即時)
  - サブラインから「区切り」シグナル
- **動作**: 該当ペルソナに対してメタ判断 Playbook (`meta_judgment.json`) を起動

**実装方針**:
- background thread (or asyncio task) として SAIVerseManager から起動
- 各ペルソナの最終メタレイヤー実行時刻を記録
- ACTIVITY_STATE による分岐:
  - `Active`: 定期発火 ON
  - `Idle`: 定期発火 OFF
  - `Sleep`/`Stop`: 定期発火 OFF

### `MetaLayer.on_periodic_tick`

```python
class MetaLayer:
    # 既存 (Phase 2)
    def on_track_alert(self, persona_id, track_id, context):
        ...
    
    # Phase 4 新規
    def on_periodic_tick(self, persona_id, context):
        """定期実行で呼ばれる。alert と同じ判断ループを共有する。
        
        中身は alert 入口と同じ:
        - 現在状態を見て判断 (running なし含む)
        - スペル発行で Track 操作 (新規 Track 作成・既存 pending の activate・何もしない)
        """
        ...
```

両入口は **同じ判断ループ** (`_run_judgment`) を共有する。違いは context のみ:

- alert 入口: `context = {"trigger": "user_utterance", ...}` 等
- 定期入口: `context = {"trigger": "periodic_tick", "interval_seconds": ...}`

メタレイヤーのプロンプトは両ケースで「現状を見て判断する」共通形式。専用の判断ロジックを増やさない。

### `AutonomyManager` の現状と今後の方針

`saiverse/autonomy_manager.py` は Phase C-2 で **純粋なメタレイヤー定期 tick タイマー** に再構成済み (廃止ではなく再利用)。判断ロジックは `meta_judgment` Playbook に委譲済みで、AutonomyManager 自体は `MetaLayer.on_periodic_tick` を一定間隔で呼ぶだけ。

```
[現状]
AutonomyManager.start() → background loop
  → MetaLayer.on_periodic_tick(persona_id) を DEFAULT_INTERVAL_MINUTES (=50) ごとに発火
  → MetaLayer 内で meta_judgment Playbook 起動
  → ペルソナが Track 操作を判断
```

**残作業**:

- `pause_for_user` / `resume_from_user` の挙動を alert 経路と整合させる (ユーザー割り込み駆動の Track 切り替えが alert で表現できているか検証)
- env `SAIVERSE_META_LAYER_INTERVAL_SECONDS` で interval 上書きできるようにする (現状は引数経由のみ)
- 当初想定していた「MainLineScheduler 新設して AutonomyManager を削除」は不要 (再利用方針に修正)

**未解決の論点**:

「メインライン Pulse の起動」と「メタ判断 Pulse の起動」を区別する必要があるか？ 現状はメタ判断 Pulse 1 種類だけだが、Phase 4-d で `MainLineScheduler` 相当の機構が要るかは要再検討。要らないなら AutonomyManager + alert observer で完結する。

### `model_configs.py` への `cache_ttl_seconds` 追加

各モデル設定に `cache_ttl_seconds` フィールドを追加:

```json
{
  "model": "claude-sonnet-4-6-20260101",
  "provider": "anthropic",
  "cache_ttl_seconds": 240,
  ...
}
```

ローカルモデル (Ollama / llama.cpp) は `cache_ttl_seconds: null` (無制限) または特に設定しない。

### Pattern A/B/C の自動推定

ペルソナ作成時に `DEFAULT_MODEL` (重量級モデル) と `LIGHTWEIGHT_MODEL` (軽量モデル) の組み合わせから Pattern を推定:

```python
def detect_pulse_pattern(default_model: str, lightweight_model: str) -> str:
    default_provider = get_provider(default_model)
    lite_provider = get_provider(lightweight_model)
    
    if default_provider == "anthropic" and lite_provider in ("ollama", "llama_cpp"):
        return "A"  # Claude メイン + ローカルサブ
    elif default_provider == "anthropic" and lite_provider == "anthropic":
        return "B"  # 全 Claude
    elif default_provider in ("ollama", "llama_cpp"):
        return "C"  # 全ローカル
    else:
        return "A"  # フォールバック
```

検出した Pattern に対応するデフォルト値を Track の metadata に書き込む。

---

## サブ Phase の分割

旧 Phase C-3a/b/c/d に対応:

| サブ Phase | 内容 | 状態 |
|-----------|------|------|
| 4-a (旧 C-3a) | Handler に v0.10 拡張属性追加 + AutonomousTrackHandler 新設 + track_autonomous.json | ✅ 完了 |
| 4-b (旧 C-3b) | SubLineScheduler 新設 (まずこちらを動かす、メインラインは手動起動でも OK) | ✅ 完了 |
| 4-c (旧 C-3c) | AutonomyManager のタイマー化 + `meta_judgment` Playbook + `on_periodic_tick` 入口 | ✅ 完了 (Phase C-2 で実施済み) |
| 4-d (旧 C-3d) | 既存 ConversationManager との関係整理 | ✅ 完了 (2026-05-01 で no-op 化) |
| 4-e (新規) | メタ判断 Pulse の失敗時リカバリ + `pause_for_user` / `resume_from_user` の alert 経路統合 | 🔲 未着手 |
| 4-f (新規) | `MainLineScheduler` 相当の機構が必要かの精査 + 必要なら設計 | 🔲 未着手 |

最小実装としては 4-a + 4-b + 4-c で「自律 Track が立ったら勝手に走り続け、メタ判断が定期的に走る」状態は既に達成。残りは 4-e のリカバリ整備と 4-f の設計判断。

---

## 完了の判定基準

- [x] SubLineScheduler が動作し、自律 Track が立ったら定期的に Pulse が走る
- [x] Handler に Pulse 制御属性が揃い、metadata 経由で個別調整可能
- [x] AutonomyManager がメタレイヤー定期 tick タイマーとして動作し、`on_periodic_tick` が呼ばれる
- [x] ConversationManager と Scheduler 群の責務が整理され、重複や競合がない (ConversationManager は no-op 化)
- [ ] env `SAIVERSE_META_LAYER_INTERVAL_SECONDS` で interval 上書き可能
- [ ] メタ判断 Pulse の失敗時リカバリ (LLM error / parse error / Lock 解放 / Track 状態整合) が定義され、テスト済み
- [ ] `pause_for_user` / `resume_from_user` の挙動が alert 経路で表現可能であることが検証済み
- [ ] Pattern A/B/C が自動推定され、ペルソナ作成時に metadata に書き込まれる
- [ ] 「MainLineScheduler 相当が必要か」の精査結果に基づく対応が完了

---

## Phase 5 以降への前提条件

- MainLineScheduler が動いていること → Phase 5 の Handler tick 機構との協調
- `on_periodic_tick` が動いていること → Phase 5 の内部 alert がメタ判断に乗る
- 7 制御点が運用可能な状態 → Phase 5 の Track パラメータが意味を持つ

---

## 関連ドキュメント

- [../02_mechanics.md](../02_mechanics.md) — Pulse 階層 / 7 制御点 / Pulse 完了後挙動
- [../04_handlers.md](../04_handlers.md) — Handler 基底属性
- [phase_5_autonomy.md](phase_5_autonomy.md) — Handler tick / 内部 alert
