# Intent: デバッグコントローラー

**親**: [README.md](README.md)
**ステータス**: **v0.4 (2026-08-23)**。当初の目的だった「自律稼働のタイマーを無視した手動ステップ実行」は、対象のタイマーと判断機構がすべて退役したため消滅した。残っているのは Embedding の一括生成 1 つだけで、このパネルは**ペルソナ設定画面から手で叩ける保守操作の置き場**になっている。
**関連**: [../autonomous_behavior_v3.md](../autonomous_behavior_v3.md), [../memopedia_body_to_fragment.md](../memopedia_body_to_fragment.md), `api/routes/people/debug.py`, `frontend/src/components/DebugPanel.tsx`

## いま何ができるか

| 操作 | 動作 | 実体 |
|---|---|---|
| Embedding 一括生成 | Chronicle / Memopedia page / Fragment の未生成 embedding をまとめて作る | `POST /people/{persona_id}/debug/generate-embeddings` |

同じ `debug.py` には Memopedia の本文 → Fragment 変換 API (`.../debug/memopedia-conversion/*`) も同居しているが、あれは別の画面から呼ばれる別の道具で、設計は [memopedia_body_to_fragment.md](../memopedia_body_to_fragment.md) が持つ。

## 退役した操作 (と、その理由)

| 操作 | 退役 | 理由 |
|---|---|---|
| メタ判断を 1 回 (`fire-meta-judgment`) | 2026-08-14 | v1 メタ判断の退役 ([../track_retirement.md](../track_retirement.md) §7.4)。押しても必ず失敗する口になっていた。 |
| sub_line Pulse を 1 回 (`fire-subline-pulse`) / SubLine タイマートグル | 2026-07-13 → 2026-08-14 | 自律行動 v2 で旧 SubLineScheduler ごと廃止 (life.md v0.5 §9.2-2 改修B)。UI を先に外し、no-op で残っていたバックエンドを後から削除。 |
| 会話を切り上げ (`wrap-up-conversation`) | **2026-08-23** | 説明文の「会話終了判断を撃つ」の会話終了判断が v3 で退役した ([../autonomous_behavior_v3.md](../autonomous_behavior_v3.md) §8/§13.3)。会話は沈黙タイマーだけで閉じるので、手動で前倒しする意味が無い。 |
| Autonomy 切替 (`POST .../scheduler` の `autonomy`) | **2026-08-23** | v0.3 の止め具 (`saiverse/autonomy_wiring.py` の `AUTONOMOUS_DRIVING_SHIPPED = False`、[../autonomous_behavior_v3.md](../autonomous_behavior_v3.md) §11.1) で判断点・見張り・コマが発火しない。切り替えても効果が無い。 |
| 完全手動モード (`POST .../scheduler` の `manual_mode`) | **2026-08-23** | 同上。止めるべき自動発火が既にゼロ。 |
| タイマー状態の表示 (`GET .../scheduler`) | **2026-08-23** | 表示していた `autonomy_state` / `manual_mode` の両方が上の退役で意味を失った。 |

⚠ **`manager._debug_manual_mode_personas` は残っている**。完全手動モードを立てる唯一の口 (`POST .../scheduler`) が消えたので、この集合は常に空になる。読み手 (`saiverse/user_conversation.py`, `saiverse/execution_ledger_wiring.py`, スケジュール照合) の除外分岐は残したまま = 到達しない枝になっている。撤去は自律行動 v0.3 の止め具を外すときの判断とまとめて行う。

## 経緯 (以下は歴史的記録)

当初の設計意図と、退役した機構の設計は下に残す。現行の姿は上の 2 節が持つ。

### なぜ必要だったか

自律 Track の運用フローは 3 つのタイマーが並行する:
- SubLineScheduler: running autonomous Track に 30 秒間隔で sub_line Pulse
- AutonomyManager: 50 分間隔で idle 時のメタ判断 (periodic tick)
- wait_response timeout: 対話 Track が running の間、30 分沈黙で pause → メタ判断

UC-2「割り込みと復帰」のような往復シナリオを検証するとき、これらのタイマー待ち (30秒/50分/30分) が混ざると非決定論的で観察しづらい。タイマーを止めて手動でステップ実行できれば、検証が決定論的になる。

### 発火項目 (UI ボタン。すべて退役済み)

| ボタン | 動作 | 実体 |
|---|---|---|
| メタ判断を 1 回 | `manager.meta_layer.on_periodic_tick(persona_id, ctx)` を即発火 | `force` トグル付き |
| sub_line Pulse を 1 回 | 選択した running autonomous Track に `manager.pulse_dispatcher.dispatch_subline_poll(...)` | 30 秒間隔を無視 |
| 会話を切り上げ | running の wait_response Track を pause → メタ判断発火 | `TrackManager._handle_wait_response_timeout` 相当を即時 |

**`force` トグル**: OFF = 本番同様の抑止 (自律行動が OFF / running が wait_response 型なら skip。2026-07-14 以前は `ACTIVITY_STATE != Active` 判定)。ON = 抑止無視で強制発火。`on_periodic_tick` に `force: bool = False` 引数を足し、True のとき 2 つの抑止 (`meta_layer.py:379`, `391`) をスキップする。

### タイマー制御 (UI トグル。すべて退役済み)

| トグル | 対象 | 実装 |
|---|---|---|
| SubLineScheduler on/off | manager 全体で 1 本 | `subline_scheduler.start()/stop()` は既存。API から叩く経路 + 現在状態の取得を追加 |
| AutonomyManager on/off | per-persona | 既存 `/autonomy/start,stop` API |
| 完全手動モード | 上記 + wait_response timeout | manager にフラグを持ち、`TrackManager._schedule_wait_response_timeout` がフラグ ON 時は予約しない。既存予約は cancel |

完全手動モード ON のとき自動発火はゼロになり、上の 3 ボタンだけでペルソナを駆動する。

### API (起草時の案。現存するのは `generate-embeddings` だけ)

`/people/{persona_id}/debug/` 配下:
- `POST .../fire-meta-judgment` (body: `{force: bool}`) — 退役
- `POST .../fire-subline-pulse` (body: `{track_id: str}`) — 退役
- `POST .../wrap-up-conversation` — 退役
- `POST .../scheduler` (body: `{subline, autonomy, manual_mode}`) — 退役
- `GET .../scheduler` (現在のタイマー稼働状態) — 退役

### UI 置き場所

ペルソナ画面のデバッグセクション (操作対象が per-persona のため)。ここは現在も同じ。

### 守るべき点 (起草時。1〜3 は対象機構ごと退役)

1. **本番経路と分離**: `force` / 完全手動モードはデバッグ専用フラグ。本番のメタ判断・Pulse 起動経路の挙動を変えない (フラグ OFF 時は現状と完全一致)。
2. **新しい状態遷移を作らない**: 手動発火は既存の `on_periodic_tick` / `dispatch_subline_poll` / `_handle_wait_response_timeout` を呼ぶだけ。Track 状態遷移やメッセージ永続化の新経路を作らない。
3. **完全手動モードの解除でタイマーが正しく復帰**: モード OFF 時に SubLine/Autonomy/wait_response timeout が通常稼働に戻ること。

## ログ

- 2026-05-25: 起草。UC-2 検証の道具として設計確定 (まはー承認)。実装着手。
- 2026-07-13 (v0.2, life.md v0.5 §9.2-2 改修B): 自律行動 v2 で SubLineScheduler ごと廃止されたため、UI の「sub_line Pulse を 1 回」ボタンと「SubLineScheduler on/off」トグルを削除。バックエンドは互換のため no-op で残した。
- 2026-08-14 (v0.3, Track 撤廃 順序①): v1 メタ判断の退役 ([../track_retirement.md](../track_retirement.md) §7.4) に伴い、`wrap-up-conversation` を「pause + メタ判断」から「沈黙タイマーの即時発火」へ置き換え、**開いている会話があるときだけ**撃つようにした。あわせて no-op で残っていた `fire-meta-judgment` / `fire-subline-pulse` / `scheduler.subline` を削除。
- 2026-08-23 (v0.4): まはーの実機検証で「退役済みの操作が並んでいる」と指摘。裁定により「会話を切り上げ」「Autonomy 切替」「完全手動モード」の 3 つを UI と API から撤去し、Embedding 一括生成だけを残した。理由と、後に残った `_debug_manual_mode_personas` の扱いは上の §「退役した操作」を参照。
