# 意味検索の想起が、機構名義タグの除外を経路ごとに別の一覧で持っている

**発見**: 2026-08-29 (編纂材料の一本化への Codex レビュー指摘 #5。裏取りで「事実は成立、ただし当該変更の退行ではなく既存の不揃い」と判定して切り出し)
**状態**: 未解決 (方針の裁定待ち)
**深刻度**: P3 — 実害の報告はまだ無い。機構の定型文が「過去のあなたの記憶」として想起に混入しうる、という筋の悪さが問題

## 事実 (2026-08-29 実測)

意味検索で過去の行を掘り返す消費者が、機構名義タグの除外一覧をそれぞれ手元に持っていて、中身が揃っていない:

- `["handy_tool", "spell"]` — `saiverse_memory/adapter.py` (2 箇所) / `api/routes/people/recall.py` (2 箇所) / `builtin_data/tools/memory_search_brief.py` / `sai_memory/memory/recall.py:524`
- `["handy_tool"]` だけ — `sai_memory/memory/recall.py:571`
- `SEMANTIC_EXCLUDE_TAGS = ("handy_tool", "spell")` — `saiverse/recall_walk.py:103`

そして **`event_message` (システム通知) はどの経路も除外していない**。scene 切り出し・会話キーワード検索の共有フィルタ (`sai_memory/memory/storage.py` の `_conversation_exclusion`) が機構タグ 3 種 (`MECHANISM_TAGS`) を除外しているのと不揃い。

## なぜ今直さなかったか

2026-08-29 の編纂の裁定 (提示に立った行はすべて編纂の材料に入る) は**あらすじ生成の材料**の話で、意味検索の想起がシステム通知の生の行を「過去のあなたの記憶」として蘇らせてよいかは**別の問い**。想起はペルソナの生きた認知に直接効くので、リリース直前に裁定なしで挙動を変えない (機械的な統一に見えて、実は方針の変更)。

## 提案 (裁定待ち)

1. 除外一覧の**持ち方**は `MECHANISM_TAGS` (sai_memory/memory/storage.py) の共有一本に寄せる — 手元コピーは今後もズレ続ける。
2. その上で「意味検索の想起に `event_message` を含めるか」をまはーが裁定する。scene と会話検索は既に除外しており、揃える (除外する) のが自然に見えるが、通知を思い出せること (「あのとき部屋が変わった」) に価値がある可能性もある。
3. `recall.py:571` の `["handy_tool"]` だけの経路は、意図なのか取り残しなのかを実装から確認してから揃える。

## 関連

- [arasuji_levels.md](../intent/arasuji_levels.md) §7-1 (編纂側の裁定 2026-08-29) / `MECHANISM_TAGS`
- 同族の教訓: 除外一覧の手元コピーは「隣を忘れた型」の温床 (memory: feedback_apply_the_discipline_to_the_sibling)
