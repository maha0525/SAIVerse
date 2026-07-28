# Issue: Phase 3 段階 4-D — 旧 DEPRECATED コード完全削除

**ステータス**: ✅ 完了 (2026-05-09)
**優先度**: medium
**作成日**: 2026-05-08
**関連**: [docs/intent/persona_cognition/handoff_phase3_impl.md](../intent/persona_cognition/handoff_phase3_impl.md) §段階 4-D, [docs/intent/persona_cognition/line_tag_responsibility.md](../intent/persona_cognition/line_tag_responsibility.md)

## 背景

Phase 3 の line vs タグ責務分離整理で、4-A〜4-C は完了した (revisions v0.21-v0.23)。その時点で既存 Playbook を新形式に翻訳しつつ、旧経路コードは互換のため残置した。**段階 4-D は「旧 DEPRECATED コードを完全削除する」作業**。

`meta_user` 系削除 (v0.28) と pre_spells 機構完成 (v0.28-v0.29) も済んだので、4-D の前提は揃った。あとは削除を実行するだけ。

## 削除対象

| 対象 | 場所 | 備考 |
|---|---|---|
| `include_internal` パラメータ | `sea/runtime_context.py:377` 周辺、関連関数シグネチャ | 4-A で line_role='sub_line' フォールバック扱いにしてある |
| `pulse:{uuid}` タグ併行記録 | `sai_memory/memory/storage.py:1292-1295` | Phase 2.5 で pulse_id カラム化済 |
| `LLMNodeDef.context_profile` Pydantic フィールド | `sea/playbook_models.py:48-55` | DEPRECATED マークのみ |
| `LLMNodeDef.model_type` Pydantic フィールド | `sea/playbook_models.py:57-62` | + `model_type=lightweight` を持つ 23 ノードの修正 |
| `exclude_pulse_id` 関連 | `sea/pulse_context.py` 周辺、関数の引数 | 旧仕様 |
| `required_tags` の残骸 | search/recall 系経路に意味分類用として一部残るが、context 制御用は完全削除 | 4-A で大半は廃止済 |

## 解決手順

handoff_phase3_impl.md §段階 4-D に従う:

1. それぞれの削除対象が**本当に使われていない**ことを grep で確認
2. テストが旧形式に依存していないか確認
3. 4-A〜4-C の順番を逆に辿る形で削除 (低レイヤから)
4. ruff + pytest 全パス確認
5. 実機シナリオ確認 (ユーザー会話 / 自律 Pulse / メタ判断 / サブライン / `/run_playbook`)

## ハマりどころ

- `model_type=lightweight` を持つ 23 ノードの修正方針: Y 案で 4-D 持ち越しにしていた (revisions v0.23)。Spell loop / pre_spells が定着した後だと、軽量モデル指定の意味が再考必要。スケジュール経路や spell_args_decider が軽量で動くか検討
- 削除前 grep を徹底する (コードの残骸を拾い忘れると runtime エラー)

## 関連リソース

- [docs/intent/persona_cognition/handoff_phase3_impl.md](../intent/persona_cognition/handoff_phase3_impl.md) §段階 4-D — 元計画
- [docs/intent/persona_cognition/line_tag_responsibility.md](../intent/persona_cognition/line_tag_responsibility.md) — 設計の核
- [docs/intent/persona_cognition/revisions.md](../intent/persona_cognition/revisions.md) v0.21-v0.23 — 4-A〜4-C 実装履歴
- 旧 handoff [docs/intent/persona_cognition/handoff_2026-05-01.md](../intent/persona_cognition/handoff_2026-05-01.md) — Phase 3 全体ロードマップ

## ログ

- 2026-05-08: issue 起票。Phase 3 主要刷新 (v0.28) と UI 整備 (v0.29) が完了して着手可能になった。
- 2026-05-09: 完了。実装範囲は revisions v0.34 (= 同日のメタ判断 tool 結果不可視バグ修正と一括) を参照。
  削除内訳:
  - `pulse:{uuid}` タグ併行記録 (`sea/runtime.py` / `sea/runtime_emitters.py` の 3 経路) を停止し、`messages.pulse_id` カラム書き込みのみに集約。読み出し互換 (legacy_pulse_tag) は維持。
  - `LLMNodeDef.context_profile` / `LLMNodeDef.model_type` / `CONTEXT_PROFILES` を完全削除。`_warn_once_legacy_field` も削除。`_FULL_CONTEXT_REQUIREMENTS` は `runtime_runner.py` の Playbook 既定値として実用継続のため残置。
  - `runtime_llm.py:lg_llm_node` の base_msgs 構築から context_profile 経路 (1) を削除し、`state["_messages"]` のみを参照する経路に集約。
  - `runtime.py:_select_llm_client` を `force_lightweight` フラグのみで判断するよう簡素化。
  - `ContextRequirements.include_internal` フィールドと `runtime_context.py:391-396` のフォールバックを削除。
  - `exclude_pulse_id` 引数を 4 層 (saiverse_memory adapter / persona history_manager / runtime_context / runtime) と `_payload_passes_context_filter` から削除。関連テストも整理。
  - `model_type=lightweight` 23 ノード問題: ノード単位の混在/部分指定パターンは新仕様 (line='main'/'sub' 一括) と整合せず、Y 案で持ち越されていた。まはー指示で関連 Playbook 9 件 + research オーケストレーター `memory_research` を `archive/` 移動 (= 必要時に作り直す方針)。残りの `autonomy_creation` / `autonomy_memory_organization` / `autonomy_web_research` は休眠状態のため触らず (Pydantic extra='ignore' で `model_type` フィールドは無害化される)。
  - `playbook_dry_run.py` の context_profile 依存ロジック (cached_profiles / intermediate_msg_sources / NO_CONTEXT_PROFILE / REDUNDANT_INPUT / REDUNDANT_INTERMEDIATE / STALE_MEMORIZE / PROFILE_REUSE) を削除。
  - 全 785 テスト pass、ruff clean。
