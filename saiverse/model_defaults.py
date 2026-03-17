"""Built-in default model constants.

When rotating to a new model version (e.g. Gemini preview expiry),
update BUILTIN_DEFAULT_LITE_MODEL here. All fallback references
across the codebase import from this single file.
"""

# The single source of truth for the built-in fallback lite model.
# Used as the default for: DEFAULT_MODEL, LIGHTWEIGHT_MODEL, MEMORY_WEAVE_MODEL,
# ROUTER_MODEL, IMAGE_SUMMARY_MODEL, AGENTIC_MODEL, emotion module, etc.
BUILTIN_DEFAULT_LITE_MODEL = "gemini-3.1-flash-lite-preview"
