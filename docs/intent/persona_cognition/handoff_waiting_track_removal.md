# Handoff: 「待ち」Track 廃止作業

**親**: [README.md](README.md)
**ステータス**: ✅ 完了 (2026-05-11)
**経緯**: [revisions.md](revisions.md) v0.31 (2026-05-09) で「待ちを Track 状態として持たない」方針に変更
**関連**: [pulse_dispatch.md](pulse_dispatch.md) §8 (時間差ツール基盤、Phase 5 で代替提供予定)

---

## 1. 背景

Phase 5 構想の **時間差ツール基盤** (call_id 採番 / イベントメッセージ配送 / 不在 Track への alert 通知) と「待ち (waiting) Track 機構」が役割重複していたため、v0.31 で Track 状態 / 種別から `waiting` を抜く方針に変更。代わりに時間差ツール基盤で同等機能 (= 何かを待つ Pulse の中断 → 完了通知時に再開) を実現する。

しかし Phase 3 段階 4-D 完了後も `STATUS_WAITING` 関連コードは大量に残っている。本作業ではこれらを完全削除する。

幸い、`builtin_data/tools/` に `track_wait.py` / `track_resume_from_wait.py` のような **ペルソナ向けスペルは元から作られていない** ため、ペルソナの行動経路を壊さずに削除できる。実用運用への影響は無い。

---

## 2. 削除対象

### 2.1 TrackManager (`saiverse/track_manager.py`)

- 定数 `STATUS_WAITING` (line 36)
- ALL_STATUSES / TERMINAL_STATUSES 等の集合から削除 (line 42, 48)
- メソッド (それぞれ削除):
  - `wait(track_id, waiting_for, waiting_timeout_at, ..., pulse_id=None)` (line 491-)
  - `_schedule_waiting_timeout(track)` (line 719-)
  - `_cancel_waiting_timeout(track_id)` (line 745-)
  - `_handle_waiting_timeout(track_id, persona_id, waiting_for)` (line 752-)
  - `resume_from_wait(track_id, ..., pulse_id=None)` (line 805 周辺)
- `_set_status` の `allowed_from` 引数で waiting を含む箇所すべて
- `pause` / `complete` / `abort` で waiting → terminal 遷移を許す箇所
- waiting 関連の clear_waiting_fields ロジック

### 2.2 DB Schema (`database/models.py`)

- `action_tracks.waiting_for` カラム削除
- `action_tracks.waiting_timeout_at` カラム削除
- マイグレーション (`database/migrate.py` に追加):
  - 既存 STATUS_WAITING の Track の扱い: **pending に降ろす** が推奨 (作業中だった意思を残す)
  - waiting_for / waiting_timeout_at の値は破棄
  - 自動バックアップは migrate.py の既存仕組みでカバー

### 2.3 Playbook

- `builtin_data/playbooks/public/track_waiting.json` 削除
- DB の `playbooks` テーブルに残っている可能性 → `scripts/import_all_playbooks.py` か手動 SQL で削除

### 2.4 InternalAlertPoller (`saiverse/internal_alert_poller.py`)

- `_ELIGIBLE_STATUSES` から `"waiting"` を削除 (line 37)

### 2.5 テスト (`tests/test_track_manager.py`)

waiting 関連テストを削除 (一部抜粋):

- `test_wait_with_event_scheduler_pushes_timeout`
- `test_wait_no_event_scheduler_skips_push`
- `test_resume_from_wait_cancels_timeout_schedule`
- `test_waiting_timeout_fires_alert`
- `test_waiting_timeout_no_fire_after_resume`
- `test_resume_from_wait_invalid_mode`
- `test_persistent_track_can_wait`
- `test_persistent_track_cannot_abort_from_wait`

注意: 一部のテストは waiting 直接関連でないように見えても fixture 経由で wait に依存している可能性 (例: `test_complete_sets_timestamp` などが既に flaky になっている場合)。要精査。

### 2.6 ドキュメント

- `01_concepts.md` / `02_mechanics.md` / `03_data_model.md` / `04_handlers.md` の waiting 言及箇所を削除
- `revisions.md` には経緯として残す (削除しない)

---

## 3. 注意点

### 3.1 DB マイグレーション
- 既存運用 DB に `STATUS_WAITING` 状態の Track が居る可能性 (現実的にはほぼ無いが念のため)
- マイグレーション時の方針: **pending に降ろす** (= 自然消滅、作業意図は残る)
- waiting_for / waiting_timeout_at カラムは drop

### 3.2 wait_response との混同を避ける
- `wait_response` は別機構 (handoff_2026-05-09 で実装、user_conversation Track の自動 pause タイマー)
- 本作業で削除するのは「`STATUS_WAITING` (時間差ツール待ち)」であり、`wait_response_timeout` は **残す**
- track_manager.py の `_schedule_wait_response_timeout` / `_cancel_wait_response_timeout` / `_handle_wait_response_timeout` は **削除対象外**

### 3.3 Phase 5 時間差ツール基盤との関係
- waiting 廃止後、Kitchen / dispatch_persona / X 投稿等の完了通知は別経路 (時間差ツール基盤) で扱う
- 詳細: [pulse_dispatch.md](pulse_dispatch.md) §8 の経路 δ
- この基盤無しに waiting を廃止しても、現状機能の劣化はない (元から運用されていなかったため)

---

## 4. 推奨作業順序

1. **DB マイグレーション** (`database/migrate.py` に追加): waiting → pending 降ろし + カラム drop
2. **TrackManager の wait メソッド群削除**: `wait()`, `_schedule_waiting_timeout()`, `_cancel_waiting_timeout()`, `_handle_waiting_timeout()`, `resume_from_wait()`
3. **状態定数・集合の整理**: `STATUS_WAITING` 削除 + 関連集合から除外
4. **`database/models.py` カラム削除**: waiting_for / waiting_timeout_at
5. **`internal_alert_poller.py` 更新**: `_ELIGIBLE_STATUSES` から waiting 除外
6. **Playbook 削除**: `track_waiting.json` ファイル + DB レコード
7. **テスト整理**: waiting 関連を全削除、残テストが pass するか確認
8. **ドキュメント整理**: 言及箇所を削除 (revisions.md は残す)
9. **ruff check + pytest 全体**: 回帰確認

---

## 5. 完了条件

- `grep -rn "STATUS_WAITING\|waiting_for\|waiting_timeout_at" saiverse/ database/ builtin_data/ tests/` で 0 件
- pytest 全体 pass (waiting 関連テスト削除済)
- 既存運用 DB マイグレーション完了 (まはー手動 or 起動時自動)
- pulse_dispatch.md / phase_5_autonomy.md などで waiting 言及が「歴史的経緯」として残るのみ

---

## 6. 関連リソース

- [revisions.md](revisions.md) v0.31 (2026-05-09) — 廃止方針の決定
- [pulse_dispatch.md](pulse_dispatch.md) §8 — 時間差ツール基盤 (Phase 5 で代替提供)
- [phase_5_autonomy.md](phases/phase_5_autonomy.md) §「時間差ツール基盤」 — 後継機構
- `saiverse/track_manager.py` — 削除対象コード集中地
- `database/models.py:ActionTrack` — DB カラム
