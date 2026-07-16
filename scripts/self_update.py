"""Compatibility entrypoint for the canonical update engine."""
try:
    from scripts.update_engine import main
except ModuleNotFoundError:  # Direct ``python scripts/self_update.py`` execution.
    from update_engine import main


if __name__ == "__main__":
    raise SystemExit(main())
