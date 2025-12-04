# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Notes for Claude Code

**Language**: Think in English, respond in Japanese. The repository owner prefers Japanese for communication.

**Local preferences**: If `CLAUDE.local.md` exists in the repository root, read it for additional context (names, personal preferences, etc.).

## Project Overview

SAIVerse is a multi-agent AI system where autonomous AI personas (agents) inhabit a virtual world composed of Cities and Buildings. The system features:

- Multiple LLM providers (OpenAI, Anthropic, Google Gemini, Ollama) with automatic fallback
- Persistent long-term memory using SAIMemory (SQLite) + MemoryCore (Qdrant vector DB)
- Inter-city travel: personas can dispatch to other SAIVerse instances via database-mediated transactions
- SEA (Self-Evolving Agent) framework: LangGraph-based playbook system for routing conversations and autonomous behavior
- Optional Discord gateway for real-time chat integration
- Gradio-based UI with World View, DB Manager, Task Manager, Memory Settings, and World Editor

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
python database/migrate.py --db database/data/saiverse.db
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
# city_a runs on http://127.0.0.1:8000 (UI) and port 8001 (API)
# city_b runs on http://127.0.0.1:9000 (UI) and port 9001 (API)

# With custom options
python main.py city_a --db-file database/data/saiverse.db --sds-url http://127.0.0.1:8080
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

### Backup and Recovery

**Automatic Backups (Recommended)**

SAIVerse automatically backs up both saiverse.db and persona memory.db files on startup:

- **saiverse.db**: Backed up to `database/data/saiverse.db_backup_YYYYMMDD_HHMMSS_mmm.bak`
  - Keeps last 10 backups by default (configurable via `SAIVERSE_DB_BACKUP_KEEP`)
  - Enable/disable: `SAIVERSE_DB_BACKUP_ON_START=true` (enabled by default)

- **memory.db**: Backed up using rdiff-backup to `~/.saiverse/backups/saimemory_rdiff/<persona_id>/`
  - Incremental backups with full history
  - Enable/disable: `SAIMEMORY_BACKUP_ON_START=true` (enabled by default)

**Manual Backup Scripts**

```bash
# Manual saiverse.db backup
python3 -c "from database.backup import backup_saiverse_db; from database.paths import default_db_path; backup_saiverse_db(default_db_path())"

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
```

## Architecture

### Core Components

**SAIVerseManager** (`saiverse_manager.py`)
- Central orchestrator for the entire world
- Manages all PersonaCore and Building instances in memory
- Polls `VisitingAI` and `ThinkingRequest` tables for inter-city coordination
- Delegates movement operations to OccupancyManager
- Handles SDS registration and heartbeat

**PersonaCore** (`persona/core.py`)
- The "soul" of each AI persona
- `run_pulse()` executes autonomous "cognition→decision→action" cycles
- Integrates with SAIMemory, emotion module, action handler, and task storage
- Note: On `sea_framework` branch, conversation flow is being migrated to SEA runtime

**SEARuntime** (`sea/runtime.py`)
- Executes playbooks (workflow graphs) for conversation routing using LangGraph
- Two meta-playbooks: `meta_user` (handles user input) and `meta_auto` (autonomous pulse)
- Playbooks are JSON files in `sea/playbooks/` or stored in DB `playbooks` table
- **Lightweight model support**: LLM nodes can specify `model_type: "lightweight"` to use a faster, cheaper model for simple tasks (e.g., router decisions)
  - Each persona has two model settings: `DEFAULT_MODEL` (normal) and `LIGHTWEIGHT_MODEL` (optional)
  - If `LIGHTWEIGHT_MODEL` is not set, system falls back to environment variable `SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL` or `gemini-2.5-flash-lite`
  - Use lightweight models for router nodes and simple decision-making; use default models for complex reasoning and tool parameter generation

**OccupancyManager** (`occupancy_manager.py`)
- Handles all entity movement (users, AI personas, visitors)
- Enforces building capacity limits
- Updates `BuildingOccupancyLog` and in-memory state

**ConversationManager** (`conversation_manager.py`)
- Drives autonomous conversations in each building
- Periodically calls `run_pulse()` on occupants in round-robin fashion

**RemotePersonaProxy** (`remote_persona_proxy.py`)
- Lightweight proxy for visiting personas from other cities
- Delegates thinking to home city via `/persona-proxy/{id}/think` API

### Data Flow

**User Interaction**: UI → SAIVerseManager → PersonaCore → LLM + Tools → ActionHandler → SAIMemory + BuildingHistory

**Autonomous Pulse**: ConversationManager → PersonaCore.run_pulse() → [SEARuntime (sea_framework branch)] → think/speak nodes → SAIMemory

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

**MemoryCore** (`memory_core/`)
- SBERT embeddings + Qdrant vector DB for semantic recall
- Two collections: `entries` (individual messages) and `topics` (clustered summaries)
- Located at `~/.saiverse/qdrant/` (embedded mode) or remote Qdrant server

**Task Storage** (`persona/tasks/storage.py`)
- Per-persona `tasks.db` in `~/.saiverse/personas/<id>/`
- Stores tasks, steps, and history for task management tools

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
- **Playbook** (on sea_framework branch): stores SEA playbook schemas and nodes

### LLM Integration (`llm_clients/`, `llm_router.py`)
- Factory pattern: `get_llm_client(model_name, config)` returns provider-specific client
- Providers: OpenAI (`openai.py`), Anthropic (`anthropic.py`), Gemini (`gemini.py`), Ollama (`ollama.py`)
- Ollama auto-probes localhost and falls back to Gemini 2.0 Flash if unreachable
- `llm_router.py`: Uses Gemini 2.0 Flash to decide whether to call tools (returns JSON with call/tool/args)
- Model configs in `models.json`: defines provider, context_length, image support, thinking_type/budget for Anthropic

### Tools (`tools/`)
- **Registry**: `tools/__init__.py` exports `TOOL_REGISTRY` dict (function_name → schema + callable)
- **Context**: `tools/context.py` uses contextvars to inject persona/manager references during tool execution
- **Built-in tools** (`tools/defs/`):
  - `calculator.py`: safe AST-based expression evaluator
  - `image_generator.py`: Gemini 2.5 Flash Image API
  - `item_*.py`: pickup/place/use item in building inventory
  - `task_*.py`: task_request_creation, task_change_active, task_update_step, task_close
  - `thread_switch.py`: switch SAIMemory active thread
  - `memory_recall.py`: semantic recall via MemoryCore
  - `save_playbook.py`: persist new playbook to DB (sea_framework branch)

### Action Handler (`action_handler.py`)
- Parses `::act ... ::end` blocks from LLM responses
- Executes special actions: move, pickup_item, create_persona, summon, dispatch_persona, use_item

### UI Structure (`ui/app.py`)
- Gradio app with tabs: World View, Autonomous Log, DB Manager, Task Manager, Memory Settings, World Editor
- `ui/world_view.py`: chat interface, building movement, persona summoning
- `ui/world_editor.py`: CRUD for cities/buildings/personas/tools, avatar upload, online/offline mode
- `ui/task_manager.py`: view tasks.db as DataFrame

## Important Conventions

### Code Changes
- **Before making changes**: Review recent session reflections in `docs/session_reflection_*.md` to avoid repeating mistakes
- **When debugging UI issues**:
  1. **Listen carefully**: Pay close attention to what the user is actually doing (e.g., "sidebar button" vs "home screen button")
  2. **Gather observable data first**: Add logging, check terminal output, check browser console BEFORE making changes
  3. **Understand the working case**: If something works in one scenario but not another, investigate the DIFFERENCE, don't assume the cause
  4. **One change at a time**: Make focused changes that can be verified, not multiple speculative fixes
  5. **Verify assumptions**: Don't assume "timing issue" or "selector issue" - confirm with logs
  6. **Framework behavior**: Understand how the framework works (e.g., Gradio's autoscroll triggers on visibility changes, not just data updates)
- **When touching external APIs**: Always check official docs first (especially Gemini structured output limitations)
- **Playbook modifications**: Validate that `next` node pointers form valid graphs (no accidental loops). After editing JSON files in `sea/playbooks/`, always run `python scripts/import_playbook.py --file <path>` to import the changes into the database
- **Database changes**: Write migration in `database/migrate.py`, test with `--db-file` on copy first

### Memory and History
- Building chat history: stored in memory, logged to `~/.saiverse/cities/<city>/buildings/<building>/log.json`
- SAIMemory logs: appended via `SAIMemoryAdapter.log_message()` with tags
- Pulse internal thoughts: tag='internal', include pulse_id for grouping
- User conversations: tag='conversation'

### Branch Context
- **Current branch**: `sea_framework`
- **Status**: SEA runtime and playbook system fully integrated with LangGraph, replacing direct `run_pulse()` calls
- **Meta playbooks**: `meta_user.json` (user input flow), `meta_auto.json` (autonomous pulse flow)
- **Pending work**: Building-scoped playbooks, advanced playbook features

### Testing
- Tests use `unittest` framework (pytest also works)
- Mock LLM clients when testing conversation logic
- DB tests should use temporary databases
- Check `docs/test_manual.md` for manual integration test scenarios (World Dive, Persona Genesis, etc.)

### Logging
- Main log: `saiverse_log.txt`
- Raw LLM I/O: `raw_llm_responses.txt`
- Per-persona logs: `~/.saiverse/personas/<id>/log.json`, `conscious_log.json`
- Set `SAIVERSE_LOG_LEVEL=DEBUG` in `.env` for verbose output
- SEA trace: set `SAIVERSE_SEA_TRACE=1` and `SAIVERSE_SEA_DUMP=<filepath>` to capture playbook execution
- **Debugging tip**: When `LOGGER.debug()` with `extra={}` doesn't show details, use `print()` to output directly to stdout. The logger formatter may not be configured to display `extra` fields.
- **Browser console logging**: JavaScript `console.debug()` is filtered by default in most browsers. Use `console.log()` for debug messages that should always be visible. In Chrome/Edge, open DevTools Console and set log level filter to "Verbose" or "All levels" to see `console.debug()` output.

### Common Pitfalls
- **Do not run `database/seed.py` carelessly** - it wipes the database
- **Inter-city travel is NOT via direct API calls** - it's DB-mediated through VisitingAI table polling
- **Gemini structured output does not support `additionalProperties`** - keep response schemas simple
- **Gemini context window is very large (1M+ tokens)** - Do not assume large context is the cause of errors. Gemini handles 100K+ tokens routinely. The system is designed to work with large conversation histories.
- **Playbook node transitions**: always verify `next` pointers form valid DAGs
- **When refactoring**: complete the entire change or revert; do not leave codebase in mixed state
- **Gradio SelectData.index type**: Always check for both `list` and `tuple` with `isinstance(idx, (list, tuple))` before accessing `idx[0]`. Gradio returns `list` type (e.g., `[row, col]`), not `tuple`. Missing this check causes silent failures in table selection handlers.
- **Gradio Chatbot autoscroll**: The `autoscroll=True` parameter works, but only triggers when the component becomes visible after being hidden. If updating data while already visible, autoscroll may not activate. To force autoscroll, temporarily hide the component (add CSS class), update data, then show it again. This visibility transition triggers the autoscroll behavior.
- **Gradio dynamic inline styles**: When Gradio components apply inline styles via JavaScript after page load, CSS rules (even with `!important`) cannot override them. Solution: Use JavaScript monkey patching to hijack `element.style.setProperty()` and replace values before they're applied. See `docs/session_reflection_2025-12-03_sidebar_detail_panel.md` for detailed example.
- **Asymmetric bugs indicate implementation mismatch**: If a bug occurs in scenario A but not in scenario B (despite similar logic), the cause is usually an implementation difference, not a timing/race condition. Compare code paths side-by-side to find where they diverge.
- **CSS text wrapping requires multiple layers**: For reliable wrapping of long URLs/strings in CSS, combine: `word-break: break-word`, `overflow-wrap: anywhere`, `max-width: 100%`, and `overflow-x: hidden` on both content and container elements. A single property is often insufficient, especially with frameworks that inject many nested elements.

## Dependencies

Key packages (see `requirements.txt`):
- `google-genai>=1.26.0` (Gemini API)
- `openai==1.97.0` (OpenAI + Anthropic)
- `gradio==5.38.0` (UI)
- `fastapi==0.116.1`, `uvicorn==0.35.0` (API server)
- `qdrant-client>=1.9.0` (vector DB)
- `sentence-transformers>=2.6.0`, `fastembed>=0.7.3` (embeddings)
- `rdiff-backup>=2.2.6` (backup utility)
- `discord.py>=2.4.0` (optional Discord gateway)

Embeddings models in `sbert/` (e.g., `intfloat/multilingual-e5-base`) are used if present, otherwise downloaded on first run.

## Environment Variables

Critical settings (see `.env.example`):
- `OPENAI_API_KEY`, `GEMINI_API_KEY`, `CLAUDE_API_KEY`, `OLLAMA_BASE_URL`
- `SDS_URL` (default: http://127.0.0.1:8080)
- `SAIVERSE_LOG_LEVEL` (DEBUG/INFO/WARNING)
- `SAIMEMORY_EMBED_MODEL` (e.g., intfloat/multilingual-e5-base)
- `QDRANT_LOCATION` (embedded path) or `QDRANT_URL` (remote server)
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
- `docs/session_reflection_*.md`: lessons learned from development sessions (Gradio UI patterns, debugging approaches, etc.)
- `README.md`: comprehensive setup and usage guide

## Quick Reference

**Create new persona**: Use World Editor or have user ask Genesis in "創造の祭壇" building

**Move persona between buildings**: `OccupancyManager.move_to(persona, building_id)` (do not call PersonaCore methods directly)

**Add new tool**: Define in `tools/defs/`, register in `tools/__init__.py`, link to buildings via `BuildingToolLink` or World Editor

**Modify playbook**: Edit JSON in `sea/playbooks/`, then run `python scripts/import_playbook.py --file sea/playbooks/<playbook>.json` to import to database. Alternatively, use `save_playbook` tool (validates graph before saving)

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
  4. Verify the field is stored in DB: `sqlite3 database/data/saiverse.db "SELECT nodes_json FROM playbooks WHERE name='<playbook_name>'"`

**Debug LLM calls**: Check `raw_llm_responses.txt` or set `SAIVERSE_SEA_DUMP` for playbook traces

**Access persona memory**: Use `scripts/recall_persona_memory.py` or Memory Settings UI tab
