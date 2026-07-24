# Issue: Beat 概念が実装に型として存在しない

**ステータス**: 🟠 部分解決 — **身分と直列性は実装済み、残るは「中身の対」だけ**（2026-07-22 更新）

- ✔ **身分**: `ExecutionContext`（`sea/pulse_context.py`）— 誰の・どの thread / line / aspect / model / pulse の実行か（[beat_execution_context.md](../intent/beat_execution_context.md) §6-1、2026-07-17）
- ✔ **直列性**: `BeatGate`（`sea/beat_gate.py`）— persona 単位ロック + 実行台帳の関所（同 §6-2）
- 🟠 **中身**: 表示用 merged 全文 と SAIMemory 用 continuation の対 → [runtime_llm 分割](runtime_llm_node_split_design.md) の `BeatExecution`。**Phase 1（2026-07-22）で型は立ち上がり、SAIMemory 側（`continuation`）が入った**。表示側（`display_text`）は書き手が生成 4 経路にあるため **Phase 2 で揃う** — そこで本 issue はクローズ可能になる
- 表示面（バブル区切り）の設計は [episode.md](../intent/episode.md) §9 に統合（2026-07-13、レビュー中）— 時間階層の入れ子に乗る
**優先度**: medium
**作成日**: 2026-05-29
**関連**: `docs/overview/landscape.md` §4、`sea/runtime_llm.py` `_run_spell_loop`、memory `project_beat_concept`

## 背景

俯瞰地図 (`docs/overview/landscape.md`) を作る過程で、**ペルソナの最小行動単位**に相当する概念が明確化され「**Beat**」と命名された（脚本術の beat = キャラクターの最小の行動・意図単位、に由来）。

- Beat = Pulse（認知サイクル1回）より小さく、message（記録単位）とも一致しない中間単位
- 応答(reply)でも発話(utterance)でもない中立語。喋る / 道具を使う / 自律的に内省を表出する、すべてが「1 Beat」

問題は、**この概念が実装には型/クラスとして存在しない**こと。実体は `sea/runtime_llm.py` の `_run_spell_loop` の戻り値 `full_merged_text`（ただの `str`）でしかない。名前が無いまま実装が育ったため、概念の関係図で `Playbook → Spell` と中間が飛ばされる歪みを生んでいた。正しい関係は：

```
Pulse ⊃ Beat
Playbook(発話ノード) → 生成 → Beat → 発動 → Spell
```

### 派生問題: Beat が記録先で割れている

Beat は記録先で2つに割れていて、これを束ねる概念名（型）が無い：

- **表示用**（UI バブル / 建物履歴 / ペルソナ履歴）= `full_merged_text`（Spell 結果込みの合成版）
- **SAIMemory 保存用** = `final_continuation`（最終発言のみ、巨大 record の重複回避のため）

## 解決案候補

- `Beat` を型/dataclass として導入し、`_run_spell_loop` の戻り値（現 `Tuple[str, str, int]`）を `Beat` オブジェクトに置き換える
- 表示用レンダリングと SAIMemory 保存用を `Beat` のメソッド/プロパティとして整理（`beat.rendered` / `beat.final` 等）
- Beat に id を振り、Pulse との親子関係（1 Pulse ⊃ 複数 Beat）を構造として表現
- 影響範囲調査: `_run_spell_loop` の呼び出し元、履歴記録経路、UI バブル生成経路

## 関連リソース

- `sea/runtime_llm.py` `_run_spell_loop`（docstring に「1 応答 = 1 record」の記述あり、867-868 行付近）
- `docs/overview/landscape.md` §4（Beat を中心概念として記載、実装ギャップを明示）
- memory `project_beat_concept`
- `docs/intent/persona_cognition/nested_subline_spell.md`（Spell loop / report_to_parent の設計）

## ログ

- 2026-05-29: 俯瞰地図作成中に Beat 概念を発見・命名。実装に型が無いことが判明したため本 issue を起票（リファクタは後日）。
- 2026-07-06: `runtime_llm.py` 分割設計書（[runtime_llm_node_split_design.md](runtime_llm_node_split_design.md)）に本 issue の解決を組み込み。`BeatExecution` dataclass（display_text / memory_text の対を持つ）として Phase 1 で型を導入し、Phase 3 で `Beat` を公開する計画。
- 2026-07-22: 統合工事（beat_execution_context.md §6-1 / §6-2）で Beat の**身分**（`ExecutionContext`）と**直列性**（`BeatGate`）が先に型・機構になったため、本 issue の残余を「中身の対」だけに縮小。`BeatExecution` は `ExecutionContext` を保持する形に設計変更（身分の再宣言は不変条件 §4-1 に反する）。runtime_llm 分割 Phase 0 実装時に整理。
- 2026-07-22: runtime_llm 分割 Phase 1 で `BeatExecution` を導入（`sea/runtime_llm.py`）。`_run_spell_loop` の戻り値タプルが埋め込まれていた閉包末尾 226 行が `_finalize_beat(runtime, beat)` になり、記録側のテキスト（`continuation`）が初めて型のフィールドになった。表示側と `ctx` は Phase 2。
