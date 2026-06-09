# Memory Weave snapshot が「記憶の整理」で更新されない

**ステータス**: 解決済み (2026-06-05)

## 現象

フロントエンドの「記憶の整理」(Metabolism) を実行しても、コンテキストプレビューの Memory Weave — Memopedia セクションが更新されない。サーバー再起動しても前回の snapshot が `line_head_snapshot` テーブルから復元され、古い結果が表示され続ける。

## 原因

`organize-memory` API (`api/routes/people/config.py`) が `_generate_chronicle()` で Chronicle を生成した後、`DynamicStateManager.on_metabolism()` を呼んでいなかった。

自動 Metabolism (`_run_metabolism`) では line 1908 で `DynamicStateManager.on_metabolism()` → `dispatch_event(ctx, EventType.METABOLISM)` → `capture_all()` と繋がり全 Section の snapshot が再構築されるが、手動 API はこの経路を通っていなかった。

## 修正

`organize-memory` API の Chronicle 生成後に `DynamicStateManager.on_metabolism(persona, manager)` 呼び出しを追加。

## 発見経緯

2026-06-04: `_get_memopedia_context` の `non_root` フィルタバグ (root 除外で children への再帰パスが消失) を修正した際、修正がコンテキストプレビューに反映されないことから発覚。`non_root` バグ自体は `e1866ef` (Fragment 化 commit) で混入していた。
