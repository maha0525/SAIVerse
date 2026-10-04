"""Scope inference stays shared, backwards compatible, and independent of the CLI."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from saiverse.playbook_scope import infer_scope_from_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCOPES = [
    ("public/example.json", ("public", None, None)),
    ("personal/persona_test/example.json", ("personal", "persona_test", None)),
    ("building/room_test/example.json", ("building", None, "room_test")),
]


@pytest.mark.parametrize("root", ["builtin_data", "user_data", "expansion_data/addon", "sea"])
@pytest.mark.parametrize("relative_path, expected", SCOPES)
def test_resource_layers_share_scope_rules(tmp_path, root, relative_path, expected):
    # These paths need not exist: inference does not read the JSON or validate IDs.
    path = tmp_path / root / "playbooks" / relative_path
    assert infer_scope_from_path(path) == expected


@pytest.mark.parametrize("relative_path, expected", [
    ("example.json", ("public", None, None)),
    ("playbooks", ("public", None, None)),
    ("playbooks/unknown/example.json", ("public", None, None)),
    ("playbooks/personal", ("public", None, None)),
    ("playbooks/building", ("public", None, None)),
    ("playbooks/public", ("public", None, None)),
    ("playbooks/personal/example.json", ("personal", "example.json", None)),
    ("playbooks/building/example.json", ("building", None, "example.json")),
    ("playbooks/personal/persona_test", ("personal", "persona_test", None)),
    ("playbooks/building/room_test/nested/example.txt", ("building", None, "room_test")),
    ("playbooks/unknown/playbooks/personal/persona_test/example.json", ("public", None, None)),
    ("playbooks/public/playbooks/building/room_test/example.json", ("public", None, None)),
])
def test_legacy_fallbacks_and_first_playbooks_component(tmp_path, relative_path, expected):
    assert infer_scope_from_path(tmp_path / relative_path) == expected


def test_relative_path_is_resolved_before_scope_inference(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path("builtin_data/playbooks/public/../personal/persona_test/example.json")
    assert infer_scope_from_path(path) == ("personal", "persona_test", None)


def test_symlink_uses_resolved_target_scope(tmp_path):
    target = tmp_path / "playbooks/personal/persona_test/example.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "playbooks/public/alias.json"
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation is not available: {exc}")
    assert infer_scope_from_path(link) == ("personal", "persona_test", None)


_IMPORT_PROBE = r'''
import importlib.abc
import sys
from pathlib import Path


def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise AssertionError("Network access is forbidden in the import probe")


class NoCliImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "scripts.import_playbook":
            raise AssertionError("Scope inference must not import the CLI")


sys.addaudithook(deny_network)
sys.meta_path.insert(0, NoCliImports())
before_path = list(sys.path)
from saiverse.playbook_scope import infer_scope_from_path
assert sys.path == before_path, "Shared helper import changed sys.path"
for prefix in ("scripts", "builtin_data", "database", "tools", "sea", "manager"):
    assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules), prefix
assert infer_scope_from_path(Path("playbooks/personal/persona_test/example.json")) == (
    "personal", "persona_test", None,
)

if sys.argv[1] == "admin":
    from manager import admin
    assert admin.infer_scope_from_path is infer_scope_from_path
    assert "scripts.import_playbook" not in sys.modules
'''


@pytest.mark.parametrize("target", ["helper", "admin"])
def test_fresh_import_does_not_depend_on_cli(tmp_path, target):
    # Do not let cached pytest imports hide the dependency or inherit real data.
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL",
    }}
    env.update({
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "SAIVERSE_HOME": str(tmp_path / "isolated-home"),
        "SAIVERSE_USER_DATA_DIR": str(tmp_path / "isolated-home/user_data"),
        "SAIVERSE_SKIP_TOOL_IMPORTS": "1",
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, target], cwd=tmp_path, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if target == "helper":
        assert not (tmp_path / "isolated-home").exists()


def _write_playbook(path, name="example"):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "description": "A synthetic playbook"}
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


@pytest.mark.parametrize("relative_path, expected", SCOPES)
def test_cli_passes_inferred_scope_to_save(tmp_path, monkeypatch, relative_path, expected):
    path = tmp_path / "playbooks" / relative_path
    data = _write_playbook(path)
    save = Mock()
    save_module = ModuleType("builtin_data.tools.save_playbook")
    save_module.save_playbook = save
    monkeypatch.setitem(sys.modules, save_module.__name__, save_module)
    # The CLI's own path setup is unchanged and must not leak out of this test.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", ["import_playbook.py", "--file", str(path)])

    namespace = runpy.run_path(str(PROJECT_ROOT / "scripts/import_playbook.py"), run_name="__main__")

    assert namespace["infer_scope_from_path"] is infer_scope_from_path
    scope, persona_id, building_id = expected
    save.assert_called_once_with(
        name="example", description=data["description"], scope=scope,
        created_by_persona_id=persona_id, building_id=building_id,
        playbook_json=json.dumps(data, ensure_ascii=False),
        router_callable=None, user_selectable=None,
    )


def test_cli_explicit_arguments_still_override_inference(tmp_path, monkeypatch):
    path = tmp_path / "playbooks/public/example.json"
    _write_playbook(path)
    save = Mock()
    save_module = ModuleType("builtin_data.tools.save_playbook")
    save_module.save_playbook = save
    monkeypatch.setitem(sys.modules, save_module.__name__, save_module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", [
        "import_playbook.py", "--file", str(path), "--scope", "personal",
        "--persona-id", "explicit_persona", "--building-id", "explicit_building",
        "--name", "renamed", "--description", "overridden", "--router-callable",
        "--no-user-selectable",
    ])
    runpy.run_path(str(PROJECT_ROOT / "scripts/import_playbook.py"), run_name="__main__")
    save.assert_called_once()
    assert save.call_args.kwargs == {
        "name": "renamed", "description": "overridden", "scope": "personal",
        "created_by_persona_id": "explicit_persona", "building_id": "explicit_building",
        "playbook_json": path.read_text(encoding="utf-8"),
        "router_callable": True, "user_selectable": False,
    }


@pytest.mark.parametrize("relative_path, expected", SCOPES)
def test_admin_single_import_passes_inferred_scope(tmp_path, monkeypatch, relative_path, expected):
    from manager import admin

    path = tmp_path / "playbooks" / relative_path
    _write_playbook(path)
    save = Mock()
    monkeypatch.setattr(admin, "save_playbook", save)
    service = admin.AdminService.__new__(admin.AdminService)

    result = service.import_playbook_from_file(str(path))

    assert result.startswith("Success:")
    save.assert_called_once()
    kwargs = save.call_args.kwargs
    assert (kwargs["scope"], kwargs["created_by_persona_id"], kwargs["building_id"]) == expected


def test_admin_bulk_import_passes_each_inferred_scope(tmp_path, monkeypatch):
    from manager import admin

    root = tmp_path / "playbooks"
    cases = [*SCOPES, ("unknown/example.json", ("public", None, None))]
    expected_by_name = {}
    for index, (relative_path, expected) in enumerate(cases):
        name = f"example_{index}"
        _write_playbook(root / relative_path, name)
        expected_by_name[name] = expected
    save = Mock()
    monkeypatch.setattr(admin, "save_playbook", save)
    service = admin.AdminService.__new__(admin.AdminService)

    result = service.reimport_all_playbooks(str(root))

    assert "imported=4, failed=0, scanned=4" in result
    assert save.call_count == 4
    for call in save.call_args_list:
        kwargs = call.kwargs
        assert (kwargs["scope"], kwargs["created_by_persona_id"], kwargs["building_id"]) == expected_by_name[kwargs["name"]]
