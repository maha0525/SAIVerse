"""setup creates the database SAIVerse uses only when there is none
(database/seed.py ``--if-missing``), found the way main.py finds it."""
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

from scripts import update_engine  # noqa: E402


def _sqlite(path: Path, *tables: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    for table in tables:
        conn.execute(f"CREATE TABLE {table} (id INTEGER)")
    conn.commit()
    conn.close()
    return path


def test_the_states_of_a_database(tmp_path: Path) -> None:
    assert seed.database_state(tmp_path / "none.db")[0] == seed.DB_MISSING
    assert seed.database_state(_sqlite(tmp_path / "world.db", "city", "ai"))[0] == seed.DB_READY
    assert seed.database_state(_sqlite(tmp_path / "empty.db"))[0] == seed.DB_WITHOUT_CITY
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a sqlite database, but it may be the user's world" * 100)
    assert seed.database_state(broken)[0] == seed.DB_UNREADABLE


def test_the_check_does_not_create_or_change_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    seed.database_state(missing)
    assert not missing.exists()
    world = _sqlite(tmp_path / "world.db", "city")
    before = world.read_bytes()
    seed.database_state(world)
    assert world.read_bytes() == before


@pytest.mark.parametrize(
    ("tables", "seeds"), [(None, True), ((), True), (("city",), False)],
    ids=["missing", "without city", "in place"],
)
def test_only_a_missing_or_empty_database_is_seeded(tmp_path: Path, monkeypatch, tables, seeds) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "saiverse.db"
    if tables is not None:
        _sqlite(db, *tables)
    calls: list[bool] = []
    monkeypatch.setattr(seed, "default_db_path", lambda: db)
    monkeypatch.setattr(seed, "seed_database", lambda force=False: calls.append(force))
    assert seed.seed_database_if_missing() == 0
    assert calls == ([True] if seeds else [])


def test_an_unreadable_database_is_left_and_setup_stops(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "saiverse.db"
    db.write_bytes(b"damaged" * 1000)
    before = db.read_bytes()
    monkeypatch.setattr(seed, "default_db_path", lambda: db)
    monkeypatch.setattr(seed, "seed_database", lambda force=False: pytest.fail("must not seed"))
    assert seed.seed_database_if_missing() == 2
    assert db.read_bytes() == before
    assert "setup" in capsys.readouterr().out


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


@pytest.mark.parametrize("script", ["setup.bat", "setup.sh"])
def test_setup_no_longer_checks_a_fixed_database_path(script: str) -> None:
    text = (REPO_ROOT / script).read_text(encoding="utf-8")
    assert "seed.py --if-missing" in text
    assert "saiverse.db" not in text
    assert "seed.py --force" not in text


@pytest.mark.parametrize("script", ["database/seed.py", "scripts/import_all_playbooks.py", "scripts/rebuild_addon_envs.py"])
def test_the_scripts_load_the_same_dotenv_as_main(script: str) -> None:
    assert "load_saiverse_dotenv(" in (REPO_ROOT / script).read_text(encoding="utf-8")
