# Pulse の種別 (`_pulse_type`) が子 Playbook に継承されず、head を描く model と実際に走る model がずれる

**ステータス: 未着手** (2026-08-17 起票。W10 レビュー消し込み中に見つけた隣接案件。当初は「schedule の事前承認が一段目でしか効かない」も同じ根に数えていたが、事前承認は Pulse 種別ではなく**ユーザーが指定した起動**に紐づく設計へ変えたため、その症状は消えた — 詳細は [W10 走行メモ](../handoff/2026-08-16_w10_spell_audit_remnants_handoff.md))

## 何が起きているか

`state["_pulse_type"]`（"user" / "schedule" / "auto" / "meta_judgment"）は `compile_with_langgraph` が **その呼び出しに渡された `pulse_type` 引数だけ**から作る (`sea/runtime_graph.py`)。子 Playbook を起こす経路 (EXEC ノード / SUBPLAY ノード / `call_playbook` / `run_playbook` スペル) はどれも `pulse_type` を渡さないため、**子の state では `_pulse_type` が `None` になる**。

同じ Pulse を指す `_pulse_id` は親から継承される (`sea/runtime_runner.py`)。W10 で `_auto_mode` も単調 OR で継承するようにした。`_pulse_type` だけが Pulse-root に取り残されている。

## 実害

**head を描く model の先読みが、実際に走る model とずれる**

`sea/runtime_runner.py` の `_prepare_context` 前の probe は `resolve_execution_context(persona, None, state=...)` を PulseContext なしで呼ぶため、tier は legacy フォールバック (`_force_lightweight_model` / `_pulse_type == "auto"`) だけで決まる。auto Pulse の子 Playbook (line='main') は `_pulse_type` が None なので probe は **standard** を返すが、実際に走る LLM ノードは共有 PulseContext の root frame (aspect=AUTONOMOUS) から **lightweight** を選ぶ。head の render 対象 model と実行 model が食い違い、Metabolism の閾値も別 model の帳簿で数えられる。

## なぜ W10 の消し込みで直さなかったか

継承させる修正自体は `compile_with_langgraph` の 1 行 (`pulse_type if pulse_type is not None else parent.get("_pulse_type")`) で足りるが、`_pulse_type` の消費点は tier だけではない:

- `sea/runtime.py` `_select_llm_client` / `sea/pulse_context.py` `resolve_execution_context` — legacy tier フォールバック (上の実害を直す方向に動く)
- `sea/runtime_llm.py` `_is_meta_judgment_pulse` — メタ判断 Pulse の独白バッファ。継承させると子 Playbook の LLM 出力も `meta_judgment_log` に積まれる

つまり「どの model で走るか」と「何がメタ判断の記録として残るか」に同時に触れる変更で、Spell 監査残の消し込みの範囲を越える。継承の是非は消費点ごとに裁定してから入れる。

## 直すときの入口

1. 消費点それぞれについて「子 Playbook で親の値を読むのが正しいか」を判定する。とくにメタ判断バッファは「Pulse-root だけ」が正しい可能性がある (その場合は継承ではなく root 判定の別フラグ)。
2. tier のずれを直すなら、probe の導出を legacy フォールバックでなく「これから push される root aspect」から取る形も候補 (`aspect_from_pulse_type(pulse_type)` は既に `run_meta_user` が呼んでいる)。
3. 回帰テストは「auto Pulse の子 Playbook が lightweight を選ぶ」。

## 隣接: `call_playbook` ツールの要否

`call_playbook` は builtin / 開発 DB のどの Playbook からも参照されておらず (spell でもない)、現状どこからも起動されない。残置の経緯は [revisions.md](../intent/persona_cognition/revisions.md)「旧 `call_playbook` は当面残置」。**削除するか、経路を直して残すかはまはー裁定待ち**。

同ツールにあった「確認ダイアログの宛先 (`event_callback`) を子へ渡していない」欠陥 — user Pulse でも `_request_playbook_permission` が「チャネル無し = deny」で即拒否していた — は 2026-08-17 に修正済み (回帰: `tests/test_spell_auto_mode_w10.py`)。

## 関連

- `sea/runtime_graph.py` (`_pulse_type` の生成点) / `sea/runtime_runner.py` (probe) / `sea/runtime_llm.py` (メタ判断バッファ)
