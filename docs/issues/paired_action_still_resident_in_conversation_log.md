# 判断の状況文 (paired_action) が会話ログの住人のまま残っている

**ステータス: 未着手** (2026-08-18 起票 — W14 知覚レンダリングの設計時に、experience_structure §7 の残件として切り出した)

## 何が残っているか

[experience_structure.md](../intent/experience_structure.md) §7 は「判断の状況文 (paired_action) のような行政記録は実行台帳の管轄に寄せ、会話ログの住人から外す方向 (W1 で finalize を触る際に検討)」と書いた。W1 が実際にやったのは**保存する状況文の痩身**まで:

- `saiverse/judgment_points.py:1165-1182` — LLM に渡す situation_text は原本全文を埋め込み、保存用の `paired_situation_text` は episode 参照 + 読み口の一行に留めるコールローカル注入 (W1 Chunk C D9)。
- 展開機構そのものは残っている: `saiverse_memory/adapter.py:1868-1887` の `_expand_paired_action_payloads` が全取得口 (recent / by_count / anchor 版) で無条件に走り、`paired_action_text` を持つ行の直前に action メッセージを合成挿入する。
- `paired_action_text` の書き手は判断点専用ではない: `sea/runtime.py` (`_store_memory`)、`sea/runtime_llm.py`、`sea/sluice.py` (旧 gold_panning) からも書かれる。

つまり「実行台帳の管轄に寄せる」は未着手で、判断の行政記録は依然 SAIMemory messages に住み、展開経由で提示コンテキストに載り続ける。

## なぜ W14 から分離したか

W14 (知覚レンダリング) の芯は「知覚 (外界→本人) を台帳正準+翻訳提示にする」こと。paired_action は知覚ではなく実行の行政記録で、展開機構・判断点・スルースにまたがる別の手術になる。W1 の痩身で当面の滞留実害 (2026-07-18 実測の状況文残存) は緩和済みなので、急がず独立に扱う。

## 直す方向 (未裁定)

実行台帳 (execution_ledger) 側に判断記録の置き場を作り、messages 側の `paired_action_text` 展開を段階的に畳む。着手時に experience_structure §7 / execution_ledger.md と突き合わせて設計する。

## 関連

- [experience_structure.md](../intent/experience_structure.md) §7 / [perception_buffer.md](../intent/perception_buffer.md) §10.6
- [execution_ledger.md](../intent/execution_ledger.md)
