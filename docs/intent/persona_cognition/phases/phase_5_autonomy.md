# Phase 5 — 自律稼働の本格化

**親**: [../README.md](../README.md)
**ステータス**: 🔲 未着手
**旧称**: 旧 Intent B v0.7「Handler tick / 内部 alert / Track パラメータ機構」 + 一部 v0.4

---

## 目的

「ペルソナが自分の意思で動く」を技術的に支える層。Handler tick による内部 alert + Track パラメータ機構 + ScheduleManager の Track 化。これにより**外部発話**・**内部欲求**・**スケジュール**の 3 系統が同じ alert 機構で統一される。

---

## タスク

### Handler tick 機構

| 項目 | 状態 |
|------|------|
| Handler 基底に `tick(persona_id)` メソッド | 🔲 |
| SAIVerseManager の background polling loop に Handler tick 呼び出しを統合 | 🔲 |
| 環境変数 `SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS` (デフォルト 60) | 🔲 |
| Handler 側 `register_tick(scheduler)` 登録 API | 🔲 |

### 内部 alert ポーラ

| 項目 | 状態 |
|------|------|
| Handler の `tick()` 内で閾値判定 + `set_alert` 発火 | 🔲 |
| 既存 `set_alert` 機構の context 拡張 (内部 alert 識別) | 🔲 |
| MetaLayer 側で内部 alert を外部 alert と区別せず受け取る確認 | 🔲 |

### Track パラメータ機構

| 項目 | 状態 |
|------|------|
| `action_tracks.metadata.parameters` 連続値の運用 | 🔲 |
| メタレイヤー判断プロンプトに parameters を含める処理 | 🔲 |
| `track_parameter_set` ツール (ペルソナ自身による明示更新) | 🔲 |
| パラメータ更新の経路 3 種 (tick / 外部イベント / ツール) の整備 | 🔲 |

### Phase 5 の新 Handler

| Handler | 用途 | 状態 |
|---------|------|------|
| `SomaticHandler` | 身体的欲求 Track (空腹度、睡眠負債等) | 🔲 |
| `ScheduledHandler` | スケジュール起因 Track (毎週日曜 / ごみ出し曜日朝等) | 🔲 |
| `PerceptualHandler` | 知覚起因 Track (SNS タイムライン経過時間等) | 🔲 |

### スケジュール統合 (Phase 5 内で並走)

| 項目 | 状態 |
|------|------|
| 既存 ScheduleManager と新規 ScheduledHandler の並走対応 | 🔲 |
| Track の `metadata.schedules` に書き込む形を新設 | 🔲 |
| ScheduledHandler が tick 時に `metadata.schedules` を見て時刻到来判定 | 🔲 |
| (完全移行は Phase 6 で) | - |

### ペルソナ再会機能の汎用化

| 項目 | 状態 |
|------|------|
| occupancy event を交流 Track の alert トリガーに統合 | 🔲 |
| Person Note 自動開封のロジックを SocialTrackHandler に移植 | 🔲 |
| 既存 `recall_conversation_with` の段階的廃止 | 🔲 |
| 移行期間中の既存実装と新実装の共存 | 🔲 |

---

## 残タスクの詳細

### Handler tick の実装パターン

```python
class SomaticHandler:
    track_type = "somatic"
    
    def tick(self, persona_id):
        for track in self.list_my_tracks(persona_id):
            params = self._read_parameters(track)
            
            # パラメータの自然変化 (例: 空腹度の時間経過上昇)
            elapsed_min = (now() - track.last_parameter_update).total_seconds() / 60
            params["hunger"] = min(1.0, params["hunger"] + 0.01 * elapsed_min)
            
            # 閾値判定
            if params["hunger"] >= track.thresholds.get("hunger_alert", 0.8):
                self.track_manager.set_alert(
                    track.track_id,
                    context={
                        "trigger": "internal_alert",
                        "param": "hunger",
                        "value": params["hunger"],
                    }
                )
            
            # パラメータ書き戻し
            self._write_parameters(track, params)
```

### Track パラメータの 3 経路

| 経路 | 主体 | 例 |
|------|------|-----|
| Track 自身のポーラ | Handler の tick() | 空腹度の時間経過上昇 |
| 外部イベント | addon / 既存イベント経路 | occupancy 変化で「最後に外で過ごした時間」をリセット |
| ペルソナ自身による明示更新 | `track_parameter_set` ツール | 「この掃除 Track は十分やったから dirtiness を 0 に戻す」 |

### Track パラメータのプロンプト注入

メタレイヤー判断時にプロンプトに含める:

```
[現状]
running: なし
pending Track:
  - id=t_clean, title="掃除", type=scheduled, parameters={dirtiness: 0.65}
  - id=t_sns, title="SNS確認", type=perceptual, parameters={hours_since_check: 0.45}
```

メタレイヤーはパラメータを直接観測でき、alert に至っていなくても「そろそろ気にすべき」という重み付けを判断に組み込める。

### Phase 5 の新 Handler 実装方針

**`SomaticHandler`**:

- 身体的欲求 Track (空腹度、睡眠負債、疲労度等) を管理
- 各パラメータは時間経過で連続変化
- 閾値超過で内部 alert
- 例: 空腹度 80% 超過 → 食事 Track が内部 alert

**`ScheduledHandler`**:

- 時刻到来で alert 化する Track を管理
- `metadata.schedules` の cron 式を tick 時に評価
- 時刻到来 + 蓄積パラメータの組み合わせも可能 (例: 締切 24 時間前 + 進捗不足)
- 例: 30 分間 SNS 未確認 → SNS Track が内部 alert

**`PerceptualHandler`**:

- 「最後に確認してから」の経過時間や未確認件数で alert
- 外部チャネルからの新着通知数を追跡
- 例: X タイムラインに新着 5 件 → 確認 Track が alert

### ペルソナ再会機能の新基盤への移行

既存 `persona/history_manager.py` の `recall_conversation_with` を新基盤上に再実装。

**新基盤での再会フロー**:

1. occupancy event 検出 (既存通り)
2. SocialTrackHandler が alert 化 (audience に自分が含まれる発話 or 入室イベント)
3. 相手ペルソナの **Person Note** を検索
4. 既存 Person Note があればその内容を読み込み、Track の開封 Note リストに追加
5. 既存 Note がなければ新規作成 (Person Note は最初の会話で自動作成)
6. メタレイヤーが「今この Track をアクティブにすべきか」を判断 (A/B フロー)

→ 「再会」は特殊機能ではなく、汎用機構の **occupancy event 由来の Person Note 自動開封**という位置づけになる。

---

## 完了の判定基準

- [ ] Handler tick 機構が動き、SomaticHandler 等のサンプル Handler が tick 経由で内部 alert を発火できる
- [ ] Track パラメータがメタレイヤーのプロンプトに自動注入される
- [ ] `track_parameter_set` ツールでペルソナ自身がパラメータを書き換えられる
- [ ] SomaticHandler / ScheduledHandler / PerceptualHandler のうち少なくとも 1 つが運用ペルソナで動作確認済み
- [ ] ScheduleManager と並走して、Track 経由のスケジュール起因 alert が動く
- [ ] ペルソナ再会機能が SocialTrackHandler 経由で動き、既存の `recall_conversation_with` が deprecated になる

---

## 創発 Track の生成 (Phase 5 では扱わない)

「**そんな Track はもともと存在しなかった、しかし自分で必要だと判断して作った**」というレベルの自発性は、認知モデルの最終形だが極めて難度が高い。Phase 5 では以下の素直な経路から先に整備する:

- 内部欲求 (空腹度等) → Track 起動
- スケジュール起因 → Track 起動
- 外部 alert → Track 切り替え
- アイドル時の継続判断 (pending Track 再開、新規簡易 Track 作成)

創発 Track は Phase 6 で長期的に取り組む課題として位置づける。

---

## Phase 6 以降への前提条件

- Handler tick 機構が動いていること → Phase 6 のモニタリングラインが類似機構を流用
- 内部 alert / 外部 alert / スケジュール alert が同じ機構で統一されていること → Phase 6 の ScheduleManager 完全廃止が安全に行える
- ペルソナ再会の汎用化が完了していること → 既存特化機能の整理が進む

---

## 関連ドキュメント

- [../01_concepts.md](../01_concepts.md) — Track 特性 / Track パラメータ / 内部 alert
- [../04_handlers.md](../04_handlers.md) — Handler パターン (tick メソッドの位置づけ)
- [phase_4_pulse_scheduler.md](phase_4_pulse_scheduler.md) — メタレイヤー定期判断 (前提)
- [phase_6_extensions.md](phase_6_extensions.md) — モニタリングライン / ScheduleManager 完全廃止
