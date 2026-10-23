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


def _leftovers(at: Path) -> list[str]:
    return sorted(p.name for p in at.parent.iterdir() if p != at)


def test_a_missing_database_is_made_aside_and_put_at_the_checked_path(at: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The world is filled in a file of this run's own, never in the file at
    the database's name, and given the name only when complete."""
    calls: list[Path] = []
    monkeypatch.setattr(seed, "populate_database", _fake_populate(calls))
    assert seed.seed_database_if_missing() == 0
    assert len(calls) == 1 and calls[0] != at and calls[0].parent == at.parent
    assert calls[0].name.startswith("saiverse.db.creating-")
    assert seed.database_state(at)[0] == seed.DB_READY
    assert _leftovers(at) == []


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


def test_a_database_that_appears_while_setup_makes_one_is_left(at: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Missing when checked, put there while setup fills its own file (another
    setup, a restore): it is neither opened, moved nor replaced."""
    def populate_while_restored(path: Path) -> bool:
        _sqlite(path, "city", rows=True)
        _sqlite(at, "city", "persona", rows=True)  # the restored world
        return True

    monkeypatch.setattr(seed, "populate_database", populate_while_restored)
    assert seed.seed_database_if_missing() == 2
    conn = sqlite3.connect(at)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    conn.close()
    assert tables == ["city", "persona"]
    assert _leftovers(at) == []


@pytest.mark.parametrize("how", ["returned", "raised"])
def test_a_failed_fill_removes_only_this_runs_file(at: Path, monkeypatch, how) -> None:  # type: ignore[no-untyped-def]
    def half(path: Path) -> bool:
        _sqlite(path, "city")  # tables made, world not committed
        if how == "raised":
            raise OSError("disk full")
        return False

    monkeypatch.setattr(seed, "populate_database", half)
    assert seed.seed_database_if_missing() == 1
    assert not at.exists()
    assert _leftovers(at) == []  # only this run's own file, removed


def test_a_failure_while_making_the_tables_closes_the_file_and_removes_it(at: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The real populate_database, failing in create_all after the city table:
    the file is closed (Windows cannot rename an open file), moved away, and
    the next setup creates the database again instead of taking it for one."""
    real_create_all = seed.Base.metadata.create_all

    def fails_after_city(engine, *args, **kwargs):  # type: ignore[no-untyped-def]
        seed.Base.metadata.tables["city"].create(engine)
        raise OSError("disk full")

    monkeypatch.setattr(seed.Base.metadata, "create_all", fails_after_city)
    assert seed.seed_database_if_missing() == 1
    assert not at.exists()
    assert _leftovers(at) == []  # closed, so Windows let it be removed

    monkeypatch.setattr(seed.Base.metadata, "create_all", real_create_all)
    assert seed.seed_database_if_missing() == 0
    assert seed.database_state(at)[0] == seed.DB_READY
    assert _leftovers(at) == []


def test_a_failed_world_skips_the_playbooks(at: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """populate_database returns before the playbook import when the world was
    not committed (the import would open the file again)."""
    monkeypatch.setattr(seed, "_create_user", lambda db, cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(seed, "import_initial_playbooks", lambda db_path=None: pytest.fail("must not import"))
    assert seed.populate_database(at) is False


@pytest.mark.skipif(os.name == "nt", reason="the hard-link path is the Mac / Linux one")
def test_publishing_never_overwrites_on_posix(tmp_path: Path) -> None:
    source = tmp_path / "new.db"
    source.write_text("new", encoding="utf-8")
    target = tmp_path / "saiverse.db"
    target.write_text("world", encoding="utf-8")
    with pytest.raises(FileExistsError):
        seed._publish_without_overwriting(source, target)
    assert target.read_text(encoding="utf-8") == "world"


@pytest.mark.parametrize("appears", [False, True])
def test_without_hard_links_the_copy_never_overwrites(tmp_path: Path, monkeypatch, appears) -> None:  # type: ignore[no-untyped-def]
    """The Mac / Linux fallback where hard links are not supported: a database
    that appears at the name just before it is published is left as it is."""
    monkeypatch.setattr(seed.os, "name", "posix")
    source = tmp_path / "new.db"
    source.write_bytes(b"new world" * 1000)
    target = tmp_path / "saiverse.db"

    def no_links(src, dst):  # type: ignore[no-untyped-def]
        if appears:
            target.write_bytes(b"restored world")
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(seed.os, "link", no_links)
    if appears:
        with pytest.raises(FileExistsError):
            seed._publish_without_overwriting(source, target)
        assert target.read_bytes() == b"restored world"
        assert source.exists()
    else:
        seed._publish_without_overwriting(source, target)
        assert target.read_bytes() == b"new world" * 1000
        assert not source.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot rename over an open file; the race is Mac / Linux only")
def test_a_failed_copy_never_removes_what_is_at_the_name(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The copy fails after another process put a database at the name (a
    restore renames over it): that database stays."""
    monkeypatch.setattr(seed.os, "name", "posix")
    monkeypatch.setattr(seed.os, "link", lambda src, dst: (_ for _ in ()).throw(OSError(1, "no links")))
    source = tmp_path / "new.db"
    source.write_bytes(b"new world" * 1000)
    target = tmp_path / "saiverse.db"
    real_fsync = os.fsync

    def restored_then_fails(fd):  # type: ignore[no-untyped-def]
        real_fsync(fd)
        replacement = tmp_path / "restore.tmp"
        replacement.write_bytes(b"restored world")
        os.replace(replacement, target)
        raise OSError(5, "I/O error")

    monkeypatch.setattr(seed.os, "fsync", restored_then_fails)
    with pytest.raises(OSError):
        seed._publish_without_overwriting(source, target)
    assert target.read_bytes() == b"restored world"


def test_publishing_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "new.db"
    source.write_text("new", encoding="utf-8")
    target = tmp_path / "saiverse.db"
    target.write_text("world", encoding="utf-8")
    with pytest.raises(FileExistsError):
        seed._publish_without_overwriting(source, target)
    assert target.read_text(encoding="utf-8") == "world"
    target.unlink()
    seed._publish_without_overwriting(source, target)
    assert target.read_text(encoding="utf-8") == "new" and not source.exists()


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


def test_a_dotenv_that_appears_while_setup_checks_is_kept(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """None found when checked, put there before the template is written (a
    restored setting): it keeps its SAIVERSE_HOME."""
    root = tmp_path / "SAIVerse"
    root.mkdir()
    (root / ".env.example").write_text("# example\n", encoding="utf-8")

    def none_yet(_root):  # type: ignore[no-untyped-def]
        (root / ".env").write_text("SAIVERSE_HOME=the-users-world\n", encoding="utf-8")
        return None

    monkeypatch.setattr(setup_venv, "find_dotenv_path", none_yet)
    assert setup_venv.ensure_dotenv(root)
    assert (root / ".env").read_text(encoding="utf-8") == "SAIVERSE_HOME=the-users-world\n"


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
