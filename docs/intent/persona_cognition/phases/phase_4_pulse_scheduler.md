# Phase 4 — Pulse 階層 + Scheduler + メタ定期判断

**親**: [../README.md](../README.md)
**ステータス**: 🟡 約 40%
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
| `MainLineScheduler` クラス | 🔲 | コメント `Phase C-3c で別途実装予定` (`pulse_scheduler.py:18`) |
| `AutonomyManager` の `MainLineScheduler` への移管 | 🔲 | 旧 `autonomy_manager.py` は現存 (レガシー残置) |
| `MainLineScheduler` ↔ `SubLineScheduler` の連携 (TTL 接近で main 起動) | 🔲 | MainLineScheduler 実装後 |

### メタレイヤー定期実行 (Phase 4-c)

| 項目 | 状態 |
|------|------|
| `MetaLayer.on_periodic_tick(persona_id, context)` 入口 | 🔲 |
| `on_track_alert` と `on_periodic_tick` の判断ループ共通化 | 🔲 |
| 環境変数 `SAIVERSE_META_LAYER_INTERVAL_SECONDS` (デフォルト 3000) | 🔲 |

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

### `AutonomyManager` の責務再配置

既存 `saiverse/autonomy_manager.py` (872 行) は **MainLineScheduler** に再配置する:

- 既存の Decision/Execution 分離 → メインライン Pulse の起動経路に転用
- 自律行動の意思決定ロジック → メタ判断 Playbook へ移植 (中身は Playbook で書く)
- `pause_for_user` / `resume_from_user` → MainLineScheduler の優先度制御に統合 (alert 駆動と同じ枠組み)

**段階的移行**:

1. MainLineScheduler を新設して動作確認
2. AutonomyManager の Decision/Execution ロジックを段階的に移植
3. AutonomyManager を空殻化 (deprecated 警告のみ)
4. すべての参照を MainLineScheduler に切り替え後、削除

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
| 4-c (旧 C-3c) | AutonomyManager → MainLineScheduler 再配置 + メタ判断 Playbook 新設 | 🔲 未着手 |
| 4-d (旧 C-3d) | 既存 ConversationManager との関係整理 | 🔲 未着手 |

最小実装としては 4-a + 4-b で「自律 Track が立ったら勝手に走り続ける」状態は作れる (= 既に達成)。4-c でメインライン定期実行が乗る。

---

## 完了の判定基準

- [x] SubLineScheduler が動作し、自律 Track が立ったら定期的に Pulse が走る
- [x] Handler に Pulse 制御属性が揃い、metadata 経由で個別調整可能
- [ ] MainLineScheduler が動作し、`SAIVERSE_META_LAYER_INTERVAL_SECONDS` 経過で `on_periodic_tick` が呼ばれる
- [ ] AutonomyManager のロジックが完全に MainLineScheduler に移植され、autonomy_manager.py が削除される
- [ ] Pattern A/B/C が自動推定され、ペルソナ作成時に metadata に書き込まれる
- [ ] ConversationManager と Scheduler 群の責務が整理され、重複や競合がない

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
