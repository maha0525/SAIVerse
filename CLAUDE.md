# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Notes for Claude Code

**Language**: Think in English, respond in Japanese. The repository owner prefers Japanese for communication.

**Local preferences**: If `CLAUDE.local.md` exists in the repository root, read it for additional context (names, personal preferences, etc.).

## Project Overview

SAIVerse is a multi-agent AI system where autonomous AI personas (agents) inhabit a virtual world composed of Cities and Buildings. The system features:

- Multiple LLM providers (OpenAI, Anthropic, Google Gemini, Ollama, llama.cpp) with automatic fallback
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
# city_b backend runs on http://127.0.0.1:9000 (API at /api)
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

# Ingest logs into Qdrant for semantic recall
python scripts/ingest_persona_log.py persona_id

# Query memory semantically
python scripts/recall_persona_memory.py persona_id "query text"

# Process task requests
python scripts/process_task_requests.py --base ~/.saiverse/personas

# Visualize memory topics in browser
python scripts/memory_topics_ui.py

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
- `run_pulse()` executes autonomous "cognition→decision→action" cycles
- Integrates with SAIMemory, emotion module, action handler, and task storage
- Conversation flow is driven by SEA runtime (playbook-based)

**SEARuntime** (`sea/runtime.py`)
- Executes playbooks (workflow graphs) for conversation routing using LangGraph
- Two meta-playbooks: `meta_user` (handles user input) and `meta_auto` (autonomous pulse)
- Playbooks are JSON files in `sea/playbooks/` or stored in DB `playbooks` table
- **Lightweight model support**: LLM nodes can specify `model_type: "lightweight"` to use a faster, cheaper model for simple tasks (e.g., router decisions)
  - Each persona has two model settings: `DEFAULT_MODEL` (normal) and `LIGHTWEIGHT_MODEL` (optional)
  - If `LIGHTWEIGHT_MODEL` is not set, system falls back to environment variable `SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL` or `gemini-2.5-flash-lite-preview-09-2025`
  - Persona model priority: chat UI override > persona `DEFAULT_MODEL` (DB) > env `SAIVERSE_DEFAULT_MODEL` > built-in `gemini-2.5-flash-lite-preview-09-25`.
  - Use lightweight models for router nodes and simple decision-making; use default models for complex reasoning and tool parameter generation

**OccupancyManager** (`saiverse/occupancy_manager.py`)
- Handles all entity movement (users, AI personas, visitors)
- Enforces building capacity limits
- Updates `BuildingOccupancyLog` and in-memory state

**ConversationManager** (`saiverse/conversation_manager.py`)
- Drives autonomous conversations in each building
- Periodically calls `run_pulse()` on occupants in round-robin fashion

**RemotePersonaProxy** (`saiverse/remote_persona_proxy.py`)
- Lightweight proxy for visiting personas from other cities
- Delegates thinking to home city via `/persona-proxy/{id}/think` API

### Data Flow

**User Interaction**: UI → SAIVerseManager → PersonaCore → LLM + Tools → ActionHandler → SAIMemory + BuildingHistory

**Autonomous Pulse**: ConversationManager → PersonaCore.run_pulse() → SEARuntime → think/speak nodes → SAIMemory

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

## Model Configuration

**Model Configuration** (`models/` directory, `saiverse/model_configs.py`)
- Model configurations are stored as individual JSON files in `models/` directory
- Each file represents one model with its provider, context length, and parameters
- Legacy `models.json` is supported as fallback for backward compatibility

**Model Config Structure**:
```json
{
  "model": "mistralai/mistral-large-3-675b-instruct-2512",
  "display_name": "Mistral Large 3 (NIM)",
  "provider": "openai",
  "context_length": 128000,
  "base_url": "https://integrate.api.nvidia.com/v1",
  "api_key_env": "NVIDIA_API_KEY",
  "convert_system_to_user": true,
  "structured_output_backend": "xgrammar",
  "parameters": { ... }
}
```

**Key Fields**:
- `model`: The actual model ID used in API calls (required)
- `display_name`: Human-readable name shown in UI dropdowns (optional, defaults to model ID)
- `provider`: LLM provider (`openai`, `anthropic`, `gemini`, `ollama`)
- `convert_system_to_user`: Wrap system messages in `<system>` tags for compatibility (Nvidia NIM, etc.)
- `structured_output_backend`: Backend for structured output (`xgrammar`, `outlines` for Nvidia NIM)
- `parameters`: UI-configurable parameters (temperature, top_p, max_tokens, etc.)

**Adding a New Model**:
1. Create a JSON file in `models/` (e.g., `models/my-model.json`)
2. Define `model`, `display_name`, `provider`, and other required fields
3. Restart the application to load the new config
4. Model will appear in all model selection dropdowns

**Migration Script**:
- `scripts/migrate_models_to_directory.py`: Migrates legacy `models.json` to `models/` directory structure

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
- **AI**: home city, system prompt, emotion state, INTERACTION_MODE (auto/user/sleep), IS_DISPATCHED flag, DEFAULT_MODEL
- **BuildingOccupancyLog**: tracks entry/exit timestamps
- **VisitingAI**: manages inter-city move transactions (status: requested/accepted/rejected)
- **ThinkingRequest**: queues remote thinking calls (status: pending/processed/error)
- **Tool** + **BuildingToolLink**: associates available tools with buildings
- **Blueprint**: templates for creating new personas
- **Playbook**: stores SEA playbook schemas and nodes

### LLM Integration (`llm_clients/`, `saiverse/llm_router.py`)
- Factory pattern: `get_llm_client(model_name, config)` returns provider-specific client
- Providers: OpenAI (`openai.py`), Anthropic (`anthropic.py`), Gemini (`gemini.py`), Ollama (`ollama.py`), llama.cpp (`llama_cpp.py`)
- Ollama auto-probes localhost and falls back to Gemini 2.0 Flash if unreachable
- llama.cpp: Directly loads GGUF models without external servers (requires `llama-cpp-python`). Falls back to Gemini on load failure if `fallback_on_error: true`
- `llm_router.py`: Uses Gemini 2.0 Flash to decide whether to call tools (returns JSON with call/tool/args)
- Model configs in `models.json`: defines provider, context_length, image support, thinking_type/budget for Anthropic
- For llama.cpp models: `model_path` (GGUF file path), `n_gpu_layers` (-1=all, 0=CPU only), `fallback_on_error` (default: true)
- See `docs/llama_cpp_integration.md` for detailed setup instructions

### Tools (`tools/`)
- **Registry**: `tools/__init__.py` exports `TOOL_REGISTRY` dict (function_name → schema + callable)
- **Loading**: Tools are loaded from both `~/.saiverse/user_data/tools/` (priority) and `builtin_data/tools/`
- **Subdirectory support**: Tools can be organized in subdirectories with `schema.py` (e.g., git-cloned tool repos)
- **Context**: `tools/context.py` uses contextvars to inject persona/manager references during tool execution
- **Built-in tools** (`builtin_data/tools/defs/` or `tools/defs/`):
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
- **Playbook modifications**: Validate that `next` node pointers form valid graphs (no accidental loops). After editing JSON files in `sea/playbooks/`, always run `python scripts/import_playbook.py --file <path>` to import the changes into the database
- **Database changes**: Write migration in `database/migrate.py`, test with `--db-file` on copy first

### Memory and History
- Building chat history: stored in memory, logged to `~/.saiverse/cities/<city>/buildings/<building>/log.json`
- SAIMemory logs: appended via `SAIMemoryAdapter.log_message()` with tags
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
- Check `docs/test_manual.md` for manual integration test scenarios (World Dive, Persona Genesis, etc.)

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

- `docs/architecture.md`: component diagram and data flow
- `docs/database_design.md`: table schemas and rationale
- `docs/test_manual.md`: manual test scenarios
- `docs/sea_integration_plan.md`: SEA framework integration roadmap
- `docs/roadmap.md`: future features
- `docs/session_reflection_*.md`: lessons learned from development sessions (debugging approaches, etc.)
- `README.md`: comprehensive setup and usage guide

## Quick Reference

**Create new persona**: Use the frontend UI or have user ask Genesis in "創造の祭壇" building

**Move persona between buildings**: `OccupancyManager.move_to(persona, building_id)` (do not call PersonaCore methods directly)

**Add new tool**: Define in `tools/defs/` (or `~/.saiverse/user_data/tools/` for custom tools), register automatically on startup. Tools in subdirectories need a `schema.py` file. Link to buildings via `BuildingToolLink` table or frontend UI

**Modify playbook**: Edit JSON in `builtin_data/playbooks/` or `~/.saiverse/user_data/playbooks/`, then run `python scripts/import_playbook.py --file <path>` to import to database. Alternatively, use `save_playbook` tool (validates graph before saving)

**Playbook design philosophy**:
- **Router simplicity**: The router node in meta playbooks is designed to run on lightweight LLMs. It should ONLY select which playbook to execute (enum selection), not decide complex arguments.
- **Arguments decided inside playbooks**: Each playbook should include an LLM node that decides the tool arguments based on available context (inventory, building items, conversation history, etc.). This approach provides better flexibility and leverages the full context within the playbook.
- **Reference implementation**: See `memory_recall_playbook.json` for the canonical pattern:
  1. `generate_query` LLM node with `response_schema` to structure output
  2. `recall` TOOL node with `args_input` mapping state variables to tool parameters
  3. `record` MEMORIZE node to save results to SAIMemory
- **Multi-value tool returns**: Tools returning tuples (e.g., `generate_image`) can use `output_keys` to expand values into multiple state variables. Example: `"output_keys": ["text", "snippet", "file_path", "metadata"]`
- **Adding new node fields**: When adding new fields to playbook nodes (e.g., `model_type`, `output_keys`):
  1. **MUST update** `sea/playbook_models.py` node definitions (`LLMNodeDef`, `ToolNodeDef`, etc.) with the new field
  2. Without this, `save_playbook` tool and `import_playbook.py` will silently drop the field during Pydantic validation
  3. After updating the schema, **re-import all affected playbooks** using `python scripts/import_playbook.py --file <path>`
   4. Verify the field is stored in DB: `sqlite3 ~/.saiverse/user_data/database/saiverse.db "SELECT nodes_json FROM playbooks WHERE name='<playbook_name>'"`

**Debug LLM calls**: Check `~/.saiverse/user_data/logs/{session}/llm_io.log` for LLM I/O, `sea_trace.log` for playbook execution traces

**Access persona memory**: Use `scripts/recall_persona_memory.py` or Memory Settings UI tab
