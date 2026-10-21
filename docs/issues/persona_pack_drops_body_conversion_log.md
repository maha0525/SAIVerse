# ペルソナまるごとパックの取り込みで、v0.2.x の本文変換の実行記録が運ばれない

- **状態**: 未解決 (2026-10-21 起票。実害は小さい — 移住先で昔の変換を取り消せなくなるだけ)
- **関連**: `docs/intent/persona_pack.md` / `docs/intent/memopedia_body_to_fragment.md` /
  `sai_memory/memopedia/body_to_fragment.py`

## 現象

v0.2.x 時代の Memopedia 本文を Fragment へ移した「変換の実行記録」(memory.db の
`memopedia_body_conversion` 表。どの実行が何を作ったかを持ち、変換の取り消しだけが読む) は、
パックには入るが、取り込みで捨てられる。新しく作られたペルソナの memory.db にこの表が無く、
取り込みは「無い表の行はスキップ」と扱うため。

実測 (2026-10-21、エリスのパックをテスト環境へ迎えたとき): 警告
`memory.db has no table 'memopedia_body_conversion'; 4185 row(s) skipped`。
変換の**結果** (ページ本文と Fragment) はページの行として全件無傷で運ばれており、
全 1,320 ページの中身が本番と一致することは確認済み。

## 利用者への影響

移住先では、v0.2.x から引き継いだ本文変換を「取り消し」で巻き戻せない。変換済みの
記憶そのものは完全に運ばれるので、普段の想起・表示には影響しない。

## 方向の候補 (未裁定)

- 取り込みで表が無ければ `init_body_conversion_table` で作ってから入れる (数行の修正)
- 仕様として「運ばない」に倒し、intent の既知の限界に明記する
