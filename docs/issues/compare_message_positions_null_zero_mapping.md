# compare_message_positions が NULL created_at を 0 に写像する (正典順序の二枚目)

**発見**: 2026-08-31 (被覆補修 §16 の Codex 消し込み中、実装エージェントの同族走査)
**状態**: 🔲 未解決 — 影響先が §14 の anchor 前進系のため、v0.3 リリース前には触らない (まはー裁定を経ず既存機構の挙動を変えない)
**深刻度**: P3 — 実害が出るのは「created_at が NULL の行と 0 (1970 epoch) の行が同一 DB に混在」する場合のみ

## 事実

メッセージの正典順序の正は「NULL created_at は全ての実時刻より前」
(`sai_memory/memory/storage.py` の `_canonical_before_clause` 族)。
被覆補修 (§16) はこの共有述語に一本化済みだが、
`sai_memory/arasuji/storage.py` の `compare_message_positions` は NULL→0 の
写像で比較しており、規則の二枚目として残っている。使用者は §14 の
anchor 前進系 (冷えた起点の保守)。

## やること (v0.3 後)

- 使用箇所を洗い、共有述語 (`canonical_position_key`) へ寄せる。
- NULL 行と ts=0 行の混在 DB での回帰テストを §16 のテスト
  (`tests/test_coverage_repair.py` の canonical 系) と同じ形で足す。

## 関連

- `tests/test_coverage_repair.py` — 一本化済み側の回帰
- [intent: あらすじのレベル制](../intent/arasuji_levels.md) §14 / §16
