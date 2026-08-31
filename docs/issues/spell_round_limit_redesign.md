# Issue: spell round 上限到達時の挙動を line 別に再設計する

**ステータス**: 🔲 未着手 (低優先 — 現状実害なし) → **前提変更を [episode.md](../intent/episode.md) §10 が記録（2026-07-13、レビュー中）**: 自律セッションの AUTONOMOUS 化で sub_line 持ち越し設計の前提が変わる。main_line 会話側（棄却）は不変
**優先度**: low
**作成日**: 2026-05-25
**関連**: `sea/runtime_llm.py:_run_spell_loop` (L911 `while loop_count < _MAX_SPELL_LOOPS`), `SAIVERSE_SPELL_MAX_ROUNDS` (.env, 現状 10), `sea/pulse_context.py:current_line_metadata`

## 背景

spell loop の連続実行回数上限 (`_MAX_SPELL_LOOPS`, env `SAIVERSE_SPELL_MAX_ROUNDS`, 現状 10) に到達すると、現状は**残りの spell を棄却** (実行しない) する。MAX=10 なので実用上めったに達しないが、1 つの自律 Pulse で多数の spell を連続発火する長いケースでは達しうる。その時「問答無用で棄却」は乱暴、というのがまはーの問題提起。

(発端は memopedia_note が発火しなかった件だが、そちらは `spell=True` 欠落が原因で回数制限は無関係だった。本 issue は回数制限そのものの設計改善として独立。)

## まはーが詰めた設計

| | 会話 (main_line) | 自律 (sub_line) |
|---|---|---|
| 上限到達時 | **従来通り棄却** (応答は返る、挙動変えない) | **残り spell 実行 + 結果を SAIMemory 注入 + 応答 LLM スキップ → 次 Pulse で継続** |
| 上限把握 | **必要** (下記) | **不要** (持ち越すので気にしなくてよい) |

**会話 (main_line) の上限把握** (会話中だけ意味を持つ):
1. 各 spell 結果メッセージに「今 Pulse の残り実行回数」を添える
2. 上限-1 回目の spell 結果に「上限に達しました。次の応答でのスペルは不発となります」注釈
3. それでも超過して spell を使ったら「スペル連続使用回数の上限です。スペルは発動されませんでした」システム通知を挿入

**自律 (sub_line) の持ち越し**:
- 上限到達後の round の spell も実行し結果を SAIMemory に注入、応答 LLM 呼び出しはスキップ。数分後の次 Pulse でその結果が見えるので継続できる。
- 「LLM 再呼び出しの上限」であって「spell 実行の上限」ではない、という意味付けに変える。

**却下した案**: main_line でも「実行 + 応答 1 回保証」とする案は、結局「今までに 1 ターン足すだけで応答が返る点は同じ」で実質変化なし。main_line は今まで通りでよい (まはー判断)。

## 実装の足がかり

- spell loop の上限分岐: `runtime_llm.py:911` の while。`loop_count >= _MAX_SPELL_LOOPS` 到達時の処理を line 別に分ける。
- line 判定: `state["_pulse_context"].current_line_metadata().get("line_role")` で `main_line` / `sub_line` を取得できる。
- 自律の「結果注入 + 応答スキップ」は、上限到達 round で spell を実行 (`_run_spell_tool_async`) して `_store_memory` し、LLM 再呼び出しを行わずループを抜ける形。

## ログ

- 2026-05-25: 起票。memopedia_note 不発の調査中に round 上限の挙動が議論になり、設計まで詰めたが、当該不発は spell=True 欠落が原因で回数制限は無関係と判明 (MAX=10 で未達)。実害が無いため設計を本 issue に保存して先送り。
