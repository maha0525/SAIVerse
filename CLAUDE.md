# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Notes for Claude Code

**Language**: Think in English, respond in Japanese. The repository owner prefers Japanese for communication.

**Local preferences**: If `CLAUDE.local.md` exists in the repository root, read it for additional context (names, personal preferences, etc.).

## Project Overview

SAIVerse is a multi-agent AI system where autonomous AI personas (agents) inhabit a virtual world composed of Cities and Buildings. The system features:

- Multiple LLM providers (OpenAI, Anthropic, Google Gemini, Ollama, llama.cpp server) with automatic fallback
- Persistent long-term memory using SAIMemory (SQLite)
- Inter-city travel: personas can dispatch to other SAIVerse instances via database-mediated transactions
- SEA (Self-Evolving Agent) framework: LangGraph-based playbook system for routing conversations and autonomous behavior
- Optional Discord gateway for real-time chat integration
- Next.js frontend with REST API backend

## Development Commands

### Database Setup

**⚠️ IMPORTANT: Database Safety ⚠️**

```bash
# Initialize NEW database (⚠️ DESTROYS existing data - requires confirmation)
python database/seed.py
# You will be prompted to type 'DELETE' to confirm

# Force initialization without confirmation (DANGEROUS - use in scripts only)
python database/seed.py --force

# SAFE: Update playbooks only (does NOT affect personas or other data)
python scripts/import_all_playbooks.py

# SAFE: Update playbooks with force update
python scripts/import_all_playbooks.py --force

# SAFE: Preview changes without making them
python scripts/import_all_playbooks.py --dry-run

# Run migrations (for schema changes - preserves data)
python database/migrate.py
```

**Safety Notes:**
- `seed.py` will **DELETE ALL DATA** including personas, conversations, and playbooks
- `import_all_playbooks.py` is **SAFE** - only updates playbooks, preserves everything else
- `migrate.py` creates automatic backups before schema changes
- Always manually backup important data before destructive operations

### Running the System
```bash
# Start SDS (directory service) - optional, required for multi-city
python sds_server.py

# Launch a city instance
python main.py city_a
# city_a backend runs on http://127.0.0.1:8000 (API at /api)
# NOTE: only city_a is seeded by default (builtin_data/cities.json). A second
#       city (e.g. city_b on 9000) must be added to cities.json / DB first —
#       `python main.py city_b` errors out until then.
# Frontend (Next.js) runs on http://localhost:3000

# With custom options
python main.py city_a --db-file user_data/database/saiverse.db --sds-url http://127.0.0.1:8080
```

### Testing
```bash
# Run all tests
python -m pytest

# Run specific test file
python -m pytest tests/test_llm_clients.py

# Run with unittest
python -m unittest discover tests
```

### Linting
```bash
# Check for errors (undefined names, syntax errors, etc.)
ruff check .

# Auto-fix what can be fixed
ruff check --fix .

# Check specific file
ruff check path/to/file.py
```

**IMPORTANT for Claude Code**: After writing or modifying Python code, always run `ruff check` on the changed files before considering the task complete. This catches undefined variables (like `LOGGER` instead of `logging`), unused imports, and other common errors that would cause runtime failures.

### GPU Setup (Optional)

SAIMemory's embedding computation can be accelerated with NVIDIA CUDA:

```bash
# Install GPU dependencies (requires CUDA Toolkit + cuDNN pre-installed)
pip uninstall onnxruntime -y
pip install -r requirements-gpu.txt

# Verify CUDA is available
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
# Should include 'CUDAExecutionProvider'
```

**Environment variable control**:
- `SAIMEMORY_EMBED_CUDA=1` - Force GPU
- `SAIMEMORY_EMBED_CUDA=0` - Force CPU
- Unset - Auto-detect (use GPU if available)

**Files involved**:
- `sai_memory/memory/recall.py` - Embedder class with CUDA detection
- `requirements-gpu.txt` - GPU-specific dependencies
- `docs/getting-started/gpu-setup.md` - Full setup guide

### Test Environment (Isolated Backend Testing)

For testing the backend without affecting production data, use the isolated test environment:

```bash
# Setup test environment (creates test_data/ directory)
python test_fixtures/setup_test_env.py

# Start test server (port 18000)
./test_fixtures/start_test_server.sh

# Run API tests
python test_fixtures/test_api.py         # Full test (includes LLM calls)
python test_fixtures/test_api.py --quick # Quick test (no LLM calls)

# Reset database only
python test_fixtures/setup_test_env.py --reset-db

# Clean rebuild
python test_fixtures/setup_test_env.py --clean
```

**Test environment structure:**
- `test_fixtures/definitions/test_data.json` - Test data definitions (git-tracked)
- `test_data/` - Generated test data directory (gitignored)
- Environment variables: `SAIVERSE_HOME=test_data/.saiverse`, `SAIVERSE_USER_DATA_DIR=test_data/user_data`

**Important for AI agents:**
- Always use `--quick` mode for fast verification without LLM costs
- The chat API returns streaming NDJSON responses
- User must have `CURRENT_BUILDINGID` set for chat tests to work
- Personas need `LIGHTWEIGHT_MODEL` set for router nodes

### Backup and Recovery

**Automatic Backups (Recommended)**

SAIVerse automatically backs up both saiverse.db and persona memory.db files on startup:

- **saiverse.db**: Backed up to `~/.saiverse/user_data/database/saiverse.db_backup_YYYYMMDD_HHMMSS_mmm.bak`
  - Keeps last 10 backups by default (configurable via `SAIVERSE_DB_BACKUP_KEEP`)
  - Enable/disable: `SAIVERSE_DB_BACKUP_ON_START=true` (enabled by default)

- **memory.db**: Backed up using rdiff-backup to `~/.saiverse/backups/saimemory_rdiff/<persona_id>/`
  - Incremental backups with full history
  - Enable/disable: `SAIMEMORY_BACKUP_ON_START=true` (enabled by default)

**Manual Backup Scripts**

```bash
# Startup database backup
python -c "from database.backup import backup_saiverse_db; from database.paths import default_db_path; backup_saiverse_db(default_db_path())"

# Manual persona memory backup (requires rdiff-backup)
python scripts/backup_saimemory.py persona_id --output-dir ~/.saiverse/backups/

# Import legacy JSON logs to SAIMemory
python scripts/import_persona_logs_to_saimemory.py --persona air_city_a

# Migrate data to new user_data structure
python scripts/migrate_to_user_data.py --dry-run  # Preview
python scripts/migrate_to_user_data.py             # Execute
```

## Architecture

### Core Components

**SAIVerseManager** (`saiverse/saiverse_manager.py`)
- Central orchestrator for the entire world
- Manages all PersonaCore and Building instances in memory
- Polls `VisitingAI` and `ThinkingRequest` tables for inter-city coordination
- Delegates movement operations to OccupancyManager
- Handles SDS registration and heartbeat

**PersonaCore** (`persona/core.py`)
- The "soul" of each AI persona
- Its "Pulse" (cognition→decision→action cycle) is driven by the SEA runtime — execution entry is `SAIVerseManager.run_sea_user` / `run_sea_auto` → `PulseController` → `SEARuntime` (there is **no** `run_pulse()` method)
- Integrates with SAIMemory, emotion module, and task storage
- Conversation flow is driven by SEA runtime (playbook-based)

**SEARuntime** (`sea/runtime.py`)
- Executes playbooks (workflow graphs) for conversation routing using LangGraph
- Two meta-playbooks: `meta_user` (handles user input) and `meta_auto` (autonomous pulse)
- Playbooks are JSON files in `builtin_data/playbooks/` (or `~/.saiverse/user_data/playbooks/`) or stored in DB `playbooks` table
- **Lightweight model support**: an LLM node runs on either the persona's `DEFAULT_MODEL` or `LIGHTWEIGHT_MODEL`. Which one is derived from the line's **aspect** (CONVERSATION/WORKER/AUTONOMOUS/META → model tier; see `sea/pulse_context.py`), not a per-node field. (The old per-node `model_type` field is deprecated — it only survives in `builtin_data/playbooks/archive/`.)
  - Each persona has two model settings: `DEFAULT_MODEL` (normal) and `LIGHTWEIGHT_MODEL` (optional)
  - If `LIGHTWEIGHT_MODEL` is not set, system falls back to environment variable `SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL` or `BUILTIN_DEFAULT_LITE_MODEL` (defined in `saiverse/model_defaults.py`)
  - Persona model priority: chat UI override > persona `DEFAULT_MODEL` (DB) > env `SAIVERSE_DEFAULT_MODEL` > built-in default (see `saiverse/model_defaults.py`).
  - Lightweight tier is used for sub-line/worker and autonomous judgment; default tier for main-line conversation and tool parameter generation

**OccupancyManager** (`saiverse/occupancy_manager.py`)
- Handles all entity movement (users, AI personas, visitors)
- Enforces building capacity limits
- Updates `BuildingOccupancyLog` and in-memory state

**ConversationManager** (`saiverse/conversation_manager.py`) — **legacy / no-op**
- Old autonomous-conversation driver. Superseded by `AutonomyManager` (per-persona ~50min tick) + `SubLineScheduler` (`saiverse/pulse_scheduler.py`, ~5s poll) + `track_autonomous` playbook (2026-05-01 cognitive-model migration). Class removal is a pending cleanup (see landscape §9).

**RemotePersonaProxy** (`saiverse/remote_persona_proxy.py`)
- Lightweight proxy for visiting personas from other cities
- Delegates thinking to home city via `/persona-proxy/{id}/think` API

### Data Flow

**User Interaction**: UI → SAIVerseManager → PersonaCore → LLM + Tools → ActionHandler → SAIMemory + BuildingHistory

**Autonomous Pulse**: AutonomyManager / SubLineScheduler → PulseController → SEARuntime → think/speak nodes → SAIMemory

**Inter-City Travel** (DB-mediated, not direct API calls):
1. Source city writes `VisitingAI` record with status='requested'
2. Destination city polls DB, finds request, creates RemotePersonaProxy, updates status='accepted'/'rejected'
3. Source city polls DB, sees acceptance, sets persona IS_DISPATCHED=True
4. Proxy forwards thinking requests to home city's API server via `/persona-proxy/{id}/think`

### Memory Stack

**SAIMemory** (`sai_memory/`, `saiverse_memory/adapter.py`)
- SQLite-based log storage per persona in `~/.saiverse/personas/<id>/memory.db`
- Stores messages with tags (conversation, internal, task, summary)
- Supports thread switching, tag filtering, time-based queries

**Task Storage** (`persona/tasks/storage.py`)
- Per-persona `tasks.db` in `~/.saiverse/personas/<id>/`
- Stores tasks, steps, and history for task management tools

## Model & Provider Configuration

Models and providers are stored as individual JSON files. The 3-layer priority
applies to both: `~/.saiverse/user_data/{providers,models}/` (highest) >
`expansion_data/<addon>/{providers,models}/` > `builtin_data/{providers,models}/`.

See `docs/intent/model_provider_management.md` for the full design.
See `docs/custom_providers.md` for end-user setup of Kimi / LM Studio /
llama.cpp server etc.

### Providers

Providers describe how to connect to an LLM backend (protocol, base URL,
API key environment variable). Defined in `builtin_data/providers/*.json`
and `~/.saiverse/user_data/providers/*.json`.

**Example** (custom OpenAI-compatible provider):
```json
{
  "id": "lmstudio",
  "display_name": "LM Studio (local)",
  "protocol": "openai_compat",
  "base_url": "http://localhost:1234/v1",
  "api_key_env": "LMSTUDIO_API_KEY"
}
```

**Protocols**:
- `openai_compat` — OpenAI-compatible API (LM Studio, llama.cpp server, Kimi, etc.)
- `ollama_compat` — Ollama-compatible API
- `anthropic_native`, `gemini_native`, `xai_native`,
  `nvidia_nim`, `openai_codex` — builtin only (require code support in `llm_clients/`)

UI from "モデル管理 > プロバイダ" tab can create only `openai_compat` and
`ollama_compat` providers. Other protocols are builtin-only.

### Models

Models reference a provider via `provider_ref` (recommended) or carry the
provider's connection info inline in the legacy `provider` + `base_url` form.

**Example** (using provider_ref — preferred):
```json
{
  "model": "qwen2.5-72b-instruct",
  "display_name": "Qwen 2.5 72B (LM Studio)",
  "provider_ref": "lmstudio",
  "context_length": 32768,
  "parameters": { "temperature": { "type": "slider", "min": 0, "max": 2, "default": 0.7 } }
}
```

**Example** (legacy direct fields — still supported for backwards compat):
```json
{
  "model": "mistralai/mistral-large-3-675b-instruct-2512",
  "display_name": "Mistral Large 3 (NIM)",
  "provider": "openai",
  "context_length": 128000,
  "base_url": "https://integrate.api.nvidia.com/v1",
  "api_key_env": "NVIDIA_API_KEY"
}
```

**Resolution order** (`saiverse/model_configs.py:_resolve_provider_ref`):
1. Direct fields on the model JSON (`base_url`, `api_key_env`, etc.) win
2. If `provider_ref` is set, missing fields are inherited from the provider
3. If neither, the legacy `provider` field is used as-is

**Key model fields**:
- `model`: API model ID used in calls (required)
- `display_name`: Shown in UI dropdowns
- `provider_ref` / `provider`: Provider identifier (use `provider_ref` for new models)
- `context_length`: Context window size
- `convert_system_to_user`: Wrap system messages in `<system>` tags (Nvidia NIM, etc.)
- `structured_output_backend`: `xgrammar` or `outlines` (Nvidia NIM)
- `parameters`: UI-configurable parameters spec
- `cache`: Cache settings (Anthropic explicit / Gemini-OpenAI implicit)
- `pricing`: Cost calculation rates

### Adding a New Model or Provider

**Recommended (UI)**: グローバル設定 > "モデル管理" タブから:
- プロバイダタブ: 新規追加 → プロトコル選択 → base_url / api_key_env 入力 → 接続テスト
- モデルタブ: 新規追加 → JSON で全フィールド編集 → 保存

**Manual (file)**: Place JSON files directly under
`~/.saiverse/user_data/{providers,models}/`. Builtin files in `builtin_data/`
should not be edited directly — UI editing creates a user_data override on
top of the builtin instead.

**Reload**: After file edits, call `POST /api/config/reload-models` and
`POST /api/providers/reload`, or restart the application.

**Editing builtin via UI**: Saving a builtin model/provider from the UI creates
a user_data override (the builtin is never modified). Removing the user_data
file via "削除" restores the builtin.

**Saving from chat**: ChatOptions has "別名で保存" (save as new model) and
"上書き保存" (overwrite). For builtin models, "上書き保存" creates a user_data
override; the builtin file is preserved.

## Directory Structure

### Repository Root
```
SAIVerse/
├── main.py                 ← Main entry point
├── sds_server.py           ← SDS entry point
├── setup.bat / setup.sh    ← User setup scripts
├── start.bat / start.sh    ← Launch scripts
├── update.bat              ← Update script
│
├── saiverse/               ← Core package (managers, configs, utilities)
├── api/                    ← FastAPI routes
├── database/               ← DB models, session, migration
├── llm_clients/            ← LLM provider clients
├── manager/                ← SAIVerseManager mixins
├── persona/                ← PersonaCore
├── sea/                    ← SEA runtime & playbooks
├── tools/                  ← Tool registry
├── sai_memory/             ← SAIMemory
├── saiverse_memory/        ← Memory adapter
├── phenomena/              ← Phenomena system
├── builtin_data/           ← Built-in defaults (git tracked)
├── expansion_data/         ← User-installed expansion packs (gitignored)
├── frontend/               ← Next.js frontend
├── scripts/                ← Utility scripts
└── tests/                  ← Test suite
```

### Expansion Data (`expansion_data/`)
A repository-local directory for user-installed expansion packs (tools, phenomena, models, playbooks). Created by `setup.bat`/`setup.sh` and gitignored. Users can git clone tool packages here.

```
expansion_data/
├── some_tool_pack/         ← git clone'd tool package
│   ├── tools/
│   │   ├── my_tool.py
│   │   └── complex_tool/schema.py
│   ├── phenomena/
│   ├── playbooks/public/
│   └── models/
└── another_pack/
    └── tools/
```

### User Data (`~/.saiverse/`)
User data is stored outside the repository in `~/.saiverse/` (or `SAIVERSE_HOME` env var):

```
~/.saiverse/
├── user_data/              ← User customizations (highest priority)
│   ├── tools/              ← Custom tools (priority over all)
│   ├── phenomena/          ← Custom phenomena
│   ├── playbooks/          ← Custom playbooks
│   ├── models/             ← Custom model configs
│   ├── database/           ← SQLite database (saiverse.db)
│   ├── prompts/            ← Custom prompts
│   ├── icons/              ← User-uploaded avatars
│   └── logs/               ← Session logs
├── personas/<id>/          ← Per-persona memory (memory.db, tasks.db)
├── cities/<city>/          ← City/building logs
├── image/                  ← Uploaded images
├── documents/              ← Uploaded documents
└── backups/                ← Database backups
```

**Priority** (3 levels): When loading resources: `user_data/` (highest) > `expansion_data/` (middle) > `builtin_data/` (lowest). This allows users to override expansion packs, and expansion packs to override built-in defaults.

**Migration**: On startup, `main.py` automatically migrates legacy `user_data/` (in-repo) to `~/.saiverse/user_data/`. Override with `SAIVERSE_USER_DATA_DIR` env var for testing.

## Key Files and Patterns

### Database Schema (`database/models.py`)
- **User**: login state, current location
- **City**: UI_PORT, API_PORT, online mode flag
- **Building**: capacity, system prompt, auto pulse interval
- **AI**: home city, system prompt, emotion state, ACTIVITY_STATE (Stop/Sleep/Idle/Active), IS_DISPATCHED flag, DEFAULT_MODEL
- **BuildingOccupancyLog**: tracks entry/exit timestamps
- **VisitingAI**: manages inter-city move transactions (status: requested/accepted/rejected)
- **ThinkingRequest**: queues remote thinking calls (status: pending/processed/error)
- **Tool** + **BuildingToolLink**: legacy table for associating tools with buildings — **currently unused**. Tools reach personas via Spell (`spell=True`) or Playbook TOOL nodes, not this table.
- **Blueprint**: templates for creating new personas
- **Playbook**: stores SEA playbook schemas and nodes

### LLM Integration (`llm_clients/`, `saiverse/llm_router.py`)
- Factory pattern: `get_llm_client(model_name, config)` returns provider-specific client
- Providers: OpenAI (`openai.py`), Anthropic (`anthropic.py`), Gemini (`gemini.py`), Ollama (`ollama.py`)
- Ollama auto-probes localhost and falls back to Gemini 2.0 Flash if unreachable
- llama.cpp server: Uses `llama_server.py` to auto-launch and manage llama.cpp server processes. Configured via `llama_server` field in model JSON (see `builtin_data/models/llama-cpp-server-template.json`)
- `llm_router.py`: Uses Gemini 2.0 Flash to decide whether to call tools (returns JSON with call/tool/args)
- Model configs in `models.json`: defines provider, context_length, image support, thinking_type/budget for Anthropic

### Tools (`tools/`)
- **Registry**: `tools/__init__.py` exports `TOOL_REGISTRY` dict (function_name → schema + callable)
- **Loading**: Tools are loaded from both `~/.saiverse/user_data/tools/` (priority) and `builtin_data/tools/`
- **Subdirectory support**: Tools can be organized in subdirectories with `schema.py` (e.g., git-cloned tool repos)
- **Context**: `tools/context.py` uses contextvars to inject persona/manager references during tool execution
- **Built-in tools** (`builtin_data/tools/`):
  - `calculator.py`: safe AST-based expression evaluator
  - `image_generator.py`: Gemini 2.5 Flash Image API
  - `item_*.py`: pickup/place/use item in building inventory
  - `task_*.py`: task_request_creation, task_change_active, task_update_step, task_close
  - `thread_switch.py`: switch SAIMemory active thread
  - `memory_recall.py`: semantic recall via MemoryCore
  - `save_playbook.py`: persist new playbook to DB

### Action Handler (`saiverse/action_handler.py`)
- Parses `::act ... ::end` blocks from LLM responses
- Executes special actions: move, pickup_item, create_persona, summon, dispatch_persona, use_item

## Intent Documents

Each feature/subsystem has an **Intent Document** in `docs/intent/` that describes WHY it was built, what invariants it must maintain, and the design decisions behind it.

### Workflow

1. **Before implementing**: Check if `docs/intent/<feature>.md` exists for the target feature
2. **If it exists**: Read it before writing any code
3. **If it doesn't exist**: Create it first using this process:
   - Read related code to understand the full picture
   - Draft the document
   - Interview the user about unclear points
   - Revise based on the interview
   - User reviews and gives final feedback → document is finalized
4. **Then implement** the feature with the intent document as guide

### Purpose

Intent documents record the "why" that code alone cannot express. They prevent well-intentioned changes from violating design assumptions (e.g., increasing Stelis anchor display to 50 messages defeats the purpose of context isolation).

## Important Conventions

### Documentation Maintenance (keep docs in sync with code)

**Docs are part of the change surface, not an afterthought.** When a change alters a fact a doc states, update that doc in the *same* change:

- **Adding / moving / renaming modules or top-level directories** → update `docs/developer-guide/project-structure.md` (it drifts fastest — it describes the file tree)
- **New or changed concept / mechanism** → update the relevant `docs/concepts/*.md` and the map `docs/overview/landscape.md`
- **Deprecating / killing a concept** → record it in `docs/overview/landscape.md` §9 (死んだ概念) and remove *actionable* references to it elsewhere (don't leave "do X via the dead thing" instructions)
- **New / removed feature, playbook node field, env var, API route, DB table, script** → update the matching `docs/reference/*` / `docs/features/*`

**Auto-generated reference docs**: `docs/reference/{tool-catalog,api-endpoints,database-schema}.md` are generated from code (they carry an `AUTO-GENERATED` banner — **never hand-edit them**). After changing tools (`builtin_data/tools/`), API routes (`api/routes/`), or DB models (`database/models.py`), **run `gen_reference_docs.bat` before committing** (non-Windows: `python scripts/gen_reference_docs.py`) and include the regenerated docs in the same commit. This is a developer-side step only — it is intentionally **not** wired into end-user `update.*` (that would create local git diffs on pull) and there is **no CI gate** (regenerating after the fact is harmless). `... --check` reports drift if you want to verify locally. The other reference docs (providers / phenomena / playbooks / env vars / `saiverse://` URI) are hand-maintained.

If unsure whether any doc references what you changed, `grep docs/` for the symbol / name before finishing. Stale docs send the next agent and the user to the wrong entry point — that is a real cost, not a cosmetic one. Prefer updating docs proactively even when not asked.

### Code Changes
- **Before making changes**: Review recent session reflections in `docs/session_reflection_*.md` to avoid repeating mistakes

- **⚠️ NEVER GUESS ATTRIBUTE/METHOD NAMES (CRITICAL) ⚠️**:
  **ALWAYS READ THE ACTUAL CODE BEFORE USING EXISTING OBJECTS' ATTRIBUTES OR METHODS.**

  **DO NOT**:
  - Assume an object has a `provider` attribute without checking
  - Guess that a building ID is stored in `building_id` instead of `current_building_id`
  - Write `persona.some_attribute` without verifying it exists in `persona/core.py`
  - Call `llm_client.some_method()` without checking `llm_clients/base.py`
  - Reference `db_model.COLUMN_NAME` without reading `database/models.py`

  **ALWAYS DO**:
  1. **Read the source code** - Open the file and find the actual definition (5 seconds)
  2. **Verify attribute names** - Check `__init__` or class definition for exact names
  3. **Check method signatures** - Read the actual parameters, don't guess
  4. **Use Grep/Read tools** - Search for existing usage patterns in the codebase

  **Example - WRONG**:
  ```python
  # Guessing attribute names without verification
  provider = persona.provider  # Does this exist?
  building = persona.building_id  # Or is it current_building_id?
  ```

  **Example - CORRECT**:
  ```python
  # Step 1: Read persona/core.py to verify attributes
  # Step 2: Found: self.current_building_id (line 116)
  # Step 3: Use the verified name
  building = persona.current_building_id
  ```

  **This rule applies to**:
  - PersonaCore attributes (`persona/core.py`)
  - Database model columns (`database/models.py`)
  - LLM client methods (`llm_clients/base.py`, `llm_clients/*.py`)
  - Manager methods (`manager/*.py`, `saiverse/saiverse_manager.py`)
  - Any existing class or object in the codebase

  **Only guess/invent names for NEW code you are creating.**
  **For EXISTING code, READ FIRST, then use the exact names you find.**

- **Debugging mindset (CRITICAL)**:
  1. **Logs and console output are the PRIMARY source of truth**: Always check terminal logs, browser console, and network tab FIRST before making changes
  2. **Never guess or assume**: If something doesn't work, identify EXACTLY what doesn't work by checking observable facts (logs, DOM inspection, network requests)
  3. **One problem at a time**: Don't switch approaches until you understand WHY the current approach failed
  4. **Ask "What don't I know?"**: If unclear, identify the missing information and how to obtain it (add logging, inspect DOM, check documentation) instead of guessing
  5. **Avoid speculative fixes**: Don't try multiple approaches hoping one works. Understand the root cause first.

- **When debugging UI issues**:
  1. **Listen carefully**: Pay close attention to what the user is actually doing (e.g., "sidebar button" vs "home screen button")
  2. **Gather observable data first**: Add logging, check terminal output, check browser console BEFORE making changes
  3. **Understand the working case**: If something works in one scenario but not another, investigate the DIFFERENCE, don't assume the cause
  4. **One change at a time**: Make focused changes that can be verified, not multiple speculative fixes
  5. **Verify assumptions**: Don't assume "timing issue" or "selector issue" - confirm with logs
  6. **Use browser DevTools effectively**:
     - Console: Check for errors, test selectors directly (`document.querySelector('#element')`)
     - Elements: Inspect actual DOM structure and CSS
     - Network: Verify request URLs and responses
- **When touching external APIs**: Always check official docs first (especially Gemini structured output limitations)
- **Playbook modifications**: Validate that `next` node pointers form valid graphs (no accidental loops). After editing JSON files in `builtin_data/playbooks/`, always run `python scripts/import_playbook.py --file <path>` to import the changes into the database
- **Database changes**: Write migration in `database/migrate.py`, test with `--db-file` on copy first

### Memory and History
- Building chat history: stored in memory, logged to `~/.saiverse/cities/<city>/buildings/<building>/log.json`
- SAIMemory logs: appended via `SAIMemoryAdapter.append_building_message()` / `append_persona_message()` with tags
- Pulse internal thoughts: tag='internal', include pulse_id for grouping
- User conversations: tag='conversation'

### Branch Strategy
- **main**: Stable, tested releases
- **develop**: Integration branch (default PR target). Feature branches merge here first
- **feature/\***: Individual feature branches, created from develop
- **Flow**: `feature/*` → PR → `develop` → (tested) → PR → `main`

### Testing
- Tests use `unittest` framework (pytest also works)
- Mock LLM clients when testing conversation logic
- DB tests should use temporary databases
- See `docs/developer-guide/testing.md` and `docs/test_environment.md` for test setup and scenarios

### Logging

All session logs are written under `~/.saiverse/user_data/logs/{YYYYMMDD_HHMMSS}/`:

| File | Logger | Purpose |
|------|--------|---------|
| `backend.log` | root | Application-wide log + console mirror |
| `llm_io.log` | `saiverse.llm` | LLM API request/response I/O (JSON) |
| `sea_trace.log` | `saiverse.sea_trace` | SEA playbook node execution trace |
| `timeout_diagnostics.log` | `saiverse.timeout` | Timeout event diagnostics |

- Per-persona logs: `~/.saiverse/personas/<id>/log.json`, `conscious_log.json`
- Set `SAIVERSE_LOG_LEVEL=DEBUG` in `.env` for verbose output
- SEA trace: set `SAIVERSE_SEA_TRACE=1` to enable detailed playbook debug logging
- **Debugging tip**: When `LOGGER.debug()` with `extra={}` doesn't show details, use `print()` to output directly to stdout. The logger formatter may not be configured to display `extra` fields.
- **Browser console logging**: JavaScript `console.debug()` is filtered by default in most browsers. Use `console.log()` for debug messages that should always be visible. In Chrome/Edge, open DevTools Console and set log level filter to "Verbose" or "All levels" to see `console.debug()` output.

### Common Pitfalls
- **Do not run `database/seed.py` carelessly** - it wipes the database
- **Inter-city travel is NOT via direct API calls** - it's DB-mediated through VisitingAI table polling
- **Gemini structured output does not support `additionalProperties`** - keep response schemas simple
- **Gemini context window is very large (1M+ tokens)** - Do not assume large context is the cause of errors. Gemini handles 100K+ tokens routinely. The system is designed to work with large conversation histories.
- **Playbook node transitions**: always verify `next` pointers form valid DAGs
- **When refactoring**: complete the entire change or revert; do not leave codebase in mixed state
- **Asymmetric bugs indicate implementation mismatch**: If a bug occurs in scenario A but not in scenario B (despite similar logic), the cause is usually an implementation difference, not a timing/race condition. Compare code paths side-by-side to find where they diverge.
- **CSS text wrapping requires multiple layers**: For reliable wrapping of long URLs/strings in CSS, combine: `word-break: break-word`, `overflow-wrap: anywhere`, `max-width: 100%`, and `overflow-x: hidden` on both content and container elements. A single property is often insufficient, especially with frameworks that inject many nested elements.

### Setup/Update Script Parity
The following script groups **MUST** maintain the same logic. When modifying one, always update the others:

- **Update scripts**: `update.bat`, `update.sh`, `scripts/self_update.py` — all three perform the same update flow (git pull with stash retry, pip install, DB migration, playbook import, frontend update). Changes to update logic (e.g. error handling, stash behavior, new update phases) must be applied to all three.
- **Setup scripts**: `setup.bat`, `setup.sh` — both perform the same setup flow (Python/Node check, venv creation, pip install, npm install, DB seed, git init, .env creation, SearXNG, embedding model download). Changes to setup steps must be applied to both.

When adding or modifying a step, check all related scripts for consistency. The `pip` command must always be invoked as `python -m pip` (not bare `pip`) to avoid Device Guard issues on Windows.

## Dependencies

Key packages (see `requirements.txt`):
- `google-genai>=1.26.0` (Gemini API)
- `openai==1.97.0` (OpenAI + Anthropic)
- `fastapi==0.116.1`, `uvicorn==0.35.0` (API server)
- `fastembed>=0.7.3` (SAIMemory embeddings)
- `discord.py>=2.4.0` (optional Discord gateway)

Embeddings models in `sbert/` (e.g., `intfloat/multilingual-e5-small`) are used if present, otherwise downloaded on first run.

## Environment Variables

Critical settings (see `.env.example`):
- `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CLAUDE_API_KEY`, `OLLAMA_BASE_URL`
- `SDS_URL` (default: http://127.0.0.1:8080)
- `SAIVERSE_LOG_LEVEL` (DEBUG/INFO/WARNING)
- `SAIMEMORY_EMBED_MODEL` (e.g., intfloat/multilingual-e5-small)
- `SAIMEMORY_BACKUP_ON_START=true` (auto-backup persona memory.db on startup)
- `SAIVERSE_DB_BACKUP_ON_START=true` (auto-backup saiverse.db on startup, **recommended**)
- `SAIVERSE_DB_BACKUP_KEEP=10` (number of saiverse.db backups to keep)
- `SAIVERSE_GATEWAY_WS_URL`, `SAIVERSE_GATEWAY_TOKEN` (Discord gateway)

## Documentation

- `docs/overview/landscape.md`: 概念の俯瞰地図（何があってどう繋がるか）
- `docs/overview/roadmap_status.md`: 進捗マップ（何が予定され、いまどこにいるか）
- `docs/concepts/`: 各概念の開発者向けリファレンス（実装への入口。索引は `concepts/README.md`）
- `docs/reference/database-schema.md`: テーブル定義とスキーマ
- `docs/reference/api-endpoints.md`: REST API 一覧
- `docs/developer-guide/`: コントリビューション・プロジェクト構造・ツール/Playbook 追加・テスト
- `docs/session_reflection_*.md`: lessons learned from development sessions (debugging approaches, etc.)
- `README.md`: comprehensive setup and usage guide

## Quick Reference

**Create new persona**: Use the frontend UI or have user ask Genesis in "創造の祭壇" building

**Move persona between buildings**: `OccupancyManager.move_entity(entity_id, entity_type, from_id, to_id)` (do not call PersonaCore methods directly)

**Add new tool**: Define in `builtin_data/tools/` (or `~/.saiverse/user_data/tools/` for custom tools) with a `schema()` function returning `ToolSchema` + a same-named callable; registers automatically on startup. Tools in subdirectories need a `schema.py` (with `schemas()`). To make it usable by a persona, either set `spell=True` (callable from plaintext via `/spell`) or reference it in a Playbook TOOL node — **not** the legacy `BuildingToolLink` table.

**Modify playbook**: Edit JSON in `builtin_data/playbooks/` or `~/.saiverse/user_data/playbooks/`, then run `python scripts/import_playbook.py --file <path>` to import to database. Alternatively, use `save_playbook` tool (validates graph before saving)

**Playbook design philosophy**:
- **Meta-judgment dispatch is deterministic (no LLM router)**: which `meta_judgment_*` playbook runs is selected in code by `MetaLayer._SITUATION_PLAYBOOK_MAP` (`saiverse/meta_layer.py`) from Track/persona state. The dispatched situation playbooks (`meta_judgment_running` / `idle_pending` / `idle_empty` / `alert` / `life_purpose`) use structured output: an LLM node with `response_schema` returns the decision (action/decision/create enums), then a `tool` node applies the Track op. (The base `meta_judgment.json` is a separate NL-monologue-+-`/spell` variant that is NOT in the dispatch map.)
- **Arguments decided inside playbooks**: Each playbook should include an LLM node that decides the tool arguments based on available context (inventory, building items, conversation history, etc.). This approach provides better flexibility and leverages the full context within the playbook.
- **Reference implementation**: See `builtin_data/playbooks/public/generate_image_playbook.json` for the canonical pattern:
  1. `decide_prompt` LLM node with `response_schema` to structure output (→ `output_key: "gen_params"`)
  2. `generate` TOOL node with `args_input` mapping state variables to tool parameters (+ `output_keys` for tuple returns)
  3. `record` MEMORIZE node to save results to SAIMemory
- **`args_input` value types**: String values are resolved as state variable paths (e.g., `"gen_params.title"`). Non-string values (int, bool, etc.) are used as literals. To pass a **literal string**, use `{"$literal": "value"}` syntax (e.g., `{"$literal": "Anima.json"}`). Without `$literal`, a string like `"Anima.json"` would be interpreted as a state key lookup and resolve to `None`.
- **Multi-value tool returns**: Tools returning tuples (e.g., `generate_image`) can use `output_keys` to expand values into multiple state variables. Example: `"output_keys": ["text", "snippet", "file_path", "metadata"]`
- **Adding new node fields**: When adding new fields to playbook nodes (e.g., `output_keys`, `response_schema_source`):
  1. **MUST update** `sea/playbook_models.py` node definitions (`LLMNodeDef`, `ToolNodeDef`, etc.) with the new field
  2. Without this, `save_playbook` tool and `import_playbook.py` will silently drop the field during Pydantic validation
  3. After updating the schema, **re-import all affected playbooks** using `python scripts/import_playbook.py --file <path>`
   4. Verify the field is stored in DB: `sqlite3 ~/.saiverse/user_data/database/saiverse.db "SELECT nodes_json FROM playbooks WHERE name='<playbook_name>'"`

**Debug LLM calls**: Check `~/.saiverse/user_data/logs/{session}/llm_io.log` for LLM I/O, `sea_trace.log` for playbook execution traces

**Access persona memory**: Use the Memory Settings UI tab (semantic recall / browse). Per-persona memory lives in `~/.saiverse/personas/<id>/memory.db`.
