# Memory Weave snapshot が「記憶の整理」で更新されない

## 現象

フロントエンドの「記憶の整理」(Metabolism) を実行しても、コンテキストプレビューの Memory Weave — Memopedia セクションが更新されない。サーバー再起動しても前回の snapshot が `line_head_snapshot` テーブルから復元され、古い結果が表示され続ける。

## 期待される動作

Metabolism 実行時に `EventType.METABOLISM` が `pipeline.dispatch_event` に届き、`capture_all` で全 Section (memory_weave 含む) の snapshot が再構築される。

## 調査ポイント

1. **フロントエンドの「記憶の整理」が head pipeline の METABOLISM イベントを発火しているか**: `sea/runtime.py` の Metabolism 処理 (`_run_metabolism`) から `dispatch_event(ctx, EventType.METABOLISM)` が呼ばれる経路があるか確認
2. **snapshot store からの load と capture_all の優先順位**: `integration.py:ensure_snapshot` で store から load 成功すると capture_all をスキップする設計。METABOLISM 後に store が更新されていなければ次回起動でも古い snapshot が復元される
3. **memory_weave Section の `refresh_on_events` が空**: 現状 `refresh_on=[]` で登録されているため、イベント駆動の refresh 対象にならない。METABOLISM は `capture_all` を走らせるので本来は問題ないはずだが、そもそも METABOLISM イベントが pipeline に届いていない可能性

## 暫定回避策

`line_head_snapshot` テーブルから該当ペルソナのレコードを DELETE してサーバー再起動すると、次回 `ensure_snapshot` で store が空 → `capture_all` が走る。

```sql
DELETE FROM line_head_snapshot WHERE persona_id = 'air_city_a';
```

## 発見経緯

2026-06-04: `_get_memopedia_context` の `non_root` フィルタバグ (root 除外で children への再帰パスが消失) を修正した際、修正がコンテキストプレビューに反映されないことから発覚。`non_root` バグ自体は `e1866ef` (Fragment 化 commit) で混入していた。
