"""The upgrade chain across early-access (rc) releases.

docs/intent/early_access_release.md §3-5: early-access versions are PEP 440
pre-releases (``0.4.0rc1``), and a world records one ``LAST_KNOWN_VERSION``. The
release discipline is that the first rc edge starts at the latest stable
release and the final release keeps every rc edge, so a world at *any* point
on the path -- still stable, on rc1, on rc2 -- reaches the final release by
exactly the edges it has not run yet. This checks the mechanism that
discipline relies on, with a synthetic chain 0.3.14 -> 0.4.0rc1 -> 0.4.0rc2
-> 0.4.0 (the real chain is walked by ``test_upgrade_release_chain.py``):

- ``current_version`` accepts an rc VERSION;
- ``select_handlers`` picks, from each starting world, a gap-free run of edges
  that ends at the target and includes no edge the world already ran;
- driving one world through rc1, rc2 and the final release in separate
  starts runs every handler exactly once.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from packaging.version import Version

import saiverse
from saiverse import upgrade

RC1 = "0.4.0rc1"
RC2 = "0.4.0rc2"
FINAL = "0.4.0"
STABLE = "0.3.14"


@pytest.fixture
def ran() -> list[str]:
    return []


@pytest.fixture(autouse=True)
def _synthetic_chain(ran: list[str]):  # type: ignore[no-untyped-def]
    saved = list(upgrade.HANDLERS)
    saved_loaded = upgrade._handlers_loaded
    upgrade.HANDLERS.clear()
    upgrade._handlers_loaded = True  # keep the real registry out of this test

    def edge(name: str, scope: str, from_version: str, to_version: str) -> upgrade.UpgradeHandler:
        return upgrade.UpgradeHandler(
            name=name,
            scope=scope,  # type: ignore[arg-type]
            from_version=from_version,
            to_version=to_version,
            run=lambda **_: ran.append(name),
        )

    for scope in ("city", "ai"):
        upgrade.HANDLERS.extend(
            [
                edge(f"{scope}_0_3_14", scope, "0.3.13", STABLE),
                edge(f"{scope}_rc1", scope, STABLE, RC1),
                edge(f"{scope}_rc2", scope, RC1, RC2),
                edge(f"{scope}_final", scope, RC2, FINAL),
            ]
        )
    yield
    upgrade.HANDLERS.clear()
    upgrade.HANDLERS.extend(saved)
    upgrade._handlers_loaded = saved_loaded


def test_current_version_reads_an_rc_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(saiverse, "__version__", RC1)
    current = upgrade.current_version()
    assert current == Version(RC1)
    assert current.is_prerelease
    assert Version(STABLE) < current < Version(RC2) < Version(FINAL)


@pytest.mark.parametrize("scope", ["city", "ai"])
@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (STABLE, ["rc1", "rc2", "final"]),
        (RC1, ["rc2", "final"]),
        (RC2, ["final"]),
        (FINAL, []),
    ],
)
def test_every_world_on_the_path_reaches_the_final_release(
    scope: str, start: str, expected: list[str]
) -> None:
    selected = upgrade.select_handlers(scope, Version(start), Version(FINAL))  # type: ignore[arg-type]
    assert [h.name for h in selected] == [f"{scope}_{suffix}" for suffix in expected]
    # Gap-free: each edge starts where the previous one ended.
    cursor = Version(start)
    for handler in selected:
        assert Version(handler.from_version) == cursor
        cursor = Version(handler.to_version)
    assert cursor == Version(FINAL)


@pytest.mark.parametrize("scope", ["city", "ai"])
def test_a_missing_rc_edge_stops_the_chain(scope: str) -> None:
    upgrade.HANDLERS[:] = [h for h in upgrade.HANDLERS if h.name != f"{scope}_rc2"]
    with pytest.raises(ValueError, match="gap"):
        upgrade.select_handlers(scope, Version(STABLE), Version(FINAL))  # type: ignore[arg-type]


class _Session:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_upgrading_through_each_release_runs_every_handler_once(ran: list[str]) -> None:
    city = SimpleNamespace(LAST_KNOWN_VERSION=STABLE)
    session = _Session()
    for target in (RC1, RC2, FINAL):
        ok = upgrade._run_handlers_for_entity(
            session,  # type: ignore[arg-type]
            scope="city",
            entity=city,
            entity_id="1",
            target=Version(target),
        )
        assert ok
        assert city.LAST_KNOWN_VERSION == target
    assert ran == ["city_rc1", "city_rc2", "city_final"]

    # Starting again on the final release is a no-op, not a second run.
    assert upgrade._run_handlers_for_entity(
        session,  # type: ignore[arg-type]
        scope="city",
        entity=city,
        entity_id="1",
        target=Version(FINAL),
    )
    assert ran == ["city_rc1", "city_rc2", "city_final"]


def test_an_rc_code_refuses_a_world_already_on_the_final_release(ran: list[str]) -> None:
    city = SimpleNamespace(LAST_KNOWN_VERSION=FINAL)
    ok = upgrade._run_handlers_for_entity(
        _Session(),  # type: ignore[arg-type]
        scope="city",
        entity=city,
        entity_id="1",
        target=Version(RC2),
    )
    assert not ok
    assert city.LAST_KNOWN_VERSION == FINAL
    assert ran == []
