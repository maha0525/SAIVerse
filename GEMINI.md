# GEMINI.md

This file provides guidance to Gemini (Google's AI) when working with code in this repository.

## Notes for Gemini

**Language**: Think in English, respond in Japanese. The repository owner prefers Japanese for communication.

**Local preferences**: If `CLAUDE.local.md` exists, read it for additional context like names and personal preferences.

## Project Summary

SAIVerse is a multi-agent AI system where autonomous AI personas inhabit a virtual world. Key features:

- Multiple LLM providers (Gemini, OpenAI, Anthropic, Ollama, llama.cpp)
- Persistent memory using SAIMemory (SQLite)
- SEA framework: LangGraph-based playbook system for AI behavior
- Next.js frontend + FastAPI backend

## Directory Structure

```
SAIVerse/
├── builtin_data/        ← Built-in defaults (git tracked)
│   ├── tools/           ← Default tools
│   ├── phenomena/       ← Default phenomena
│   ├── playbooks/       ← Default playbooks
│   ├── models/          ← Default model configs
│   ├── prompts/         ← Default prompts
│   └── icons/           ← Default icons
│
├── user_data/           ← User customizations (gitignored)
│   ├── tools/           ← Custom tools (priority)
│   ├── models/          ← Custom model configs
│   ├── database/        ← SQLite database
│   └── icons/           ← Uploaded avatars
```

**Priority**: `user_data/` overrides `builtin_data/` when loading resources.

## Key Commands

```bash
# Start
python main.py city_a

# Database init (DESTRUCTIVE)
python database/seed.py

# Import playbooks (SAFE)
python scripts/import_all_playbooks.py

# Migrate to user_data structure
python scripts/migrate_to_user_data.py
```

## Key Files

- `saiverse_manager.py` - Central orchestrator
- `persona/core.py` - AI persona implementation
- `sea/runtime.py` - Playbook execution engine
- `data_paths.py` - Unified path management for user_data/builtin_data
- `model_configs.py` - Model configuration loading

## Code Conventions

- **ALWAYS read code before using existing objects** - verify attribute/method names
- **Check `database/models.py`** for column names
- **Playbooks**: JSON in `builtin_data/playbooks/` or `user_data/playbooks/`
- **Tools**: Defined in `tools/defs/` or `user_data/tools/`

## UI Conventions

### Modal Components

When creating modal dialogs, **always use the shared `ModalOverlay` component**:

```tsx
import ModalOverlay from '@/components/common/ModalOverlay';

<ModalOverlay onClose={handleClose} className={styles.overlay}>
    <div className={styles.modal} onClick={e => e.stopPropagation()}>
        {/* modal content */}
    </div>
</ModalOverlay>
```

**Do NOT** use direct `onClick={onClose}` on overlay divs — this causes modals to close unexpectedly when users drag from input fields.

For detailed documentation, see `CLAUDE.md`.
