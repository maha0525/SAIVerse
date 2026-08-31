# Phase 1 — 基盤刷新

**親**: [../README.md](../README.md)
**ステータス**: ✅ 完了
**旧称**: Phase 0 (handoff 解消) + Phase 1.3 (scope 機構)

---

## 目的

handoff 3 経路問題の解消と、7 層ストレージモデルを支えるデータモデル拡張。本 Phase が以降すべての Phase の前提。

旧 Intent B v0.11 で明文化された P0-1〜P0-7 タスクと、Phase 1.3 (scope='discardable'/'committed' 機構) を統合する。

---

## 背景: handoff 3 経路問題

`docs/intent/handoff_track_context_management.md` で報告された「Phase C-3b 実装後の動作確認で観察された多重記録問題」。Spell loop / `_emit_say` / action 文の保存先がバラバラで、ハードコードされた `tags=["conversation"]` がノードの `memorize.tags` 設定を無視していた。

### 経路 A: Spell loop 内 memorize (`sea/runtime_llm.py:430-442`)

**問題**: ハードコードで `tags=["conversation"]` 固定 → ノードの `memorize.tags` 設定を無視 + 保存先が固定

**解決**: 呼び出し元ライン (line_role, scope) から動的に保存先を決定する。`tags` ハードコードは廃止、ノードの `memorize.tags` を尊重する。

### 経路 B: spell loop 終了後 `_emit_say` (`sea/runtime_llm.py:980`)

**問題**: `speak: false` の LLM ノードで spell が動いた場合も `_emit_say` が走り、ペルソナの「発話」として外向きに記録 + Building history に流入

**解決**: `speak: false` のノードでは `_emit_say` 全体を **skip**。

### 経路 C: LLM ノード本体 memorize (`sea/runtime_llm.py:1717-1768`)

**問題**: `prompt` (action template) を user role で SAIMemory に保存 → ユーザー発話と混ざる

**解決**: `prompt` の単独保存をやめ、応答メッセージの `paired_action_text` カラムに紐付ける。

---

## タスク

| ID | タスク | 状態 | 実装場所 | 旧称 |
|----|--------|------|---------|------|
| 1-1 | `PulseContext._line_stack` / `LineFrame` / push/pop/current_line | ✅ | `sea/pulse_context.py:56-224` | P0-1 |
| 1-2 | `messages` テーブル拡張 (line_role / line_id / scope / paired_action_text) | ✅ | `sai_memory/memory/storage.py:101-129` | P0-2、Phase 1.3 |
| 1-3 | `meta_judgment_log` / `track_local_logs` テーブル新設 | ✅ | `database/models.py:512-580` | P0-3 |
| 1-4 | Spell loop の `tags=["conversation"]` 固定廃止 | ✅ | `sea/runtime_llm.py:434-465` | P0-4 |
| 1-5 | `speak: false` 時に `_emit_say` skip | ✅ | `sea/runtime_llm.py:977-983` | P0-5 |
| 1-6 | action 文ペア保存 (`paired_action_text` 利用) | ✅ | `sea/runtime_llm.py:1733-1766` | P0-6 |
| 1-7 | `include_internal` フィルタを line_role / scope ベースへ移行 | ✅ | (Phase 1.4 で DEPRECATED 化済み、削除は Phase 3 完了後) | P0-7 |
| 1-8 | scope 昇格 SQL UPDATE 機構 (`discardable` → `committed`) | ✅ | (Phase 1.3 マージ済み) | Phase 1.3 |

---

## 完了の判定基準

- [x] `messages` テーブルに新カラム (line_role / line_id / scope / paired_action_text) が追加され、既存行は `scope='committed'` のデフォルト値で互換維持
- [x] `meta_judgment_log` / `track_local_logs` テーブルが作成され、空のまま起動できる (運用は Phase 2 で実装済み: handoff_2026-04-30 Part 2)
- [x] Spell loop が `memorize.tags` を尊重する (回路 A 修正の検証)
- [x] `speak: false` のノードが Building history に発話を流さない (回路 B 修正の検証)
- [x] action 文が user メッセージとして単独保存されない (回路 C 修正の検証)
- [x] `discardable` scope のメッセージが `committed` に昇格できる SQL UPDATE が動く

すべて完了済み。

---

## Phase 2 以降への前提条件

- ライン階層機構 (`PulseContext._line_stack`) が動いていること → Phase 2 の Track Handler が呼ぶ
- `messages` の 4 カラムが揃っていること → Phase 2 以降のメッセージ保存ロジックが利用
- `meta_judgment_log` / `track_local_logs` テーブルが存在すること → Phase 3 のメタ判断 Playbook が書き込み

---

## 関連ドキュメント

- [../03_data_model.md](../03_data_model.md) — テーブルスキーマの詳細
- [../02_mechanics.md](../02_mechanics.md) — メタ判断の commit/discard 機構
- `handoff_track_context_management.md` — 観察記録の元ドキュメント
