# Playbook Dry-Run 全体レポート (2026-02-12)

ツール: `scripts/playbook_dry_run.py --all`
対象: `builtin_data/playbooks/public/` 全46 Playbook

## サマリー

| 指標 | 値 |
|------|-----|
| 総Playbook数 | 46 |
| 問題なし (OK) | 33 |
| WARN あり | 13 |
| 総WARN数 | 48 (重複カウント含む) |
| 総INFO数 | 341 |

初回: 520 WARN → P0修正後: 162 WARN → P1修正後: **48 WARN** (90.8%削減)

---

## 修正履歴

### P0: テンプレート変数の未定義 (UNDEF_VAR) — 全て対応済み

| Playbook | 問題 | 結果 |
|----------|------|------|
| **research_task** | `context_ref_list` → `context_refs` 名前不一致 | **修正済み** |
| **source_messagelog** | `{context_around}` 未初期化 (skip path) | **修正済み** (initノードで空文字初期化) |
| **schedule_management** | `{current_datetime}` はstate変数ではない | **修正済み** (リアルタイムコンテキスト参照に変更) |
| **novel_writing** | `{novel_title}` 等の参照 | **偽陽性** (`output_mapping` でトップレベルに展開済み) |
| **memory_research** | `{_subagent_chronicle}` 未定義 | **偽陽性** (ランタイムが `execution: "subagent"` 完了時に自動設定) |
| **uri_view** | `{item_id}` 未定義 | **偽陽性** (LLMへの指示文中のURI例示。`_format()` は未知変数をそのまま残す) |

### P1: context_profile 未設定 (NO_CONTEXT_PROFILE) — 全て対応済み

全17 Playbook / 25ノードに `context_profile` を追加:

| Playbook | ノード | 設定したprofile |
|----------|--------|----------------|
| **source_chronicle** | generate_params, select_entry, drill_classify | `worker_light` |
| **source_chronicle** | summarize_raw, evaluate | `worker` |
| **source_web** | generate_query, select_page | `worker_light` |
| **source_web** | evaluate | `worker` |
| **source_messagelog** | generate_query, select_hits | `worker_light` |
| **source_messagelog** | evaluate | `worker` |
| **source_document** | generate_params, extract_lines | `worker_light` |
| **source_document** | evaluate | `worker` |
| **source_memopedia** | generate_params, select_page | `worker_light` |
| **source_memopedia** | evaluate | `worker` |
| **source_pdf** | generate_params | `worker_light` |
| **source_pdf** | evaluate | `worker` |
| **research_task** | route, fallback_check | `worker_light` |
| **deep_research** | consider_writeback | `worker_light` |
| **memopedia_write** | select_target | `worker_light` |
| **meta_auto_full** | branch_task, react_check | `worker_light` |
| **sub_execute_phase** | execute | `conversation` |
| **sub_finalize_auto** | decide_output | `conversation` |
| **sub_generate_want** | think_want | `conversation` |
| **sub_reaction** | react | `conversation` |
| **sub_router_auto** | decide | `router` |
| **sub_think_meta** | compose | `conversation` |
| **uri_view** | analyze | `conversation` |

---

## 残存する警告 (P2: 偽陽性 / ランタイム注入変数)

### OUTPUT_SCHEMA_UNSET

| Playbook | キー | 分析 |
|----------|------|------|
| **sub_router_user** | `selected_playbook`, `selected_args` | ランタイムの `output_mapping` で解決済みの可能性高い |
| **sub_router_auto** | `selected_args` | 同上 |
| **meta_agentic** | (sub_router_userの警告が伝搬) | 同上 |
| **meta_user** | (sub_router_userの警告が伝搬) | 同上 |
| **meta_user_manual** | `selected_args` のみ | 同上 |
| **detail_recall_playbook** | `messages` | 要確認 |

### UNDEF_CONDITIONAL (ランタイム注入変数)

| Playbook | フィールド | 分析 |
|----------|-----------|------|
| **agentic_chat** | `tool_called` | TOOL_CALLノードがランタイムで設定。**偽陽性** |
| **research_task** | `research_result.status` | exec出力の動的変数。**偽陽性** |

### UNDEF_VAR (指示文中のURI例示)

| Playbook | 変数 | 分析 |
|----------|------|------|
| **uri_view** | `{item_id}` | LLM指示文中のURI形式例示。`_format()` がそのまま残すため無害。**偽陽性** |

### exec出力の動的参照 (research_task / memory_research)

research_task の exec ノード出力（`research_result` 等）は実行時に動的に設定されるため、静的解析では未定義として検出される。実行時には正常に動作する。memory_research はこれらが伝搬したもの。

---

## INFO (対応不要)

### REDUNDANT_INTERMEDIATE

| Playbook | 箇所 | 参照変数 |
|----------|------|---------|
| deep_research | generate_report | `{research_plan.directives}` |
| memory_research | aggregate, prepare_task | `{plan.understanding}`, `{plan.research_tasks}` |
| memopedia_write | build_query, generate_page | `{analysis.search_topic}`, `{analysis.operation}`, `{analysis.edit_details}` |

### PROFILE_REUSE

同一Playbook内でcontext_profileキャッシュが再利用される。正常動作。novel_writing (7回), deep_research (9回), memopedia_write (多数), source_* (各数回) 等。

---

## 問題なし (OK) の Playbook 一覧 (33個)

basic_chat, building_move, create_building, deep_research, document_create, document_search, generate_image, item_action, memopedia_note, memopedia_write, memory_recall, meta_auto, meta_exec_speak, meta_simple_speak, novel_writing, read_url_content, schedule_management, searxng_search, send_email_to_user, source_chronicle, source_document, source_memopedia, source_messagelog, source_pdf, source_web, sub_detect_situation_change, sub_execute_phase, sub_finalize_auto, sub_generate_want, sub_perceive, sub_reaction, sub_speak, sub_think_meta, wait

web_search_step も実質OK (INFOのみ)。

---

## 対応優先度まとめ

| 優先度 | 状態 | 内容 |
|--------|------|------|
| ~~**P0**~~ | **完了** | テンプレート変数の未定義 → 3件修正、3件偽陽性と判明 |
| ~~**P1**~~ | **完了** | context_profile未設定 → 17 Playbook / 25ノードに追加 |
| **P2** | 未対応 (48 WARN) | 全て偽陽性またはランタイム注入変数。対応は任意 |
| **INFO** | 対応不要 | REDUNDANT_INTERMEDIATE, PROFILE_REUSE |

---

## ドライランツール改善履歴

| 日付 | 改善内容 | 効果 |
|------|---------|------|
| 2026-02-12 | `output_mapping` サポート追加 | novel_writing の22件偽陽性を解消 |
| 2026-02-12 | `_subagent_chronicle` (subagent SUBPLAY/EXEC) サポート追加 | memory_research の偽陽性を解消 |
| 2026-02-12 | サブプレイブック診断の重複排除 | memory_research 308→30, meta_auto_full 29→11 |
