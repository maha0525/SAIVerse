# Issue: spell の judgment が子ライン出力より後に記録され時系列が逆転する

**ステータス**: 🟡 修正済み・実機検証待ち
**優先度**: high
**作成日**: 2026-05-25
**関連**: `sea/runtime_llm.py:_run_spell_loop`, `builtin_data/tools/run_playbook.py`, `docs/intent/persona_cognition/01_concepts.md` (不変条件「全部俺の経験 = 統一された時系列の記憶」)

## 背景

`run_playbook` 等で子ライン (sub-line) を起動する spell を含む LLM 応答で、**起動の意思決定 (judgment) が、それが起動した子ラインの出力より後の `created_at` で記録される**時系列逆転が起きていた。

まはーが Pulse タイムラインで観察: autonomous Track の sub_line Pulse (cb67c333) の最初のメッセージが web_research の `decide_topic` 出力 (research_topic JSON) で、それを起動した judgment 独白 (「新しい自律Trackが起動した。リサーチしよう」+ `/spell run_playbook name='web_research'`) は 24 msg の最後尾にあった。原因が結果より後に並ぶ = ペルソナの記憶の因果が逆転。

## 原因

`_run_spell_loop` の記録順序:
1. `assistant_content` (judgment) を確定 (≈L935) + LLM messages に append (≈L936)
2. **spell を asyncio.gather で実行** (≈L946) — `run_playbook` は子ラインを同期実行し、子ラインの各ノードが先に `_store_memory`
3. spell 実行**後**に `assistant_content` を `_store_memory` (≈L1018)

LLM messages には judgment が正しく先に入るが、SAIMemory への記録だけが spell 実行後に遅れ、その間に子ラインが全ノードを記録し終える。`run_playbook` は親の pulse_id を継承する (`run_playbook.py:136`) ため、judgment と子ライン出力は同じ pulse_id だが created_at が逆順になる。

## 修正

`assistant_content` の `_store_memory` を spell 実行の**前**に前倒し (judgment は L935 で確定済み)。`combined_results` (spell 結果サマリ) は実行後でないと作れないので従来通り後に記録。結果として `judgment → 子ライン出力 → 結果サマリ` の因果順になる。`_run_spell_loop` は 3 経路 (streaming/sync/tool mode) 共通なので 1 箇所で全経路修正。

## 残課題

- SAIMemory の `created_at` は秒精度 (`int(time.time())`)。web_research のように数秒かかる spell では judgment と明確に分かれるが、瞬時に終わる spell だと同一秒内で順序が保証されない。完全な順序保証が要るなら `created_at` ミリ秒化か明示 seq カラムが別途必要 (別 issue 候補)。
- 修正前に既に逆転して記録されたレコードはそのまま残る (新規 Pulse から正しくなる)。

## ログ

- 2026-05-25: 根本特定 + 前倒し修正 (ruff / test_run_playbook_spell / test_subplay_line / test_spell_args_parsing 計 29 件通過)。サーバー再起動後に有効、実機検証待ち。
