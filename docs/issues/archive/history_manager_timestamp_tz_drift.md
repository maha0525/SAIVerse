# Issue: history_manager 経由メッセージの created_at が system TZ 解釈で 9 時間ずれる

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-10
**関連**: `persona/history_manager.py:_prepare_message`, `saiverse_memory/adapter.py:_timestamp_to_epoch`, pulse_dispatch.md §9.2 (本問題が顕在化した経緯)

## 背景

メモリーモーダルで `_inject_track_context` (Track 切替通知) のメッセージが、書かれた現実時刻より **9 時間先** で表示される。

実機確認 (sophie_city_a, 2026-05-10):

| 経路 | epoch | UTC 解釈 | JST 解釈 |
|---|---|---|---|
| `_store_memory` 経由 (例: meta_judgment ターン) | 1778385675 | 2026-05-10 04:01:15 | 2026-05-10 13:01:15 ← 実時刻 |
| `history_manager._prepare_message` 経由 (例: Track 切替通知) | 1778418075 | 2026-05-10 13:01:15 | 2026-05-10 22:01:15 ← 9時間先 |

差分は exactly 32400 秒 = 9 時間 = JST と UTC のオフセット。

## 原因

`history_manager._prepare_message` が timestamp を `datetime.now().isoformat()` (= naive local ISO 文字列) で詰めている。

`saiverse_memory/adapter.py:_timestamp_to_epoch` が:

```python
dt = datetime.fromisoformat(value)  # naive datetime
return int(dt.timestamp())
```

で epoch に変換するが、`naive_dt.timestamp()` は **Python プロセスの system TZ** で naive を解釈する。プロセスの system TZ が UTC として認識されている (Windows + miniconda env 等の特定の状況で起こる) と、JST 13:01:15 の naive を「UTC 13:01:15」と誤解釈し、epoch が 9 時間先になる。

`_store_memory` 経路は `int(time.time())` を直接 epoch として保存するので影響を受けない。これが「特定経路だけズレる」原因。

## 影響範囲

`history_manager` 経由で書かれる全メッセージの created_at が ±9 時間 (system TZ 次第) ずれる可能性がある。

具体的に影響するメッセージ:

- `_inject_track_context` 経由の Track 切替通知
- `auto_ingest_building_messages` 経由の他ペルソナ発話・ユーザー発話 (の history_manager 側に書かれる分)
- `add_message` / `add_to_persona_only` / `add_to_building_only` を経由する全エントリ

`_store_memory` 経由 (meta_judgment / emit_speak / emit_say / emit_think 等) は影響なし。

## 解決案候補

**案 A**: `_prepare_message` の timestamp を TZ-aware UTC ISO に変更

```python
# Before
new_msg["timestamp"] = datetime.now().isoformat()
# After
new_msg["timestamp"] = datetime.now(timezone.utc).isoformat()
```

`fromisoformat` が tz-aware datetime を生成 → `.timestamp()` は system TZ に依存せず正しく UTC epoch に変換される。最小変更。

**案 B**: `_prepare_message` の timestamp に直接 epoch を入れる

```python
new_msg["timestamp"] = int(time.time())
```

`_timestamp_to_epoch` が int も受け取れるよう改修も必要。フォーマット統一の議論あり。

**案 C**: プロセス起動時に明示的に `os.environ["TZ"]` を設定する

副作用が広いので非推奨。

私の推奨: **案 A**。最小変更で根本解決。

## 関連リソース

- `persona/history_manager.py:_prepare_message`
- `saiverse_memory/adapter.py:_timestamp_to_epoch`
- pulse_dispatch.md §9.2 段階 2 で `_inject_track_context` の呼び出し経路が history_manager 経由に統一されたため、本問題が顕在化したケースが増えた (修正前は別経路もあった可能性)
- 実機確認時の DB 抜粋 (sophie_city_a/memory.db, 2026-05-10):
  ```
  epoch=1778385675 (UTC 04:01:15) line_role=meta_judgment ← _store_memory 経由
  epoch=1778418075 (UTC 13:01:15) Track 切替通知 ← history_manager 経由 (9時間先)
  ```

## ログ

- 2026-05-10: pulse_dispatch.md §9.5 段階 5 完了後の実機テスト中に発見、起票
- 2026-06-11: 同根の別経路を発見・修正。`builtin_data/tools/meta_judgment_finalize.py` が
  メタ判断メッセージを `datetime.utcnow().isoformat()` (= naive UTC ISO) で書いており、
  `_timestamp_to_epoch` がプロセスの system TZ (今回は JST) で naive を解釈して
  created_at が 9h **手前** にずれていた (ruler_region_mistvale_city_a_city_a で実機確認、
  stored=1781148122 vs 正=1781180522、差 32400s)。案 A に倣い tz-aware UTC
  (`datetime.now(timezone.utc).isoformat()`) に修正。**`history_manager._prepare_message`
  本体と他の `datetime.now().isoformat()` プロデューサ群は未修正のまま** (本 issue は継続)。
  根治には naive を渡すプロデューサを全て tz-aware に揃えるか、`_timestamp_to_epoch` を
  naive=UTC 固定にした上で local naive プロデューサを一掃する必要がある。
