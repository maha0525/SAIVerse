"""setup creates the database SAIVerse uses only when there is none
(database/seed.py ``--if-missing``), found the way main.py finds it, and
never removes or replaces a database that is there."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "database"))

import seed  # noqa: E402

from scripts import setup_venv, update_engine  # noqa: E402


def _sqlite(path: Path, *tables: str, rows: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    for table in tables:
        conn.execute(f"CREATE TABLE {table} (id INTEGER)")
        if rows:
            conn.execute(f"INSERT INTO {table} VALUES (1)")
    conn.commit()
    conn.close()
    return path


def test_the_states_of_a_database(tmp_path: Path) -> None:
    assert seed.database_state(tmp_path / "none.db")[0] == seed.DB_MISSING
    assert seed.database_state(_sqlite(tmp_path / "world.db", "city", "ai", rows=True))[0] == seed.DB_READY
    # SQLite matches table names without regard to case, and so does the check.
    assert seed.database_state(_sqlite(tmp_path / "Upper.db", "City", rows=True))[0] == seed.DB_READY
    # A city table without a city: a setup that stopped while it filled the file.
    assert seed.database_state(_sqlite(tmp_path / "unfilled.db", "city", "ai"))[0] == seed.DB_INCOMPLETE
    assert seed.database_state(_sqlite(tmp_path / "empty.db"))[0] == seed.DB_EMPTY
    zero = tmp_path / "zero.db"
    zero.write_bytes(b"")
    assert seed.database_state(zero)[0] == seed.DB_EMPTY
    assert seed.database_state(_sqlite(tmp_path / "partial.db", "ai", rows=True))[0] == seed.DB_INCOMPLETE
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a sqlite database, but it may hold a world" * 100)
    assert seed.database_state(broken)[0] == seed.DB_UNREADABLE


def test_the_check_does_not_create_or_change_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    seed.database_state(missing)
    assert not missing.exists()
    world = _sqlite(tmp_path / "world.db", "city")
    before = world.read_bytes()
    seed.database_state(world)
    assert world.read_bytes() == before


def _fake_populate(calls: list[Path], ok: bool = True):  # type: ignore[no-untyped-def]
    def populate(path: Path) -> bool:
        calls.append(path)
        if ok:
            _sqlite(path, "city", rows=True)
        return ok

    return populate


@pytest.fixture
def at(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    db = tmp_path / "database" / "saiverse.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setattr(seed, "default_db_path", lambda: db)
    monkeypatch.setattr(seed, "ensure_data_dir", lambda: db.parent)
    monkeypatch.setattr(seed, "seed_database", lambda force=False: pytest.fail("must not replace anything"))
    return db


def test_a_world_in_place_is_left(at: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _sqlite(at, "City", "ai", rows=True)
    before = at.read_bytes()
    monkeypatch.setattr(seed, "populate_database", lambda path: pytest.fail("must not populate"))
    assert seed.seed_database_if_missing() == 0
    assert at.read_bytes() == before


def test_a_missing_database_is_created_at_the_checked_path(at: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[Path] = []
    monkeypatch.setattr(seed, "populate_database", _fake_populate(calls))
    assert seed.seed_database_if_missing() == 0
    assert calls == [at]
    assert seed.database_state(at)[0] == seed.DB_READY


@pytest.mark.parametrize("kind", ["empty", "no city table", "no city", "unreadable"])
def test_a_file_that_may_hold_a_world_is_left_and_setup_stops(at: Path, monkeypatch, capsys, kind) -> None:  # type: ignore[no-untyped-def]
    """Nothing but a missing file lets setup create one: an empty file may be
    the one another setup is filling right now, the others may hold a world."""
    if kind == "empty":
        at.write_bytes(b"")
    elif kind == "no city table":
        _sqlite(at, "ai", "building", rows=True)
    elif kind == "no city":
        _sqlite(at, "city", "ai")
    else:
        at.write_bytes(b"damaged" * 1000)
    before = at.read_bytes()
    monkeypatch.setattr(seed, "populate_database", lambda path: pytest.fail("must not populate"))
    assert seed.seed_database_if_missing() == 2
    assert at.read_bytes() == before
    assert list(at.parent.iterdir()) == [at]
    assert "setup" in capsys.readouterr().out


def test_a_database_that_appears_after_the_check_is_left(at: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Missing when checked, there when setup creates it (another setup, a
    restore): it is not touched."""
    real_open = os.open

    def appears(path, flags, *rest):  # type: ignore[no-untyped-def]
        if Path(path) == at and not at.exists():
            _sqlite(at, "city", rows=True)
        return real_open(path, flags, *rest)

    monkeypatch.setattr(seed.os, "open", appears)
    monkeypatch.setattr(seed, "populate_database", lambda path: pytest.fail("must not populate"))
    assert seed.seed_database_if_missing() == 2
    assert seed.database_state(at)[0] == seed.DB_READY
    conn = sqlite3.connect(at)
    assert conn.execute("SELECT COUNT(*) FROM city").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("how", ["returned", "raised"])
def test_a_failed_fill_moves_the_half_made_file_away(at: Path, monkeypatch, how) -> None:  # type: ignore[no-untyped-def]
    def half(path: Path) -> bool:
        _sqlite(path, "city")  # tables made, world not committed
        if how == "raised":
            raise OSError("disk full")
        return False

    monkeypatch.setattr(seed, "populate_database", half)
    assert seed.seed_database_if_missing() == 1
    assert not at.exists()
    assert len(list(at.parent.glob("saiverse.db.failed-*"))) == 1


def test_a_failure_while_making_the_tables_closes_the_file_and_moves_it_away(at: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The real populate_database, failing in create_all after the city table:
    the file is closed (Windows cannot rename an open file), moved away, and
    the next setup creates the database again instead of taking it for one."""
    real_create_all = seed.Base.metadata.create_all

    def fails_after_city(engine, *args, **kwargs):  # type: ignore[no-untyped-def]
        seed.Base.metadata.tables["city"].create(engine)
        raise OSError("disk full")

    monkeypatch.setattr(seed.Base.metadata, "create_all", fails_after_city)
    monkeypatch.setattr(seed, "import_initial_playbooks", lambda db_path=None: None)
    assert seed.seed_database_if_missing() == 1
    assert not at.exists()
    assert len(list(at.parent.glob("saiverse.db.failed-*"))) == 1

    monkeypatch.setattr(seed.Base.metadata, "create_all", real_create_all)
    assert seed.seed_database_if_missing() == 0
    assert seed.database_state(at)[0] == seed.DB_READY


def test_setup_creates_the_database_in_saiverse_home(tmp_path: Path) -> None:
    """The real script, as setup runs it: the database goes under SAIVERSE_HOME
    (where main.py looks), and a second run leaves it as it is."""
    home = tmp_path / "home"
    env = {**os.environ, "SAIVERSE_HOME": str(home), "PYTHONIOENCODING": "utf-8"}
    db = home / "user_data" / "database" / "saiverse.db"
    first = subprocess.run(
        [sys.executable, "database/seed.py", "--if-missing"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert seed.database_state(db)[0] == seed.DB_READY
    before = db.stat().st_mtime_ns
    second = subprocess.run(
        [sys.executable, "database/seed.py", "--if-missing"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "Database already exists" in second.stdout
    assert db.stat().st_mtime_ns == before


def test_a_saiverse_home_in_dotenv_is_used_and_the_environment_wins(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / ".env").write_text("SAIVERSE_HOME=from-dotenv\nSAIVERSE_E2E_ONLY_IN_DOTENV=1\n", encoding="utf-8")
    monkeypatch.setenv("SAIVERSE_HOME", "from-environment")
    monkeypatch.delenv("SAIVERSE_E2E_ONLY_IN_DOTENV", raising=False)
    assert update_engine.load_saiverse_dotenv(tmp_path) == tmp_path / ".env"
    assert os.environ["SAIVERSE_HOME"] == "from-environment"
    assert os.environ["SAIVERSE_E2E_ONLY_IN_DOTENV"] == "1"
    monkeypatch.delenv("SAIVERSE_E2E_ONLY_IN_DOTENV")


def test_setup_keeps_a_dotenv_above_the_folder(tmp_path: Path) -> None:
    """A .env above the SAIVerse folder chooses the world; setup does not hide
    it with a new one (which would change the SAIVERSE_HOME SAIVerse reads)."""
    (tmp_path / ".env").write_text("SAIVERSE_HOME=somewhere\n", encoding="utf-8")
    root = tmp_path / "SAIVerse"
    root.mkdir()
    (root / ".env.example").write_text("# example\n", encoding="utf-8")
    assert setup_venv.ensure_dotenv(root)
    assert not (root / ".env").exists()


def test_setup_creates_a_dotenv_when_there_is_none(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "SAIVerse"
    root.mkdir()
    (root / ".env.example").write_text("# example\n", encoding="utf-8")
    monkeypatch.setattr(setup_venv, "find_dotenv_path", lambda _root: None)
    assert setup_venv.ensure_dotenv(root)
    assert (root / ".env").read_text(encoding="utf-8") == "# example\n"


@pytest.mark.parametrize("script", ["setup.bat", "setup.sh"])
def test_setup_decides_the_dotenv_before_anything_reads_data_paths(script: str) -> None:
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    assert "seed.py --if-missing" in text
    assert "saiverse.db" not in text
    assert "seed.py --force" not in text
    assert ".env.example .env" not in text  # only setup_venv.py ensure-dotenv copies it
    order = [text.index(marker) for marker in (
        "setup_venv.py check-stopped", "setup_venv.py ensure-dotenv", "rebuild_addon_envs.py",
        "seed.py --if-missing", "import_all_playbooks.py",
    )]
    assert order == sorted(order)


@pytest.mark.parametrize("script", ["database/seed.py", "scripts/import_all_playbooks.py", "scripts/rebuild_addon_envs.py"])
def test_the_scripts_load_the_same_dotenv_as_main(script: str) -> None:
    assert "load_saiverse_dotenv(" in (REPO_ROOT / script).read_text(encoding="utf-8")
