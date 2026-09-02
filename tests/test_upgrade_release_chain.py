"""The release edges in ``saiverse.upgrade_handlers`` must form an unbroken chain.

Every release needs an edge into its version (a no-op one when nothing
migrates); a missing or mistyped edge makes ``select_handlers`` raise
"Upgrade chain ends at X" on the first start after the update, for every user.
Until 2026-09-03 this was only checked by hand before tagging (a scratchpad
script), so a typo in ``from_version`` / ``to_version`` would have reached
users. This test walks the whole chain, from a database that predates version
tracking (``PRE_VERSION_AWARE``) to the current ``VERSION``, for both scopes.
"""
from __future__ import annotations

import pytest
from packaging.version import Version

from saiverse import upgrade

OLDEST_SUPPORTED = upgrade.PRE_VERSION_AWARE


@pytest.fixture(autouse=True)
def _default_handlers():
    upgrade.HANDLERS.clear()
    upgrade._handlers_loaded = False
    upgrade._load_default_handlers()
    yield
    upgrade.HANDLERS.clear()
    upgrade._handlers_loaded = False


@pytest.mark.parametrize("scope", ["city", "ai"])
def test_release_edges_reach_current_version_from_oldest_db(scope: str) -> None:
    current = upgrade.current_version()
    assert current > OLDEST_SUPPORTED

    # select_handlers raises on a gap or a chain that stops short of the target.
    selected = upgrade.select_handlers(scope, OLDEST_SUPPORTED, current)

    assert selected, f"no {scope} edges between {OLDEST_SUPPORTED} and {current}"
    assert Version(selected[0].from_version) == OLDEST_SUPPORTED, (
        f"first {scope} edge starts at {selected[0].from_version}, not {OLDEST_SUPPORTED}"
    )
    assert Version(selected[-1].to_version) == current, (
        f"{scope} chain ends at {selected[-1].to_version}, VERSION is {current}"
    )


@pytest.mark.parametrize("scope", ["city", "ai"])
def test_previous_release_has_an_edge_into_current_version(scope: str) -> None:
    current = upgrade.current_version()
    incoming = [
        handler
        for handler in upgrade.HANDLERS
        if handler.scope == scope and Version(handler.to_version) == current
    ]
    assert incoming, f"no {scope} edge into {current}; add a no-op edge in upgrade_handlers.py"
