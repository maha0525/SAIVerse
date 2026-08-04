# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

It is intentionally limited to **repo-specific gotchas and working agreements**. Reference material (full command lists, schemas, directory trees, provider specs) lives under `docs/` and is linked from here rather than duplicated.

## Notes for Claude Code

**Language**: Think in English, respond in Japanese. The repository owner prefers Japanese for communication.

**Local preferences**: If `CLAUDE.local.md` exists in the repository root, read it for additional context (names, personal preferences, etc.).

## Production Persona Safety — Absolute Boundary

Production personas and their histories are user-owned, identity-bearing data. A coding or testing request does **not** authorize interacting with them.

- Never send a message to a production persona, trigger a production Pulse/Playbook/Spell, call a persona's configured LLM, or write to production SAIMemory / persona event logs / conversation history / world state without the user's **immediate, explicit approval for that exact operation**.
- Never invent text and submit it as the user. User-authored input sent to a persona must be verbatim text the user explicitly approved for sending. Paraphrase, “test messages,” and agent-written prompts presented as the user are impersonation.
- “Continue,” “test it,” “use the production path,” “I restarted it,” or similar workflow approval is not approval to contact a production persona. Approval must identify that a live persona interaction may occur and that it can create persistent history and paid API usage.
- Before requesting approval, state the target persona/environment, the exact message or action, whether an LLM/API charge will occur, and which persistent stores may change. Approval is single-purpose and does not carry forward to later calls.
- Default all integration tests to an isolated `SAIVERSE_HOME`, synthetic persona, mock/fake LLM, or no-LLM harness. “Real path” means reproducing the production code path in isolation, not using production identity or data.
- Read-only inspection of production logs/configuration is allowed when necessary. Any action that can generate cognition, speech, memory, events, schedules, movement, or paid inference is a write and requires approval.
- If accidental production contact occurs, stop all production mutations immediately. Do not delete, edit, “repair,” or provoke another response to compensate. Report the exact known operations and wait for the user's cleanup decision.

This boundary overrides autonomy, persistence, “finish the task,” “verify the real path,” and minimizing user burden. Persona dignity, authorship integrity, and cost authorization come first.

## Project Overview

SAIVerse is a multi-agent AI system where autonomous AI personas inhabit a virtual world of Cities and Buildings. Multiple LLM providers with automatic fallback, persistent memory via SAIMemory (SQLite), the SEA (Self-Evolving Agent) LangGraph playbook framework, an optional Discord gateway, and a Next.js frontend over a FastAPI backend.

Start with `docs/overview/landscape.md` (concept map) and `docs/overview/roadmap_status.md` (where we are) before reading code.

## Development Commands

### Database — read this before running anything destructive

```bash
python database/seed.py              # ⚠️ DELETES ALL DATA (personas, conversations, playbooks). Prompts for 'DELETE'
python database/seed.py --force      # ⚠️ same, no confirmation
python scripts/import_all_playbooks.py   # SAFE: playbooks only, preserves everything else
python database/migrate.py           # schema changes, creates automatic backups
```

`seed.py` is the single most dangerous command in the repo. `import_all_playbooks.py` is what you almost always actually want.

### Running, testing, linting

```bash
python main.py city_a    # backend on 127.0.0.1:8000 (API at /api), frontend on :3000
python -m pytest         # tests (unittest also works)
ruff check .             # or: ruff check --fix .
```

Only `city_a` is seeded by default (`builtin_data/cities.json`). Starting an unregistered city name (`python main.py city_b`) on a single-city DB does **not** error: the CITYNAME auto-repair renames `CITYID=1` to the requested name (tutorial-rename rescue). Since 2026-07-31 the repair is refused while another running process owns the same DB (runtime-marker check) — renaming a live city would run the same personas in two processes.

**After writing or modifying Python, always run `ruff check` on the changed files** before considering the task complete. It catches undefined names (e.g. `LOGGER` where `logging` was meant) that would otherwise fail at runtime.

### Other setups

- **GPU / embeddings** (`SAIMEMORY_EMBED_CUDA=1|0`, unset = auto-detect): `docs/getting-started/gpu-setup.md`
- **Isolated test environment** (port 18000, `test_data/`): `docs/test_environment.md`. Use `python test_fixtures/test_api.py --quick` for fast verification without LLM costs. The chat API returns streaming NDJSON; the user must have `CURRENT_BUILDINGID` set and personas need `LIGHTWEIGHT_MODEL` for router nodes.
- **Backup / recovery scripts**: `docs/reference/scripts.md`. Automatic backups on startup are controlled by `SAIVERSE_DB_BACKUP_ON_START`, `SAIVERSE_DB_BACKUP_KEEP`, `SAIMEMORY_BACKUP_ON_START`.
- **Test setup and scenarios**: `docs/developer-guide/testing.md`

## Architecture

### Core Components

**SAIVerseManager** (`saiverse/saiverse_manager.py`) — central orchestrator, owns all PersonaCore and Building instances in memory, delegates movement to OccupancyManager, handles SDS registration. Its `VisitingAI`/`ThinkingRequest` polling is **frozen (2026-07-16)** and does not start.

**PersonaCore** (`persona/core.py`) — the "soul" of each persona. **There is no `run_pulse()` method.** The Pulse (cognition→decision→action) is driven by the SEA runtime: `SAIVerseManager.run_sea_user` / `run_sea_auto` → `PulseController` → `SEARuntime`.

**SEARuntime** (`sea/runtime.py`) — executes playbooks via LangGraph. Two meta-playbooks: `meta_user` (user input) and `meta_auto` (autonomous pulse). Playbooks are JSON in `builtin_data/playbooks/` or the DB `playbooks` table.

- **Model tier is derived from the line's aspect** (CONVERSATION/WORKER/AUTONOMOUS/META, see `sea/pulse_context.py`), **not** from a per-node field. The old per-node `model_type` only survives in `builtin_data/playbooks/archive/`.
- Persona model priority: chat UI override > persona `DEFAULT_MODEL` (DB) > env `SAIVERSE_DEFAULT_MODEL` > built-in default (`saiverse/model_defaults.py`). `LIGHTWEIGHT_MODEL` falls back to `SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL` or `BUILTIN_DEFAULT_LITE_MODEL`.
- Lightweight tier serves sub-line/worker and autonomous judgment; default tier serves main-line conversation and tool parameter generation.

**OccupancyManager** (`saiverse/occupancy_manager.py`) — handles *all* entity movement and capacity limits. Do not call PersonaCore methods directly to move anything.

**ConversationManager** (`saiverse/conversation_manager.py`) — **legacy / no-op** since the 2026-05-01 cognitive-model migration. Its v1 successors are gone too: `SubLineScheduler` was deleted 2026-07-06 and the `track_autonomous` playbook retired 2026-07-10. Current autonomous driving is the **time-table + judgment points** in `saiverse/autonomy_wiring.py`; `AutonomyManager` now runs a watchdog-only tick. Removing the `ConversationManager` class itself is still pending (landscape §9).

**RemotePersonaProxy** (`saiverse/remote_persona_proxy.py`) — **frozen**. `/inter-city/*` and `/persona-proxy/{id}/think` return 503.

### Data Flow

- **User interaction**: UI → SAIVerseManager → PulseController → SEARuntime → LLM + Tools/Spell → SAIMemory + BuildingHistory
- **Autonomous pulse**: time-table + judgment points (`saiverse/autonomy_wiring.py`) / `AutonomyManager` watchdog → PulseController → SEARuntime → think/speak nodes → SAIMemory
- **Inter-city travel**: 🧊 **frozen 2026-07-16**. It was DB-mediated (VisitingAI table polling), never direct API calls. Revival re-designs from `docs/handoff/2026-07-15_persona_city_building_separation_audit.md` (landscape §8).

### Memory Stack

- **SAIMemory** (`sai_memory/`, `saiverse_memory/adapter.py`) — per-persona SQLite at `~/.saiverse/personas/<id>/memory.db`, messages tagged conversation / internal / task / summary.
- **Task storage** (`persona/tasks/storage.py`) — per-persona `tasks.db`.

## Model & Provider Configuration

Full design: `docs/intent/model_provider_management.md`. End-user setup (Kimi / LM Studio / llama.cpp): `docs/custom_providers.md`. Provider list: `docs/reference/providers.md`.

The parts that bite:

- **3-layer priority** for both providers and models: `~/.saiverse/user_data/{providers,models}/` > `expansion_data/<addon>/{providers,models}/` > `builtin_data/{providers,models}/`.
- **Resolution order** (`saiverse/model_configs.py:_resolve_provider_ref`): direct fields on the model JSON win → missing fields inherited from `provider_ref` → legacy `provider` field used as-is. Prefer `provider_ref` for new models.
- Only `openai_compat` and `ollama_compat` providers can be created from the UI. `anthropic_native`, `gemini_native`, `xai_native`, `nvidia_nim`, `openai_codex` require code support in `llm_clients/` and are builtin-only.
- Editing a builtin model/provider from the UI **creates a user_data override**; the builtin file is never modified. Deleting the override restores the builtin.
- After editing files by hand, `POST /api/config/reload-models` and `POST /api/providers/reload`, or restart.

## Directory Structure

Full tree: `docs/developer-guide/project-structure.md`.

The rule that matters: **resource loading is 3-layer — `~/.saiverse/user_data/` (highest) > `expansion_data/` (user-installed packs, gitignored) > `builtin_data/` (lowest)**. This applies to tools, phenomena, playbooks, and models. User data lives outside the repo in `~/.saiverse/` (override with `SAIVERSE_HOME` / `SAIVERSE_USER_DATA_DIR` for testing); `main.py` migrates legacy in-repo `user_data/` on startup.

## Key Files and Patterns

**Database schema** (`database/models.py`, generated reference: `docs/reference/database-schema.md`). Non-obvious points:

- **AI.AUTONOMY_ENABLED** (bool, default True) toggles autonomous behavior **only**. It does not stop replies to conversation. "Is it active hours right now" is a separate concept owned by Life.
- ⚠️ The old `ACTIVITY_STATE` (Stop/Sleep/Idle/Active) was dismantled and the column dropped on 2026-07-14 — only "Active or not" ever mattered (landscape §9).
- **VisitingAI** / **ThinkingRequest** are frozen; nothing polls them.
- **Tool** + **BuildingToolLink** is a legacy table and **currently unused**. Tools reach personas via Spell (`spell=True`) or Playbook TOOL nodes, *not* this table.

**LLM integration** (`llm_clients/`, `saiverse/llm_router.py`) — factory pattern via `get_llm_client(model_name, config)`. Ollama auto-probes localhost and falls back to Gemini 2.0 Flash if unreachable. `llm_router.py` uses Gemini 2.0 Flash to decide tool calls. llama.cpp server processes are auto-launched by `llama_server.py` (configured via the `llama_server` field in the model JSON).

**Tools** (`tools/`, generated catalog: `docs/reference/tool-catalog.md`) — `TOOL_REGISTRY` in `tools/__init__.py`. Tools load from `~/.saiverse/user_data/tools/` (priority) and `builtin_data/tools/`. Tools in subdirectories need a `schema.py` with `schemas()`. `tools/context.py` uses contextvars to inject persona/manager references during execution.

## Comprehensibility Is a Design Constraint (把握可能性、2026-07-28 確立)

**設計 (intent・概念構造・用語) とドキュメントは、リポジトリオーナーとユーザーが全体を把握できる複雑さに収まっていなければならない。** これは機能要件と同格の制約で、超えたら設計不合格。オーナーの「読んで分からない」がその判定であり、反論の対象ではない。AI (Claude) の処理能力が上がったことで、複雑さの痛みを書き手が感じなくなった — だから外部の検査で縛る。**コード内部の複雑さは対象外** (実装は AI スケールで扱ってよい)。

- **健全性の検査**: そのサブシステムの芯を、専門用語なしの数行で説明できるか。できなければ設計が複雑すぎるか、書き手が理解していない。
- **例外処理・救済機構が本体を覆い始めたら止まる**: 例外経路を精巧にする前に、供給源 (上流の欠陥) を塞いで機構ごと消せないかを先に問う。経緯の実例: `docs/handoff/2026-07-28_arasuji_pipeline_audit.md` (Chronicle の規則の大半が上流の傷への例外処理だった)。
- **用語の新設は同族の負債**: 既存の定義済み語彙で書けるならそれが最善。新語は「今後も残る確固たる概念」にだけ与える。
- 「読んで分からない」と言われたら、説明を直すだけでなく**設計自体が不合格の可能性**を必ず検討する。分かりにくい説明の何割かは、分かりにくい設計の正確な写しである。

## Intent Documents

Each feature/subsystem has an Intent Document in `docs/intent/` recording WHY it was built and what invariants it must hold. They capture the reasoning that code cannot express — e.g. raising the Stelis anchor display to 50 messages would defeat the purpose of context isolation.

**Workflow**: before implementing, check for `docs/intent/<feature>.md`. If it exists, read it first. If it doesn't, create it — read the related code, draft, interview the user on unclear points, revise, get final feedback — then implement with it as guide.

### Reasoning at whole-system scope

Before narrowing to a local implementation, state the relevant whole in the intent. This is a reasoning frame, not a form to fill; the shape varies by subsystem, but narrowing is not allowed until the whole has been stated.

- **Outcome and affected parties**: what users, personas, operators, and maintainers must be able to rely on.
- **End-to-end journey**: producers, transformations, persistence, consumers, replacement, migration, operations. Include only the relevant stages, but do not stop at the file being edited.
- **Invariants and ownership**: what must stay true, which boundary owns that truth, which component is the source of truth. Place the correction where the invariant can hold **for every consumer** — a local fix must not make one component look correct by concealing a broken contract or exporting cleanup downstream.
- **Change placement**: why this location is the correct owner rather than the easiest place to mask the symptom.
- **Consequences**: if the local change works perfectly, what remains wrong, what cost or ambiguity moves elsewhere, and who hits it next. A small blast radius is not evidence of a complete design.
- **Verification journey**: evidence that crosses the boundaries the outcome depends on. A local test or a good screenshot cannot by itself prove an end-to-end outcome. Before handoff, state which real journey was verified and which boundary remains unverified — do not redefine completion to match the part that was easiest to test.

### After a failure we caused

Record **three distinct causes**: the proximate technical cause, the decision-making failure that made the local change look sufficient, and the process or system condition that let that decision pass. Addressing only the first is incident cleanup.

Then apply the **transfer test**: the proposed prevention must catch at least two structurally similar failures in *unrelated* subsystems. A rule written mainly in this incident's nouns, file types, or exact steps is another local workaround — reject it.

Prefer durable enforcement (clear ownership, executable contracts, validators, boundary tests, observability) over prose reminders. When only a written rule is currently possible, say so instead of presenting documentation as a completed safeguard.

## Important Conventions

### Documentation Maintenance (docs are part of the change surface)

When a change alters a fact a doc states, update that doc in the **same** change:

- Adding / moving / renaming modules or top-level directories → `docs/developer-guide/project-structure.md` (drifts fastest)
- New or changed concept → the relevant `docs/concepts/*.md` and the map `docs/overview/landscape.md`
- Deprecating a concept → record it in `docs/overview/landscape.md` §9 (死んだ概念) and remove *actionable* references elsewhere
- New/removed feature, playbook node field, env var, API route, DB table, script → the matching `docs/reference/*` / `docs/features/*`

**Auto-generated docs** — `docs/reference/{tool-catalog,api-endpoints,database-schema}.md` carry an `AUTO-GENERATED` banner and must **never be hand-edited**. After changing tools, API routes, or DB models, run `gen_reference_docs.bat` (non-Windows: `python scripts/gen_reference_docs.py`) and include the regenerated docs in the same commit. There is no CI gate; `--check` reports drift locally. The other reference docs (providers / phenomena / playbooks / env vars / `saiverse://` URI) are hand-maintained.

If unsure whether a doc references what you changed, grep `docs/` for the symbol before finishing.

### Continuous Refactoring (Claude surfaces this proactively)

The repository owner cannot judge *when* refactoring is due — noticing the timing is Claude's job. Refactoring is narrow and frequent, never repo-wide.

- **Boy-scout rule**: while working in an area, fix small obviously-safe debt you notice there (dead code, stale comments, trivial duplication) in the same branch, as a **separate commit** from the feature change. Propose anything larger instead of mixing it in.
- **Pre-work health check**: before a significant change in a subsystem, consult `docs/overview/architecture_health.md` (§4 lookup table) and check `docs/issues/` and landscape §9 for known debt. If a cleanup would make the coming change safer, propose it as a prep step *before* implementing.
- **Scope discipline**: a refactor needs a reason, usually "we are about to touch this area". Broad refactors of code nobody plans to touch never recover their regression-verification cost.
- **Filler tasks**: dead-concept removals in `docs/issues/` and landscape §9 are low-risk standalone cleanups, good to suggest when work is otherwise paused.

### Progress Tracking — 進行中案件の台帳 (`docs/overview/in_flight.md`)

進行中(アクティブ)の案件は `docs/overview/in_flight.md` に索引される。**状態の真実は各 intent(冒頭のステータス行) / issue(未解決 `docs/issues/` ↔ 完了 `docs/issues/archive/` のフォルダ位置)が持ち**、台帳はそこから「進行中」だけを抽出して *次アクション* と *誰待ち* を可視化する薄いビュー(完了・未着手は載せない、状態は背負わない)。

**Claude が番人**: 案件(intent / issue / 対応コード)に触れたセッションでは、終わる前に台帳と doc のステータスを**同じコミット**で現況に合わせる。まはーに更新を求めない。

| トリガー | やること |
|---|---|
| **進行中入り**(実装着手 or レビュー/検証サイクル入り) | 台帳に行追加 + doc を進行中に(issue=未解決に置く / intent=ステータス行を設計中〜検証待ちに) |
| **進行中の変化**(次アクション/誰待ち/フェーズが動いた) | 台帳の該当行を**差し替え**、押し出される旧文面は同一コミットで doc の「経緯」節へ**移送**(削除禁止、教訓は memory へ) |
| **完了**(実機/まはー検証まで済) | 台帳から削除 + issue は `archive/` へ移動 / intent はステータス行を「完了」に |
| **却下・凍結** | 台帳から外す + doc に理由を記録 |

**次アクション欄の器 (2026-08-04 確立)**: 書けるのは「現在地 1 文 + 次の一手 1〜2 文」だけ(上限 300 字)。過去形の記録(日付・コミットハッシュ・裁定の経緯・教訓)は書かず、各案件 doc の「経緯」節へ。台帳を触ったら `python scripts/check_in_flight.py`(字数と過去形マーカーの機械検査)を通してから終える。解体時点で未移送の3行だけは当時の行のまま(行全体の指紋一致)に限り警告扱い(警告のみなら exit 0、免除外の違反は exit 1) — どの列でも書き換えたら本検査に昇格する。

**状態語彙(intent ステータス行と台帳で共通)**: `未着手`(構想止まり・台帳外) / `設計中` / `実装待ち` / `実装中` / `検証待ち` / `完了` / `凍結`。

**台帳の手前 — アイディア帳 (`docs/overview/ideas.md`)**: intent にも issue にもなっていない生アイディアの置き場。まはーがチャットで「これやりたい」と口にしたアイディアは、私(Claude)がここに書き留める(まはーに管理させない)。着手が決まったら `ideas.md` から削除して in_flight 台帳へ卒業させる。

### ⚠️ Never guess attribute or method names

**Read the actual definition before using any existing object's attributes or methods.** This applies to PersonaCore (`persona/core.py`), DB columns (`database/models.py`), LLM clients (`llm_clients/`), and manager methods (`manager/`, `saiverse/saiverse_manager.py`).

The failure mode is specific: writing `persona.building_id` when the real attribute is `persona.current_building_id`, or assuming a `provider` attribute exists. A five-second Read or Grep settles it. Only invent names for **new** code you are creating.

Related trap: `getattr(obj, name, default)` silently swallows attribute-name typos. When a value is unexpectedly the default, suspect the name before suspecting the logic.

### Debugging

- **Logs and console output are the primary source of truth.** Check terminal output, browser console, and the network tab *before* changing anything. The user always runs at DEBUG level, so nothing is hidden by log level.
- **Identify exactly what fails before fixing.** If unclear, name the missing information and how to obtain it (add logging, inspect the DOM, read the docs) instead of guessing. Don't try several approaches hoping one lands.
- **One problem at a time.** Don't switch approaches until you understand why the current one failed.
- **Asymmetric bugs indicate implementation mismatch.** If a bug appears in scenario A but not B despite similar logic, compare the code paths side by side — it is almost never a timing/race issue.
- **When the user reports a UI issue**, listen precisely to what they actually did ("sidebar button" ≠ "home screen button"), and investigate the *difference* between the working and broken case rather than assuming a cause. Use DevTools: Console for errors and selector tests, Elements for real DOM/CSS, Network for request URLs.
- **When touching external APIs**, check the official docs first (Gemini structured output limits especially).

### Branch Strategy

`feature/*` → PR → `develop` → (tested) → PR → `main`. Feature branches are created from `develop`.

### Logging

Session logs live under `~/.saiverse/user_data/logs/{YYYYMMDD_HHMMSS}/`:

| File | Logger | Purpose |
|------|--------|---------|
| `backend.log` | root | Application-wide log + console mirror |
| `llm_io.log` | `saiverse.llm` | LLM API request/response I/O (JSON) |
| `sea_trace.log` | `saiverse.sea_trace` | SEA playbook node execution trace |
| `timeout_diagnostics.log` | `saiverse.timeout` | Timeout event diagnostics |

Per-persona: `~/.saiverse/personas/<id>/log.json`, `conscious_log.json`. Set `SAIVERSE_LOG_LEVEL=DEBUG` and `SAIVERSE_SEA_TRACE=1` for verbose output.

- When `LOGGER.debug()` with `extra={}` shows no details, use `print()` — the formatter may not render `extra` fields.
- Browser `console.debug()` is filtered by default; use `console.log()` for messages that must always appear.

### Memory and History

Building chat history is kept in memory and logged to `~/.saiverse/cities/<city>/buildings/<building>/log.json`. SAIMemory rows are appended via `SAIMemoryAdapter.append_building_message()` / `append_persona_message()`. Pulse internal thoughts use tag `internal` with a `pulse_id` for grouping; user conversations use tag `conversation`.

### Setup/Update Script Parity

These groups **must** stay in sync — changing one requires changing the others:

- **Update**: `update.bat`, `update.sh`, `scripts/self_update.py`
- **Setup**: `setup.bat`, `setup.sh`

`pip` must always be invoked as `python -m pip` (not bare `pip`) to avoid Device Guard issues on Windows.

## Common Pitfalls

- **Do not run `database/seed.py` carelessly** — it wipes the database.
- **Inter-city travel is not direct API calls** — it was DB-mediated through VisitingAI polling (and is currently frozen).
- **Gemini structured output does not support `additionalProperties`** — keep response schemas simple.
- **Gemini's context window is 1M+ tokens.** Do not blame large context for errors; the system is designed for large histories.
- **Playbook `next` pointers must form valid DAGs.** After editing JSON in `builtin_data/playbooks/`, run `python scripts/import_playbook.py --file <path>` to load it into the DB.
- **Schema changes go in `database/migrate.py`**, and are tried against a copy first: `python database/migrate.py --db <path-to-copy>` (the flag is `--db`, not `--db-file`). It migrates in place, so pointing it at the live DB is how you find out the hard way.
- **When refactoring, complete the change or revert it** — never leave the codebase in a mixed state.
- **CSS text wrapping needs several properties together**: `word-break: break-word` + `overflow-wrap: anywhere` + `max-width: 100%` + `overflow-x: hidden` on both content and container. One property alone is usually not enough with frameworks that nest many elements.

## Environment Variables

Full list: `docs/reference/environment-vars.md`. Set in `.env` (see `.env.example`). The ones you will actually reach for: `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CLAUDE_API_KEY`, `OLLAMA_BASE_URL`, `SAIVERSE_LOG_LEVEL`, `SAIVERSE_SEA_TRACE`, `SAIMEMORY_EMBED_MODEL`, `SAIVERSE_DB_BACKUP_ON_START`, `SAIVERSE_GATEWAY_WS_URL`.

## Documentation Map

- `docs/overview/landscape.md` — 概念の俯瞰地図（何があってどう繋がるか）
- `docs/overview/roadmap_status.md` — 進捗マップ（何が予定され、いまどこにいるか）
- `docs/overview/in_flight.md` — 進行中案件の台帳
- `docs/overview/architecture_health.md` — サブシステム別の負債監査
- `docs/concepts/` — 各概念の開発者向けリファレンス（索引は `concepts/README.md`）
- `docs/reference/` — DB スキーマ、API、ツール、env var、スクリプト
- `docs/developer-guide/` — コントリビューション・プロジェクト構造・ツール/Playbook 追加・テスト
- `docs/session_reflection_*.md` — 過去セッションの教訓
- `README.md` — setup and usage

## Quick Reference

**Create a persona**: frontend UI, or ask Genesis in 創造の祭壇.

**Move a persona**: `OccupancyManager.move_entity(entity_id, entity_type, from_id, to_id)`.

**Add a tool**: define in `builtin_data/tools/` (or `~/.saiverse/user_data/tools/`) with a `schema()` returning `ToolSchema` plus a same-named callable; it registers on startup. Subdirectories need `schema.py` with `schemas()`. To make it reachable by a persona, set `spell=True` or reference it from a Playbook TOOL node — **not** the legacy `BuildingToolLink` table.

**Modify a playbook**: edit the JSON, then `python scripts/import_playbook.py --file <path>`. Or use the `save_playbook` tool, which validates the graph first.

**Playbook design rules**:

- **Meta-judgment dispatch is deterministic — there is no LLM router.** Which `meta_judgment_*` playbook runs is chosen in code by `MetaLayer._SITUATION_PLAYBOOK_MAP` (`saiverse/meta_layer.py`) from Track/persona state. The dispatched situation playbooks (`meta_judgment_running` / `idle_pending` / `idle_empty` / `alert` / `life_purpose`) use structured output: an LLM node with `response_schema` returns the decision, then a `tool` node applies the Track op. The base `meta_judgment.json` is a separate NL-monologue + `/spell` variant that is **not** in the dispatch map.
- **Decide tool arguments inside the playbook.** Include an LLM node that chooses arguments from available context. Canonical example: `builtin_data/playbooks/public/generate_image_playbook.json` — `decide_prompt` (LLM + `response_schema` → `output_key`) → `generate` (TOOL + `args_input`, `output_keys`) → `record` (MEMORIZE).
- **`args_input` value types**: strings resolve as state variable paths (`"gen_params.title"`). Non-strings are literals. For a **literal string**, use `{"$literal": "Anima.json"}` — without it, `"Anima.json"` is looked up as a state key and resolves to `None`.
- **Adding a new node field**: you **must** update the node definitions in `sea/playbook_models.py` (`LLMNodeDef`, `ToolNodeDef`, …) first. Otherwise `save_playbook` and `import_playbook.py` silently drop the field during Pydantic validation. Then re-import the affected playbooks and verify with `sqlite3 ~/.saiverse/user_data/database/saiverse.db "SELECT nodes_json FROM playbooks WHERE name='<name>'"`.

**Debug LLM calls**: `~/.saiverse/user_data/logs/{session}/llm_io.log` for I/O, `sea_trace.log` for playbook traces.

**Inspect a running world**: `scripts/inspect_world.py` is the entry point for investigating persona/world state — prefer it over raw sqlite or grep.

**Access persona memory**: the Memory Settings UI tab (semantic recall / browse). Files live at `~/.saiverse/personas/<id>/memory.db`.
