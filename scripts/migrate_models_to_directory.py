#!/usr/bin/env python3
"""Migrate models.json to models/ directory structure."""

import json
import re
from pathlib import Path


def sanitize_filename(model_id: str) -> str:
    """Convert model ID to a valid filename."""
    # Replace slashes and colons with hyphens
    sanitized = model_id.replace("/", "-").replace(":", "-")
    # Remove other problematic characters
    sanitized = re.sub(r"[^\w\-.]", "-", sanitized)
    return sanitized


def generate_display_name(model_id: str) -> str:
    """Generate a human-readable display name from model ID."""
    # Split by common separators
    parts = re.split(r"[/:\-_]", model_id)

    # Capitalize and clean up parts
    cleaned_parts = []
    for part in parts:
        # Skip common prefixes/suffixes
        if part.lower() in ("hf.co", "unsloth", "gguf", "instruct", "v0.1"):
            continue
        # Handle version numbers
        if re.match(r"^\d+\.\d+", part):
            part = f"v{part}"
        # Capitalize
        cleaned_parts.append(part.capitalize())

    return " ".join(cleaned_parts)


def main():
    models_json_path = Path("models.json")
    models_dir = Path("models")

    if not models_json_path.exists():
        print("models.json not found!")
        return

    # Load existing models.json
    with models_json_path.open("r", encoding="utf-8") as f:
        models = json.load(f)

    print(f"Found {len(models)} models in models.json")

    # Create models directory
    models_dir.mkdir(exist_ok=True)

    # Track existing files to avoid overwriting
    existing_files = {f.stem: f for f in models_dir.glob("*.json")}

    migrated_count = 0
    skipped_count = 0

    for model_id, config in models.items():
        # Generate filename
        filename = sanitize_filename(model_id) + ".json"
        file_path = models_dir / filename

        # Check if file already exists
        if file_path.stem in existing_files:
            print(f"  ⏭️  Skipping {model_id} (file already exists: {filename})")
            skipped_count += 1
            continue

        # Build new config structure
        new_config = {
            "model": model_id,
            "display_name": config.get("display_name") or generate_display_name(model_id),
            **config  # Include all original fields
        }

        # Add structured_output_backend for Nvidia NIM Mistral models
        if "mistralai" in model_id.lower() and config.get("base_url") == "https://integrate.api.nvidia.com/v1":
            if "structured_output_backend" not in new_config:
                new_config["structured_output_backend"] = "xgrammar"
                print(f"  ✨ Added xgrammar backend to {model_id}")

        # Write to file
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(new_config, f, indent=2, ensure_ascii=False)
            f.write("\n")  # Add trailing newline

        print(f"  ✅ Migrated {model_id} -> {filename}")
        migrated_count += 1

    print(f"\n✨ Migration complete!")
    print(f"   Migrated: {migrated_count}")
    print(f"   Skipped (already exist): {skipped_count}")
    print(f"   Total models: {len(models)}")
    print(f"\nℹ️  You can now review and edit display names in {models_dir}/")


if __name__ == "__main__":
    main()
