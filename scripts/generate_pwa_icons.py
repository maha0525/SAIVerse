#!/usr/bin/env python3
"""
Generate PWA icons from the existing icon.jpg.

Requirements:
    pip install Pillow

Usage:
    python scripts/generate_pwa_icons.py
"""

from pathlib import Path
from PIL import Image


def main():
    # Paths
    frontend_dir = Path(__file__).parent.parent / "frontend"
    public_dir = frontend_dir / "public"
    icons_dir = public_dir / "icons"
    source_icon = public_dir / "icon.jpg"

    if not source_icon.exists():
        print(f"Error: Source icon not found at {source_icon}")
        return 1

    # Create icons directory
    icons_dir.mkdir(parents=True, exist_ok=True)

    # Load source image
    print(f"Loading {source_icon}...")
    img = Image.open(source_icon)

    # Convert to RGB if necessary (remove alpha channel)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Generate icons
    sizes = [192, 512]

    for size in sizes:
        output_path = icons_dir / f"icon-{size}.png"
        print(f"Generating {output_path} ({size}x{size})...")

        # Resize with high quality
        resized = img.resize((size, size), Image.Resampling.LANCZOS)
        resized.save(output_path, "PNG", optimize=True)
        print(f"  Created: {output_path}")

    # Also create apple-touch-icon.png (180x180)
    apple_icon = icons_dir / "apple-touch-icon.png"
    print(f"Generating {apple_icon} (180x180)...")
    resized = img.resize((180, 180), Image.Resampling.LANCZOS)
    resized.save(apple_icon, "PNG", optimize=True)
    print(f"  Created: {apple_icon}")

    print("\nDone! PWA icons generated successfully.")
    return 0


if __name__ == "__main__":
    exit(main())