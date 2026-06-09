# Emitter の SAIMemory 責務分離

## 現状

`sea/runtime_emitters.py` に 4 つの emit メソッドがある:

| メソッド | Building 履歴 | SAIMemory | voice/TTS フック |
|---------|:---:|:---:|:---:|
| `emit_speak()` | o | o (add_to_persona_only) | o |
| `emit_say()` | o | x | o |
| `emit_speak_start()` | o (placeholder) | x | o |
| `emit_speak_finalize()` | o (確定) | x (sync_to_memory=False) | o |

SAIMemory への書き込みは `_store_memory()` (sea/runtime.py) が唯一の実経路。`emit_speak` の SAIMemory 書き込みはプロダクション経路から直接呼ばれておらず実質 dead code だが、`emit_speak_finalize` が内部で `emit_speak` の `add_to_persona_only` を使っている（`sync_to_memory=False` で SAIMemory は回避）。

## 問題

- 名前から「どれが SAIMemory に書くか」が読み取れない
- `emit_speak` に SAIMemory 関連の修正を入れても効かない（実際に 2026-06-08 に踏んだ）
- `audience` メタデータの付与を `emit_speak` に入れたが、実際の SAIMemory 書き込み経路は `_store_memory` だったため無意味だった

## 改善案

- emitter の責務を「Building 履歴 + voice/TTS フック専任」に明確化
- `emit_speak` の `add_to_persona_only(sync_to_memory=True)` を `sync_to_memory=False` に変更、または log.json 書き込みだけに限定
- `emit_speak_finalize` が `emit_speak` を間接利用している箇所をリファクタし、Building 履歴 + log.json の書き込みを直接行う形に整理
- 可能なら `emit_speak` を廃止し、`emit_say` / `emit_speak_start` / `emit_speak_finalize` の 3 メソッドに統一

## 優先度

低。`_store_memory` が唯一の SAIMemory 経路だと把握していれば実害はない。次のリリースサイクルで整理。
