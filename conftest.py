"""Root pytest configuration for SAIVerse.

Collection policy for the tree below the repo root:

* ``temp/`` is excluded via ``addopts = --ignore=temp`` in pyproject.toml
  (gitignored vendored third-party code that runs build steps at import).
* ``expansion_data/<addon>/`` holds installed addons, each a *separate* git
  repository. Their test suites are addon-owned and are **opt-in**: they are
  skipped by default so ``python -m pytest`` exercises only the main repo, and
  are collected when ``--addons`` is passed.
* ``expansion_data/<addon>/**/external/`` is vendored third-party code inside an
  addon (e.g. voice-tts's GPT-SoVITS / BigVGAN, whose tests build CUDA kernels
  at import). It is **never** collected, even with ``--addons``.
"""
from __future__ import annotations

from pathlib import Path


def pytest_addoption(parser):
    parser.addoption(
        "--addons",
        action="store_true",
        default=False,
        help="Also collect addon test suites under expansion_data/<addon>/tests/",
    )


def pytest_ignore_collect(collection_path: Path, config) -> bool | None:
    parts = collection_path.parts
    if "expansion_data" not in parts:
        return None  # main repo (tests/, discord_gateway/tests, ...) — default rules
    # Vendored third-party trees inside an addon must never be collected.
    if "external" in parts:
        return True
    # Addon-owned tests are opt-in.
    if not config.getoption("--addons"):
        return True
    return None
