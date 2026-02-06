"""Debug script to verify model config loading"""
import json
from model_configs import MODEL_CONFIGS, get_model_config

# Check if gemini-3-flash-preview is loaded
print("=== Checking MODEL_CONFIGS ===")
print(f"Total configs loaded: {len(MODEL_CONFIGS)}")
print(f"\nAll config keys: {list(MODEL_CONFIGS.keys())}")

if "gemini-3-flash-preview" in MODEL_CONFIGS:
    print("\n✓ gem ini-3-flash-preview found in MODEL_CONFIGS")
    config = MODEL_CONFIGS["gemini-3-flash-preview"]
    print("Config content:")
    print(json.dumps(config, indent=2, ensure_ascii=False))
else:
    print("\n✗ gemini-3-flash-preview NOT found in MODEL_CONFIGS")

# Check via get_model_config
print("\n=== Checking get_model_config() ===")
config = get_model_config("gemini-3-flash-preview")
if config:
    print("Config retrieved:")
    print(json.dumps(config, indent=2, ensure_ascii=False))
    print(f"\nmodel field value: {config.get('model')}")
else:
    print("Config is empty!")
