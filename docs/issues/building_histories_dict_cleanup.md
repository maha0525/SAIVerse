# Issue: Phase 2+3 後の `building_histories` dict 残存参照整理

**ステータス**: 🔲 未着手
**優先度**: low (= 動作影響なし、 dead/no-op コード整理)
**作成日**: 2026-05-20
**関連**:
- `docs/intent/building_memory_unified.md`
- `docs/handoff_2026-05-20_building_memory_db.md`
- Phase 2+3 統合作業 (uncommitted)

## 背景

Phase 2+3 で source of truth が `building_messages` テーブル (DB) に移管され、 `manager.building_histories` (= `Dict[str, List[Dict]]`) は **空 dict 初期化のみ**に縮退した (`manager/initialization.py:163`)。 `_save_building_histories` も no-op 化済 (`manager/history.py:189-191`)。

ただし `building_histories` への **書き込み / 参照箇所が複数残存**しており、 動作影響はないが「Phase 2+3 後の実装と矛盾するコード」 として整理対象。

## 残存箇所 (grep 確認済)

| 場所 | 内容 | 整理方針 |
|---|---|---|
| `saiverse/saiverse_manager.py:799` | `_append_building_history_note` 内の `building_histories.setdefault(bid, []).append(...)` | **本 issue とは別件で修正済**: Layer 1 経由化 (= `respeak_split_message_unification.md` 修正と同時期の commit) |
| `saiverse/saiverse_manager.py:1764` | `_reload_buildings` で削除済 building の `building_histories.pop(bid, None)` | 空 dict から pop するだけなので no-op。 削除可 |
| `saiverse/saiverse_manager.py:1781-1782` | `_reload_buildings` で新規 building の `building_histories[bid] = []` 初期化 | 空 dict なので不要。 削除可 |
| `manager/persona.py:399` | persona 新規作成時の `building_histories[new_building_id] = []` | 同上、 削除可 |
| `manager/blueprints.py:254` | blueprint 経由 persona 作成時の同様の初期化 | 同上、 削除可 |
| `api/routes/chat.py:111` | debug log `manager.building_histories.keys()` で空 dict を出力 | 削除 or 「DB ベースの履歴数」 の debug log に置換 |
| `api/routes/system.py:257, 310` | quarantine restore/reset 経路の `manager.building_histories[bid] = data` | **別 issue**: `quarantine_path_dead_code_removal.md` で扱う |
| `persona/mixins/history.py:176` | `_save_conscious_log` メソッド名 (= 中身は `persona_pulse_cursor` DB 保存に刷新済) | rename 候補 (= `_save_pulse_cursors` 等)。 呼び出し元 `api/routes/system.py:176` も追従要 |
| `manager/history.py:189-191` | `_save_building_histories` no-op 関数 | 互換のため残しているが、 呼び出し元が `_append_building_history_note` だけになれば削除可 |
| `manager/history.py:154` | コメントに「`conscious_log.json` に save」 と旧記述 | コメント更新 |
| `saiverse/saiverse_manager.py:1773` | `building_memory_paths` の `log.json` パス算出が残存 | `persona/history_manager.py:36` に「legacy log.json (archive) のパス参照のために残す」 とあり、 archive 読み出しでは現役。 残す or 削除は要確認 |

## 整理方針

1. 1 つの commit で `building_histories` dict 関連の write/read 箇所をまとめて削除
2. `_save_building_histories` 自体も削除 (= 呼び出し元解消後)
3. `_save_conscious_log` を `_save_pulse_cursors` 等に rename (呼び出し元も追従)
4. コメントの「conscious_log.json に save」 等の古い記述を更新

## 検証

- `pytest` 950 件が引き続き PASS
- 起動 → persona 新規作成 → building 新規作成 → 削除 の一連で例外なし
- `_reload_buildings` (= UI からの building 編集) で例外なし

## ログ

- 2026-05-20: 害なし整理対象として issue 化 (= Phase 2+3 commit 取り後の余裕あるタイミングで対処)
