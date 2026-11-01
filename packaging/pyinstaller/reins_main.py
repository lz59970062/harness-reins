"""Frozen entry point for the single ``reins`` console script declared in pyproject.toml."""
from harness_reins.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
