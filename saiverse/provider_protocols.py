"""Protocol names shared by configuration validation and their implementations.

Kept free of client imports so loading provider/model registries cannot recurse
through llm_clients.__init__ into the factory while they are being initialized.
"""

SUPPORTED_LLM_PROTOCOLS = frozenset({
    "openai_compat", "openai_codex", "nvidia_nim", "anthropic_native",
    "gemini_native", "xai_native", "ollama_compat",
})
JEV_COMPAT_PROTOCOL = "jev_compat"
SUPPORTED_PROVIDER_PROTOCOLS = SUPPORTED_LLM_PROTOCOLS | {JEV_COMPAT_PROTOCOL}
