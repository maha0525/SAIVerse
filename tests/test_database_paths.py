from pathlib import Path

from database import paths as db_paths


def test_default_db_path_uses_configurable_data_dir(monkeypatch, tmp_path):
    custom_data_dir = tmp_path / "data"
    monkeypatch.setattr(db_paths, "_get_data_dir", lambda: custom_data_dir)
    monkeypatch.setattr(db_paths, "DEFAULT_DB_NAME", "world.db", raising=False)

    path = db_paths.default_db_path()

    assert path == custom_data_dir / "world.db"
    assert path.parent.exists()


def test_ensure_data_dir_is_idempotent(monkeypatch, tmp_path):
    custom_data_dir = tmp_path / "nested" / "data"
    monkeypatch.setattr(db_paths, "_get_data_dir", lambda: custom_data_dir)

    first = db_paths.ensure_data_dir()
    second = db_paths.ensure_data_dir()

    assert first == second == custom_data_dir
    assert custom_data_dir.exists()
