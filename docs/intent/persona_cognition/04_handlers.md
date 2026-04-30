# 認知モデル: Handler パターン

**親**: [README.md](README.md)
**関連**: [01_concepts.md](01_concepts.md) (用語) / [02_mechanics.md](02_mechanics.md) (動的な仕組み) / [03_data_model.md](03_data_model.md) (永続化)

このファイルは Track 種別ごとの**振る舞いの差**を実装する Handler パターンを集約する。

---

## 設計方針

Track 種別ごとに固有の特性 (alert 化条件・Pulse リズム・許可される Playbook) があるが、これを TrackManager 本体に詰め込むと責務が肥大化する。

→ **Handler パターン**を Track 種別ごとに繰り返し適用する。TrackManager 側は変更しない。

```
saiverse/track_handlers/
├── __init__.py
├── user_conversation_handler.py     # Phase 2 実装済み (対ユーザー Track)
├── social_track_handler.py          # Phase 2 実装済み (交流 Track)
├── autonomous_track_handler.py      # Phase 2 実装済み (自律 Track)
├── (Phase 5) somatic_handler.py            # 身体的欲求 Track (空腹度 etc.)
├── (Phase 5) scheduled_handler.py          # スケジュール起因 Track
└── (Phase 5) perceptual_handler.py         # 知覚起因 Track (SNS 経過時間 etc.)
```

各 Handler の責務:

- 対応する Track 種別の **取得 / 自動作成** (`ensure_track` / `get_or_create_track`)
- その種別固有の **イベント受け口** (例: `on_user_utterance`, `on_persona_utterance`, `on_parameter_threshold`)
- 「running なら直接、そうでなければ alert 化」の分岐 (Phase 2 で確立したパターン)

新しい Track 種別はそれぞれ Handler クラスを追加するだけで拡張可能。SAIVerseManager は対応する Handler をディスパッチ先として管理する。

---

## Handler 基底属性

各 Handler が定義するクラス属性:

```python
class TrackHandlerBase:
    # メッセージング (Pulse 開始プロンプトの固定情報セクションで利用)
    pulse_completion_notice: str = "..."     # この Track のリズム説明
    track_specific_guidance: str = "..."     # 種別固有の振る舞い指針
    post_complete_behavior: str = "wait_response"  # or "meta_judge"
    
    # Pulse 制御 (Phase 4 で導入)
    default_pulse_interval: int = 30                # Track 単位の Pulse 間隔 (秒)
    default_max_consecutive_pulses: int = -1        # -1 = 無制限
    default_subline_pulse_interval: int = 0         # サブライン連続時の各 Pulse 間隔 (秒)
    
    # Phase 5 で導入
    def tick(self, persona_id):
        """定期的に呼ばれ、Track パラメータ更新と内部 alert 判定を行う。"""
        ...
```

### `pulse_completion_notice` (Phase 4)

Pulse 完了後どう振る舞うかを**ペルソナに伝える**文字列。Pulse 開始プロンプトの固定情報セクションに含める:

```python
class UserConversationTrackHandler:
    pulse_completion_notice = (
        "このパルスは対ユーザー会話 Track のもの。"
        "Pulse 完了後はユーザーの返答を待つ状態に入る。"
        "次のイベントが来るまで他のことを考えなくて良い。"
    )

class AutonomousTrackHandler:
    pulse_completion_notice = (
        "このパルスは自律 Track のもの。"
        "Pulse 完了後はメタレイヤーが次の判断をする。"
        "続行するか別 Track に切り替えるかは任せて良い。"
    )
```

### `post_complete_behavior` (Phase 4)

機械可読な分類:

| 値 | 意味 | 該当 Track 種別 |
|---|---|---|
| `wait_response` | 応答待ち型。次のイベントまで休む | user_conversation, social, external, waiting |
| `meta_judge` | 連続実行型。一段落 → メタレイヤー判断 | autonomous, scheduled (Phase 5), somatic (Phase 5) |

メタレイヤー定期実行が来た時、現 running Track の Handler の `post_complete_behavior` を見て:

- `wait_response`: 抑止 (ユーザー応答待ちなので発火しない)
- `meta_judge`: 通常判断 (続行か切り替えか判断)

### Pulse 制御属性 (Phase 4)

```python
class UserConversationTrackHandler:
    post_complete_behavior = "wait_response"
    default_pulse_interval = 0  # ユーザー応答が来たら即起動なので関係ない

class AutonomousTrackHandler:
    post_complete_behavior = "meta_judge"
    default_pulse_interval = 30  # 30 秒に 1 回サブライン Pulse
    default_max_consecutive_pulses = -1  # メインキャッシュ TTL までは無制限
    default_subline_pulse_interval = 0  # 連続実行 (ローカル前提)
```

各 Handler が Track 種別固有のデフォルトを定義する。Track 個別の値は `action_tracks.metadata` で上書き可能 (= [02_mechanics.md の 7 制御点](02_mechanics.md#pulse-サイクルの-7-つの制御点) の (1)(2)(7))。

---

## 既存 Handler の概要

### `UserConversationTrackHandler` (Phase 2 実装済み)

対ユーザー会話 Track。永続 Track (`is_persistent=true`)。

- **alert トリガー**: ユーザー発話イベント
- **post_complete_behavior**: `wait_response`
- **output_target**: `building:current` または `external:...`
- **Note**: 該当ユーザーの Person Note を自動開封

### `SocialTrackHandler` (Phase 2 実装済み)

ペルソナ間の会話を扱う交流 Track。永続 Track。ペルソナにつき 1 個。

- **alert トリガー**: 同 Building 内の他ペルソナ発話 (audience に自分が含まれる)
- **post_complete_behavior**: `wait_response`
- **output_target**: `building:current`
- **Note**: 相手ペルソナの Person Note を自動開封

### `AutonomousTrackHandler` (Phase 2 実装済み)

プロジェクト遂行・記憶整理・創作等の自律行動。一時 Track。

- **alert トリガー**: なし (メインライン判断で `track_create` → `track_activate`)
- **post_complete_behavior**: `meta_judge`
- **output_target**: `none` (基本独白)
- **Note**: 対象 Project Note + Vocation Note

---

## Track 特性の追加情報管理

各 Track 種別が必要な追加情報 (パラメータ、スケジュール定義、内部 alert 閾値等) は、`action_tracks.metadata` フィールド (JSON) に格納する。スキーマ拡張を都度行わない方針。

例 (掃除 Track、Phase 5):

```json
{
  "parameters": {
    "dirtiness": 0.32
  },
  "schedules": [
    {"cron": "0 10 * * 0", "label": "weekly"}
  ],
  "thresholds": {
    "dirtiness_alert": 0.7
  }
}
```

将来この共通形式を `track_parameters` テーブル等として正規化する可能性はあるが、まず metadata JSON で運用してから判断する。

---

## Handler tick 機構 (Phase 5 で導入)

### tick の責務

専用クラスを Handler ごとに作るのではなく、**Handler の `tick()` メソッド内で判定 + set_alert 発火**する形に統一する:

```python
class SomaticHandler:
    def tick(self, persona_id):
        for track in self.list_my_tracks(persona_id):
            params = self._read_parameters(track)
            if params["hunger"] >= track.thresholds.get("hunger_alert", 0.8):
                self.track_manager.set_alert(
                    track.track_id,
                    context={"trigger": "internal_alert", "param": "hunger", "value": params["hunger"]}
                )
```

`set_alert` は Phase 2 既実装の機構をそのまま使う。alert observer (MetaLayer) は外部 alert と内部 alert を区別せず受け取る (context で判別)。

### tick 駆動

SAIVerseManager の既存の background polling loop に Handler tick の呼び出しを足す方針。Handler 側に `register_tick(scheduler)` のような登録 API を持たせ、SAIVerseManager は scheduler 経由で全 Handler を回す。

頻度はパラメータ種別による:

- 身体的欲求: 1 分間隔程度
- スケジュール時刻: 1 分間隔程度
- 知覚起因: 5 分間隔程度

環境変数: `SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS` (デフォルト 60)。

---

## Track 種別ごとの専用 Playbook

メインラインの Pulse 開始プロンプトに使用可能 Playbook 候補を含めるため、Track 種別ごとに**専用 Playbook**を用意する。

### 命名

| Playbook | 用途 | Phase |
|----------|------|-------|
| `track_user_conversation.json` | 対ユーザー Track 用 | Phase 3 (実装済み) |
| `track_autonomous.json` | 自律 Track 用 (記憶整理 / 開発 / 創作の汎用基盤) | Phase 3 (実装済み) |
| `meta_judgment.json` | メタ判断専用 | Phase 3 (実装済み、Phase 1.2 でマージ) |
| `track_social.json` | 交流 Track 用 | Phase 3 (未着手) |
| `track_external.json` | 外部通信 Track 用 | Phase 3 (未着手) |
| `track_waiting.json` | 待機 Track の起動時 (応答到達後の処理) | Phase 3 (未着手) |

各 Playbook はメインライン (重量級) で:

- Pulse 開始プロンプト構成 (固定情報 + 動的情報) を組み立てる
- Track 状況 + 候補から判断
- 応答生成 or サブ Playbook 呼び出し or スペル発火

### 既存 SEA との整合: (a) 路線

既存の `meta_user` Playbook (router ノードが軽量モデル) はそのまま流用しない。代わりに **Track 種別ごとに専用 Playbook を新規作成**する。理由:

- (a) 路線 (Playbook 機構を活かして拡張する) の方がユーザーの小回りが効く
- 新規 Playbook を書けば、既存 `meta_user` の挙動を破壊せず段階移行できる
- Track 種別ごとに最適化された Playbook を持てる

---

## Track 種別を追加する手順

新しい Track 種別 (例: `vocation_practice` = 創作練習用) を追加する場合:

1. **Handler クラス**を新設
   ```python
   # saiverse/track_handlers/vocation_practice_handler.py
   class VocationPracticeTrackHandler(TrackHandlerBase):
       track_type = "vocation_practice"
       pulse_completion_notice = "..."
       track_specific_guidance = "..."
       post_complete_behavior = "meta_judge"
       default_pulse_interval = 60
       # ...
   ```

2. **Playbook**を新設 (`builtin_data/playbooks/public/track_vocation_practice.json`)
3. SAIVerseManager の Handler 登録に追加
4. (必要なら) tick メソッドで内部 alert 条件を実装

---

## Handler の責務分離原則

- **TrackManager 側は変更しない**: 種別ごとの専用メソッドを足さないのが原則 (Phase 2 で確立した方針)
- **状態遷移の機構は共通化**: 既存の `track_*` ツール群 + `set_alert` を全 Handler が利用
- **Handler 固有の追加情報は metadata に**: 早すぎる正規化を避ける

---

## スケジュール統合方針

既存の ScheduleManager (個別スケジュール作業) は段階的に Track の特性として吸収する。

### Phase 5 では既存 ScheduleManager と共存

- 既存 ScheduleManager はそのまま動かす
- 新規 Track 創設時にスケジュールを与えたい場合は、Track の `metadata.schedules` に書き込む形を新設
- ScheduledHandler が tick 時に `metadata.schedules` を見て時刻到来判定 → set_alert

### Phase 6 で完全移行

- 既存 ScheduleManager の機能を ScheduledHandler に移植
- 既存スケジュールは migration で対応する Track + metadata.schedules 形式に変換
- ScheduleManager クラスは廃止

これにより「外部発話」「内部欲求」「スケジュール」の 3 系統が同じ alert 機構で統一され、メタレイヤーは区別せず判断できる。

---

## 関連ドキュメント

- [01_concepts.md](01_concepts.md) — Track 特性 / Track パラメータの概念
- [02_mechanics.md](02_mechanics.md) — Pulse 階層と 7 制御点
- [03_data_model.md](03_data_model.md) — `action_tracks.metadata` の構造
- [phases/phase_2_track_metalayer.md](phases/phase_2_track_metalayer.md) — Handler 基盤の実装状況
- [phases/phase_4_pulse_scheduler.md](phases/phase_4_pulse_scheduler.md) — Pulse 制御属性の追加
- [phases/phase_5_autonomy.md](phases/phase_5_autonomy.md) — Handler tick / 内部 alert / Track パラメータ
