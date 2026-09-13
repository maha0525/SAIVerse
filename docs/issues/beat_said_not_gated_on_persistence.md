# 「建物へ喋った」の印が、書き込みの成否を見ずに立つ

**発見**: 2026-09-14 (中断通告の追加への代行レビュー二巡目、指摘 3)
**状態**: 未解決
**深刻度**: P3 — 発火には「建物への書き込みが DB で失敗し、かつ直後に Beat が例外で死ぬ」の重なりが要る

## 症状

sea/runtime_llm.py で、建物へ本文を直接書く経路 (`_emit_say_and_capture` を呼ぶ側) の複数箇所 (2026-09-14 時点で 4580 / 4629 / 5484 / 5647 付近) が、書き込みの成否を見ずに `beat_said = True` を立てる。書けたかどうかの判定は戻り値の `message_id` の有無 (DB 採番) で行うのが repo の裁定 (builtin_data/tools/tell.py) で、対照的に `_emit_beat_segments` 経由の枝は戻り値を見ている。

`beat_said` は「建物には本文があるのに本人の記憶に無い」形を防ぐ補填 (`_backfill_memory_on_beat_death`) の発火条件なので、書き込みが失敗した回にこの印が立ったまま Beat が例外で死ぬと、**建物に存在しない本文が本人の記憶にだけ書かれる** — 補填が防ごうとした食い違いの裏返しが起きる。

## 直し方 (案)

各呼び出し箇所で戻り値を受け、`isinstance(bmsg, dict) and bmsg.get("message_id")` のときだけ `beat_said` を立てる。中断通告の追加 (2026-09-14) で同じ判定を `_partial_landed_bid` に入れた実装がその場 (5484 の 3 行下) にあるので、形はそれに揃える。4 箇所それぞれの文脈 (ループ側の別判定 4758 / 5239 との棲み分け) を確認しながら一括で。

## 関連

- [server_cut_stream_writes_no_interruption_notice.md](server_cut_stream_writes_no_interruption_notice.md) (同じ判定を通告側に入れた実装)
- [unfinalized_placeholder_on_clean_exit.md](unfinalized_placeholder_on_clean_exit.md) (同じレビューで出た隣の残債)
