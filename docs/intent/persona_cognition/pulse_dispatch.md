# Intent: Pulse 起動経路ディスパッチ

**親**: [README.md](README.md)
**ステータス**: v0.3 実装一巡完了 (2026-05-10、段階 1〜5 完了 / ケース 4 実機検証済 / ケース 5・6 は自律稼働中の発見ベースで観察中)
**関連 Intent**: [meta_judgment_structured.md](meta_judgment_structured.md), [track_chronicle.md](track_chronicle.md), [02_mechanics.md](02_mechanics.md), [04_handlers.md](04_handlers.md)
**関連 handoff**: [handoff_2026-05-10.md](handoff_2026-05-10.md) (origin_track_id 経路修正の過程で本 Intent の必要性が顕在化)

---

## 1. これは何か

ペルソナが「動く」(Pulse が走る) すべての事例を **統一土台** に載せる Intent。Pulse の起動経路をディスパッチャ層で一元化し、メタ判断を独立した並列レーンとして切り出すことで、各 Track 種別・各イベント種別ごとに散らばっていた起動ロジックを整理する。

スコープ:
- すべての Pulse 起動経路の網羅
- 直接経路 / 熟慮経路 / メタ判断並列レーン の 3 構造への整理
- `on_track_activated` hook の導入と `_inject_track_context` の統一
- PulseController の改修 (メタ判断並列レーン)
- alert 発生経路の網羅 (現役 + 将来運用)

スコープ外:
- スケジュール ↔ Track 紐付けの詳細設計 (別 Intent doc 予定、本 doc では概念のみ言及)
- Phase 5 の Handler tick / SomaticHandler 等の各 Handler 雛形実装 (本 doc は枠組みのみ)
- 監査 Track 等の将来の並列実行ユースケース (本 doc では並列を許容する設計だけ示す)

---

## 2. 動機

### 2.1 現状の歪み (handoff_2026-05-10 で顕在化)

`origin_track_id NULL` バグの追跡中に、ペルソナの動作経路が起点ごとに ad-hoc に組まれていることが判明:

- **`UserConversationTrackHandler.on_user_utterance`** がメタ判断の発火・Track コンテキスト注入・メインライン応答起動を全部抱え込み、内部に `invoke_main_line()` のハードコード経路を持つ
- メタ判断 Pulse は `MetaLayer.on_track_alert` / `on_periodic_tick` から `runtime.run_meta_user()` を **PulseController を経由せず直接呼ぶ**
- 自律 Track の連続 Pulse は SubLineScheduler の独自 5 秒 poll loop
- AutonomyManager は EventScheduler の push 駆動
- スケジュールは `submit_schedule`、現象 (Phenomena) も同じ `submit_schedule` を直叩き

これらは単独では妥当だが、全体像として「ペルソナが動くとはどういうことか」の統一モデルが無い。同じ「Track activate 後の挙動」がケース1 (ユーザー発話起因) では Handler 経由で実装され、ケース2 (自律) では実装が無い、という非対称が起きていた。

### 2.2 統一の必要性

ペルソナ認知モデル (Intent A v0.14) が成熟するにつれ、次の事象が増える:

- **自律稼働でペルソナが自分から動く**: メタ判断 → Track activate → 発話 Pulse の経路が必要
- **Pulse 中の割り込み判断**: Track Pulse 進行中に alert が発生したとき、中断 / 継続の判断にメタ判断並列実行が必須
- **Phase 5 で Handler tick / 内部 alert ポーラの運用化**: 内部状態の閾値超過で Track が自発的に alert 化する経路が増える
- **スケジュールの Track 紐付け**: スケジュールが Track 設定 (output_target / 発話するか / 内部処理か) に従って分岐する必要がある

これらを各起点に分散したまま積むと、`origin_track_id` 系のような回帰バグが繰り返し発生する。

---

## 3. ペルソナが動く事例の網羅

### 3.1 Pulse 起動契機 (起動トリガー)

| # | トリガー | 状態 | 経路 | 起動 Pulse | 現状 |
|---|---|---|---|---|---|
| 1a | ユーザー発話 | user_conversation Track が running | **直接** | main_line | 動いてる (`invoke_main_line` 直叩き) |
| 1b | ユーザー発話 | user_conversation Track が pending/alert/unstarted | **衝突なし: 直接 / 衝突あり: 熟慮** | 直接 activate → main_line / meta_judgment → main_line | 動いてる (2026-07-07 改訂、§4.2 参照) |
| 2a | SubLineScheduler 5秒 poll | autonomous Track が running | **直接** | sub_line | 動いてる |
| 2b | AutonomyManager push tick | persona idle / interval 到来 | **熟慮** | meta_judgment → (activate あれば) main_line | メタ判断は走るが activate 後の Pulse 起動が未実装 |
| 3 | スケジュール時刻到来 | 任意 | **直接** | (スケジュール定義による) | 動いてる、ただし Track 紐付けは未対応 |
| 5 | wait_response timeout | user_conversation Track が running | **熟慮** | meta_judgment → ? | timeout → 自動 pause は実装、メタ判断連動は handoff_2026-05-09 範囲 |
| 8 | 時間差ツール完了 (Phase 5) | 任意 | **熟慮** | meta_judgment → ? | 🔲 未実装 |
| 9 | Phenomenon 発火 | 任意 | **直接** | (現象定義による) | 動いてる (`inject_persona_event` 経由) |

### 3.2 Pulse 起動契機ではないがペルソナの動作に影響する事象

| # | 事象 | 内容 |
|---|---|---|
| 4 | 他ペルソナの Building 内発話 | Building 履歴に積まれるが Pulse 起動契機にはならない。次回 Pulse 起動時に `auto_ingest_building_messages` で取り込まれる |
| 6 | 入退室 (DynamicState 差分) | 何も記録されない。次回 Pulse 起動時に `DynamicStateManager.maybe_inject_event_messages` が B/C 比較で event_message を生成・注入 |
| 7 | Pulse 中断 → 再開 | 高 priority Pulse 到着で auto Pulse 等が中断、wait policy なら resumption queue (PulseController 範疇) |

これらは Pulse 起動経路ではないので、ディスパッチャの分岐対象ではない。Pulse 起動時の前処理 (context 注入) や PulseController 内部の処理として既に動いている。

### 3.3 メタ判断 Pulse の特殊性

事例 1b / 2b / 5 / 8 (熟慮経路) で発火するメタ判断 Pulse 自体は:

- `runtime.run_meta_user(... pulse_type="meta_judgment")` を **MetaLayer から PulseController を経由せず直接呼ぶ**
- per-persona Lock (`MetaLayer._get_lock`) で同一ペルソナのメタ判断は直列化
- 結果として Pulse の deferred_track_ops が Pulse 完了時に apply され Track 状態遷移が起きる

これは「Pulse 起動経路の 1 種別」ではなく、**他 Pulse と並列で動く独立レーン** として再位置付けする (詳細は §6)。

---

## 4. 経路の 3 構造

### 4.1 直接経路 (Direct Path)

メタ判断を介さず即時に Pulse を起動する経路。

対象: 1a, 1b (running 衝突なし — 2026-07-07 改訂、§4.2 参照), 2a, 3, 9

特徴:
- イベント受信 → 即時 `PulseController.submit_xxx` で Pulse 起動
- メタ判断のラウンドトリップが無いので低レイテンシ
- Track 状態遷移は伴わない (running 継続が前提)

例:
- 1a: ユーザー発話 → 既存 running Track へ即応 → main_line Pulse
- 2a: SubLineScheduler poll → autonomous Track running → sub_line Pulse
- 3: スケジュール時刻到来 → スケジュール定義の Pulse 種別で起動
- 9: Phenomenon 発火 → `submit_schedule` (= SCHEDULE priority で起動)

### 4.2 熟慮経路 (Reflective Path)

メタ判断を経由してから Pulse を起動するか / 起動しないかを決める経路。

対象: 1b (running 衝突あり), 2b, 5, 8

フロー:

```
イベント発生 (alert 化 or tick)
   ↓
TrackManager.set_alert (alert 化が伴う場合)
   ↓
MetaLayer.on_track_alert / on_periodic_tick
   ↓
メタ判断 Pulse (並列レーンで実行 — §4.3)
   ↓
deferred_track_ops apply (activate / pause / etc)
   ↓
[Track が running に遷移した場合]
   ↓
on_track_activated hook (§5)
   ↓
Handler が _inject_track_context + Pulse 起動を判断
   ↓
PulseController.submit_xxx で Pulse 起動 (or 起動しない)
```

「activate しなかった場合は Pulse 起動しない」が原則 (Q2 で確定)。これによりメタ判断結果が「現状維持」だった場合は何も起きない (= 望ましい挙動)。

**Q2 改訂 (2026-07-07)**: 対 user 発話 (1b) について、当初は「Track が running 以外なら常に熟慮経路 (activate されなかった場合は応答しない)」としつつ「応答無しが頻発するなら 1b を直接経路に動かす」と再検討余地を残していた。実際に LIFE_PURPOSE 未設定のペルソナで alert が `meta_judgment_life_purpose` に横取りされ、ユーザーの呼びかけが無応答になる実害が発生したため、以下に改訂した:

- **別の running Track と衝突していない**場合 (Idle への呼びかけ等): メタ判断を経由せず `TrackManager.activate` で対ユーザー Track を直接 activate → `on_track_activated` hook 経由で main_line 応答を起動する (= 常に即応答)
- **別の running Track と衝突している**場合のみ: 従来どおり熟慮経路 (`set_alert` → メタ判断)。activate されなかった場合は応答しない

判定と分岐は `UserConversationTrackHandler.on_user_utterance` が行う (Track 状態の動的判定は Handler 責務 — §7.1)。running 衝突時も強制応答に寄せるかは未決の残論点 (`docs/issues/user_utterance_forced_response_on_running_conflict.md`、自律行動 v2 の割り込みと復帰と合わせて設計する)。

### 4.3 メタ判断並列レーン (Meta-Judgment Parallel Lane)

> **改訂 (2026-07-17、beat_execution_context.md §2.2)**: 「並列に動く」の部分は
> **解体済み**。記憶の一直線性 (execution_ledger.md 不変条件 9) のため、persona の
> 記憶に書く生成処理は persona 単位の Beat ロック (`sea/beat_gate.py`) で直列化され、
> メタ判断は **main レーンの Beat 境界 (spell ループの周の切れ目) に挟まる直列 Beat**
> になった。本節の残りの性質は不変: メタ判断は priority 体系外で、他 Pulse を
> 中断せず・中断されず、緊急時は判断結果が進行中 Pulse を `cancel()` で止める
> (cancel は Beat 境界で効く)。下の「理由」にある『必ず Track Pulse 完了後に走ると
> 緊急 alert に間に合わない』への答えは、並列実行から **Beat 境界での挟み込み**
> (待ちは最大 1 Beat) に置き換えられた。

メタ判断 Pulse 自身は他 Pulse と **並列に動く独立レーン** として位置付ける。

理由:
- Track Pulse 進行中に alert が発生したとき、「Track を継続するか中断するか」をメタ判断が判断する必要がある
- メタ判断が Track Pulse を**必ず中断する** 設計だと、軽い alert でも進行中作業がキャンセルされて望ましくない
- メタ判断が **必ず Track Pulse 完了後に走る** 設計だと、長時間の Track Pulse 中に発生した緊急 alert に間に合わない
- → メタ判断は並列で動き、結果に応じて進行中 Pulse を `CancellationToken.cancel()` で止める or 継続させる

設計:
- PulseController に `submit_meta_judgment` を追加 (or priority=META_JUDGMENT を追加)
- このレーンの Pulse は **他 Pulse を中断せず**、**他 Pulse から中断もされない**
- メタ判断レーンの直列化は MetaLayer の per-persona Lock で従来通り保証
- メタ判断結果として「進行中 Pulse を中断すべき」という判定が出た場合のみ、進行中 Pulse の `cancellation_token.cancel()` を呼ぶ

並列実行の不変条件への影響:
- Intent A 不変条件 1 「同時実行しない (アクティブ Track は常に 1 本)」は **Track の active 概念**の話で、Pulse の並列実行を禁じてはいない
- ただしメタ判断レーンのみが他 Pulse と並列で動くことを認める (汎用の並列実行は別途の議論)
- 監査 Track のような将来ユースケース (人格安定性監査等) はこの並列レーンを枠組みとして流用できる

---

## 5. on_track_activated hook の仕様

### 5.1 発火位置

`TrackManager.activate(track_id, *, pulse_id=None)` の末尾で発火する。

```python
def activate(self, track_id, *, pulse_id=None):
    # ... 既存処理 (running 押し出し / status 変更 / wait_response timer / status_change observer)
    self._notify_track_activated(track.persona_id, track, pulse_id)  # 新規
    return track
```

引数:
- `persona_id`: ペルソナ ID
- `track`: 遷移後の ActionTrack オブジェクト
- `pulse_id`: 発動元 Pulse ID (deferred apply の場合は元のメタ判断 Pulse の id)

### 5.2 Handler 側の責務

`track_handlers/*` の各 Handler に `on_track_activated(persona_id, track, pulse_id)` を実装する。

`UserConversationTrackHandler.on_track_activated` の場合の責務:
- `_inject_track_context(persona_id, track)` を呼ぶ (Track 切替通知の SAIMemory 注入)
- main_line Pulse の起動が必要か判定し、必要なら `manager.run_sea_user(...)` で起動

`AutonomousTrackHandler.on_track_activated` の場合の責務:
- (現状は SubLineScheduler が拾う設計なので) hook では何もしない、または Track Chronicle 注入のみ
- 将来的に「activate 即時に最初の Pulse を起動」する形に整理することも検討可能

### 5.3 _inject_track_context の統一

現状: `UserConversationTrackHandler.on_user_utterance` 内の 2 箇所 (新規作成時 + alert→running 遷移時) でのみ呼ばれる。

新仕様: **`on_track_activated` hook 経由で一本化する**。`on_user_utterance` 内の `_inject_track_context` 呼び出しは削除。

これにより:
- ケース1 (ユーザー発話 → alert → activate) と ケース2 (自律 tick → activate) の両方で同じ経路で Track 切替通知が SAIMemory に入る
- 重複呼び出しが起きない (hook は activate ごとに 1 回しか発火しない)
- 既知の「assistant ロール末尾問題」(Gemini 互換性) も自然に回避される (`<system>` タグ付き user role が末尾に入る)

---

## 6. PulseController 改修

### 6.1 メタ判断専用レーンの追加

現状の priority:

```
USER (1) > SCHEDULE (2) > AUTO (3)
```

新仕様:

```
META_JUDGMENT (parallel lane)  ← 並列レーン、priority 競合外
USER (1) > SCHEDULE (2) > AUTO (3)
```

実装案:
- `ExecutionRequest.type = "meta_judgment"` を追加
- `submit_meta_judgment(persona_id, ...)` ヘルパーを追加
- `_should_interrupt` で META_JUDGMENT を中断対象から外す
- META_JUDGMENT 同士の直列化は MetaLayer の per-persona Lock に委ねる (PulseController は素通し)
- 進行中の Pulse があっても META_JUDGMENT は別レーンで起動する

### 6.2 メタ判断結果に応じた進行中 Pulse の中断

メタ判断が「進行中 Pulse を中断すべき」と判定した場合の経路:

1. メタ判断 Pulse の deferred_track_ops apply で Track 状態が変わる (例: 自律 Track が pending に降ろされる)
2. その状態変化を受けて、進行中の自律 Track Pulse の cancellation_token に cancel 指示
3. 進行中 Pulse は `ExecutionCancelledException` で正常に止まる
4. 必要に応じて新 Pulse 起動 (`on_track_activated` hook 経由)

**実装方針 (v0.2 確定)**: PulseController が Track 状態変化を監視して current pulse を cancel する経路を追加する。TrackManager の `_notify_status_change` observer に PulseController が登録し、自分が抱えている current pulse の Track id と一致する状態変化を受信した場合に、その pulse の `cancellation_token.cancel()` を呼ぶ。

理由:
- PulseController は元々 current pulse の管理を持っているため、cancel 指示の責務を集約しやすい
- TrackManager 側に cancel 経路を持たせると Track 機構と Pulse 機構の責務が交錯する

### 6.3 メタ判断と PulseController の関係を明文化

現状のコードでは MetaLayer から `runtime.run_meta_user` を **PulseController を経由せず直接呼んでいる**。

**実装方針 (v0.2 確定)**: **案 A** を採用。MetaLayer も `PulseController.submit_meta_judgment` を経由する形に統一する。

理由:
- priority 体系・cancellation 経路・Pulse メトリクスが PulseController に集約される
- 並列レーンの実装は PulseController に持たせるのが筋
- MetaLayer 側のコードがシンプルになる (Lock 管理は据え置き)

---

## 7. 統一ディスパッチャの位置づけ

### 7.1 ディスパッチャ層の責務

新規モジュール (仮) `saiverse/pulse_dispatcher.py` (or PulseController に統合) を作る。責務:

- イベント (ユーザー発話 / tick / スケジュール / 現象 / wait_response timeout / 時間差ツール完了) を受け取る
- イベント種別 + Track 状態 から **直接経路 / 熟慮経路** を選択する (ハードコードマップ)
- 経路に応じて `PulseController.submit_xxx` または `MetaLayer.on_track_alert/on_periodic_tick` を呼ぶ

経路選択マップ (例):

```python
# event_kind, track_status → path
("user_utterance", "running") → DIRECT (submit_user with main_line)
("user_utterance", "pending"|"alert"|"unstarted", 別 running Track なし) → DIRECT (直接 activate → hook 経由 main_line)  # 2026-07-07 改訂
("user_utterance", "pending"|"alert"|"unstarted", 別 running Track あり) → REFLECTIVE (set_alert → MetaLayer)
("subline_poll", "running" + autonomous) → DIRECT (submit_auto with sub_line)
("autonomy_tick", any) → REFLECTIVE (on_periodic_tick)
("schedule_fire", any) → DIRECT (submit_schedule, スケジュール定義の Pulse 種別)
("phenomenon_event", any) → DIRECT (submit_schedule)
("wait_response_timeout", "running") → REFLECTIVE (auto pause → on_periodic_tick で次回検討)
("delayed_tool_complete", any) → REFLECTIVE (set_alert → MetaLayer)
```

### 7.2 共通処理の仕込み余地

ディスパッチャ層を通すことで、以下の共通処理を一箇所で扱える:

- イベントメトリクス (どの経路がどれくらい発火しているかの計測)
- 共通 pre-spell の注入
- 経路別のログ
- 将来の並列実行制御 (監査 Track 等の追加)

これがまはーの「中で経路分岐の方が共通処理を仕込める余地があっていい」という意図 (Q1) に対応する。

---

## 8. alert 発生経路の網羅

実装上の経路:

| # | 経路 | 実装状況 | 実用状況 | 備考 |
|---|---|---|---|---|
| α | `UserConversationTrackHandler.on_user_utterance` で `set_alert` | ✅ 実装済 | ✅ 動いてる | ユーザー発話起因の alert 化 (2026-07-07 改訂: 別の running Track と衝突している場合のみ。衝突なしは直接 activate — §4.2) |
| β | `InternalAlertPoller` 60秒 tick でパラメータ閾値超過 | ✅ 実装済 | 🟡 空回り | Track の `metadata.thresholds` 未設定なので閾値判定対象が無い、Phase 5 で運用化 |
| γ | `InternalAlertPoller` 内で `Handler.tick()` を呼ぶ枠組み | ✅ 呼び出し側のみ | 🟡 各 Handler に `tick` 未実装 | Phase 5 で SomaticHandler / ScheduledHandler / PerceptualHandler を実装 |
| δ | 時間差ツール完了 → call_id 経由 alert | 🔲 Phase 5 構想 | 🔲 未実装 | Kitchen / dispatch_persona / X 投稿等のサブタスク |
| ε | スケジュール時刻到来で Track alert 化 | 🔲 Phase 5 構想 | 🔲 未実装 | 旧 ScheduleManager の Track 化に伴う |

本 Intent は枠組みを定義するだけで、各経路の運用化は Phase 5 で別途進める。

---

## 9. 既存実装からの移行ステップ

### 9.1 段階 1: hook 機構の導入 (✅ 完了 commit `ee38637`)

- `TrackManager.activate` 末尾に `on_track_activated` hook 発火を追加
- `track_handlers/__init__.py` に Handler 共通の `on_track_activated` インターフェース定義
- 各 Handler クラスに `on_track_activated(persona_id, track, pulse_id)` を実装

### 9.2 段階 2: `_inject_track_context` 統一 (✅ 完了 commit `ee38637`)

- `UserConversationTrackHandler.on_user_utterance` 内の `_inject_track_context` 呼び出しを削除
- `UserConversationTrackHandler.on_track_activated` 内に移動
- `on_user_utterance` の構造を「set_alert → on_track_alert (MetaLayer 経由) → 戻る」に整理 (Track 切替通知は hook 任せ)

### 9.3 段階 3: `invoke_main_line` ハードコード廃止 (✅ 完了 commit `496ef15`)

- ケース1 (1b) の `invoke_main_line()` ハードコードを削除
- 代わりに `UserConversationTrackHandler.on_track_activated` 内で main_line Pulse を起動
- 注意: 「activate しなかった場合に応答が無い」運用問題の発生有無を観察。多発したら 1b を直接経路に変更する。
- **2026-07-07 追記**: 上記の運用問題が実際に発生した (life_purpose_unset がメタ判断で alert より優先され、Idle ペルソナへの呼びかけが無応答になった)。1b は「別の running Track と衝突していなければ直接 activate (即応答)、衝突時のみ熟慮経路」に改訂 (§4.2 Q2 改訂)。

### 9.4 段階 4: ディスパッチャ層の導入 (✅ 完了 commit `8c34933`)

- `saiverse/pulse_dispatcher.py` を新規作成 (案 X 採用)
- 各起点コード (`manager/runtime.py:handle_user_input_stream`, `SubLineScheduler._tick`, `ScheduleManager._fire_schedule`, `inject_persona_event`, `AutonomyManager._handle_tick`) からディスパッチャ経由に切り替え
- 段階 1〜3 で統一された Handler hook と組み合わせて経路選択

### 9.5 段階 5: メタ判断並列レーンの実装 (✅ 完了 commit `b36dd12`)

- `PulseController` に `submit_meta_judgment` + メタ判断専用レーン (`_current_meta`) を追加
- `submit()` で type=meta_judgment は priority 体系外で並列実行
- `MetaLayer._run_judgment_via_playbook` から `pulse_controller.submit_meta_judgment` 経由に切り替え (旧: `runtime.run_meta_user` 直叩き)
- `_status_change_observers` の signature を `(persona_id, track_id, pulse_id)` に拡張
- `PulseController.on_track_status_change` を追加 (current pulse の origin_track_id と一致する Track の状態変化で `cancellation_token.cancel()`)
- ケース 4 (自律経路) は実機検証済 (2026-05-10、ログで `on_track_activated` → `Starting main_line pulse` の連鎖を確認)
- ケース 5 (並列) / ケース 6 (cancel) は自律稼働中の発見ベースで観察中。Phase 5 の alert 発生経路運用化 (β/γ/δ/ε) が積み上がるにつれて発火頻度が上がる想定

### 9.6 段階 6: alert 発生経路の運用化 (🔲 Phase 5 と協調)

- InternalAlertPoller の運用化 (Track にパラメータ閾値を設定して β/γ を実用化)
- 時間差ツール (δ) と Track alert 化スケジュール (ε) を実装

各段階は個別に検証可能 (1 段階ずつ実装してまはーが動作確認できる粒度) を意識する。

---

## 10. 不変条件と将来の拡張

### 10.1 維持する不変条件

[01_concepts.md](01_concepts.md) の不変条件のうち、本 Intent の影響を受けるもの:

1. **同時実行しない (アクティブ Track は常に 1 本)**: Track の active 概念は維持。**Pulse の並列実行はメタ判断並列レーンに限り認める** (= Track の active 概念とは別軸)
2. **メタレイヤーが切り替えを独占**: 維持。Track 切り替えは deferred_track_ops 経由でメタ判断 Pulse 完了時に apply
3. **キャッシュヒット継続を最優先**: 維持。Track 切り替え時の `_inject_track_context` で末尾追加方式 (キャッシュ前方は不変)
4. **メタレイヤーは恒常的に存在**: 維持。並列レーンとして PulseController に統合される

### 10.2 将来の拡張の余地

- **監査 Track 等の自己監視ユースケース**: メタ判断並列レーンの仕組みを流用して、Track Pulse と並列に動く監視 Track を追加可能。本 Intent ではこの拡張点だけ確保する (具体実装は別 Intent)
- **緊急 Pulse の汎用並列化**: メタ判断レーン以外にも「緊急通知配送」等の並列実行ニーズがあれば、PulseController の並列レーン機構を拡張する形で対応可能

---

## 11. 残課題と関連 Intent

- **スケジュール ↔ Track 紐付け** (Q2 確定): 本 Intent では「スケジュールは Track 設定 (output_target / 発話するか / 内部処理か) を見て分岐する」概念だけ言及。詳細は別 Intent doc で扱う (Phase 5 ScheduledHandler の枠組みと協調)
- **Phase 5 の Handler tick / 内部 alert ポーラ運用化**: β/γ/δ/ε を実用化する作業は本 Intent のスコープ外、Phase 5 で進める
- **`runtime.run_meta_user` メソッド名の整理**: 「meta_user」という名前が Playbook 名 (廃止済) を引きずっている。Pulse 起点の意味を反映した命名に変える余地あり (本 Intent で扱うかは別途判断)
- **段階 4 のディスパッチャ層の置き場所**: 新規モジュール vs PulseController 拡張のどちらがよいか、実装着手時に再検討
- **メタ判断結果による進行中 Pulse cancellation の具体経路**: §6.2 で示した方向性を実装着手時に詳細詰める

---

## 12. 議論メモ (起草過程の決定事項)

本 Intent は 2026-05-10 のセッションで起草された。決定の経緯:

- **「メタ判断 = 応答」案は不採用**: メタ判断と応答は出力構造・永続化経路・7 層ストレージ分類の役割が違うため統合せず、「メタ判断スキップ + 直接応答」を素直に作る方針 (まはー判断)
- **直接経路 / 熟慮経路の 2 経路分け**: 「メタ判断要否」をハードコードマップで事前定義。動的判定は不要 (まはー判断)
- **メタ判断の並列実行は将来余地ではなく必須**: alert 発生時の中断 / 継続判断のため、メタ判断は他 Pulse と並列に動く必要がある (まはー判断)
- **`invoke_main_line` ハードコード廃止 + activate しなかったら応答しない**: 1b で応答なし問題が頻発するなら 1b を直接経路に動かす (まはー判断 Q2) — ※ 2026-07-07 に実際に無応答の実害が出たため改訂済み (§4.2 Q2 改訂)
- **スケジュール ↔ Track 紐付けは別 Intent**: 本 Intent では概念のみ言及 (まはー判断 Q2 解 c)
- **ファイル名 `pulse_dispatch.md`**: Pulse 起動の入り口を一本化する話、というのが核心 (まはー判断 Q3)
- **メタ判断結果の cancel 経路 (§6.2)**: PulseController が `_notify_status_change` observer 経由で current pulse を cancel する形に確定 (まはー判断、v0.2)
- **メタ判断 PulseController 統合 (§6.3)**: 案 A (PulseController 経由統一) で確定 (まはー判断、v0.2)
