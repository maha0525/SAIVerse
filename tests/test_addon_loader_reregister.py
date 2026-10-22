"""動かしたままのアドオン再登録 (更新・選択肢の追加・再有効化) の冪等性。

register は追記なので、古い登録を外さずに重ねると persona_speak などの
handler が二重になり、発話のたびに読み上げ・合成が二重に走る (2026-10-23 に
voice-tts の更新で実害)。_register_addon_server_hooks_for /
_register_addon_integrations_for は、同じアドオンの古い登録を先に外してから
登録し直すこと。
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from saiverse import addon_context, addon_hooks, addon_loader


@pytest.fixture(autouse=True)
def _isolated_registries():
    """handler の登録簿とアドオン別の記録をテストごとに空から始める。"""
    with patch.object(addon_hooks, "_handlers", {}), patch.object(
        addon_context, "_providers", {},
    ), patch.object(addon_context, "_last_results", {}), patch.object(
        addon_context, "_generations", {},
    ), patch.dict(addon_loader._addon_server_hooks_registry, clear=True), patch.dict(
        addon_loader._addon_integration_registry, clear=True,
    ):
        yield


def _hook_addon(tmp_path, name="fixture-addon", *, with_provider=False):
    """persona_speak の server_hook を 1 つ持つアドオンのフォルダを作る。"""
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    manifest = {
        "name": name,
        "server_hooks": [
            {"event": "persona_speak", "handler": "hook_mod:on_speak"},
        ],
    }
    if with_provider:
        manifest["context_provider"] = "hook_mod:guide"
    (directory / "addon.json").write_text(json.dumps(manifest), encoding="utf8")
    (directory / "hook_mod.py").write_text(
        "def on_speak(**kwargs):\n"
        "    return None\n"
        "\n"
        "def guide(*, persona, building_id):\n"
        "    return '案内'\n",
        encoding="utf8",
    )
    return directory


def test_reregistering_server_hooks_does_not_duplicate_handlers(tmp_path):
    """更新 (動かしたまま再登録) を模して二度 register しても handler は 1 本。"""
    directory = _hook_addon(tmp_path)
    assert addon_loader._register_addon_server_hooks_for(directory) == 1
    assert addon_loader._register_addon_server_hooks_for(directory) == 1

    handlers = addon_hooks._registered_handlers("persona_speak")
    assert len(handlers) == 1
    assert len(addon_loader._addon_server_hooks_registry["fixture-addon"]) == 1


def test_reregistering_keeps_the_latest_module_code(tmp_path):
    """再登録後に残る 1 本は、新しいファイルの関数 (更新の目的そのもの)。"""
    directory = _hook_addon(tmp_path)
    addon_loader._register_addon_server_hooks_for(directory)
    (directory / "hook_mod.py").write_text(
        "def on_speak(**kwargs):\n"
        "    return 'updated'\n",
        encoding="utf8",
    )
    addon_loader._register_addon_server_hooks_for(directory)

    (handler,) = addon_hooks._registered_handlers("persona_speak")
    assert handler() == "updated"


def test_reregistering_does_not_touch_other_addons_hooks(tmp_path):
    """外すのは同じアドオンの古い登録だけ。他のアドオンの handler は残る。"""
    first = _hook_addon(tmp_path, "addon-a")
    second = _hook_addon(tmp_path, "addon-b")
    addon_loader._register_addon_server_hooks_for(first)
    addon_loader._register_addon_server_hooks_for(second)
    addon_loader._register_addon_server_hooks_for(first)

    assert len(addon_hooks._registered_handlers("persona_speak")) == 2


def test_reregistering_server_hooks_keeps_the_context_provider(tmp_path):
    """古い server_hooks を外す処理が context provider を巻き添えにしない
    (提供口は「解決してから置き換える」— 先に消すと差分検知が偽の通知を出す)。"""
    directory = _hook_addon(tmp_path, with_provider=True)
    persona = SimpleNamespace(persona_id="fixture", current_building_id="room")
    addon_loader._register_addon_server_hooks_for(directory)
    addon_loader._register_addon_server_hooks_for(directory)

    assert addon_context.collect_guidance(persona, "room") == [
        ("fixture-addon", "案内"),
    ]
    assert len(addon_hooks._registered_handlers("persona_speak")) == 1


def test_old_handler_stays_registered_while_the_new_one_resolves(tmp_path, monkeypatch):
    """古い登録を外すのは新しい handler の解決後 (module の import 中に発話が
    dispatch されても handler が 0 本にならない — 提供口の「解決してから
    置き換える」と同じ向き)。"""
    directory = _hook_addon(tmp_path)
    addon_loader._register_addon_server_hooks_for(directory)
    real_resolve = addon_loader._resolve_hook_handler
    counts_during_resolve = []

    def resolve_while_observing(addon_dir, spec):
        counts_during_resolve.append(
            len(addon_hooks._registered_handlers("persona_speak"))
        )
        return real_resolve(addon_dir, spec)

    monkeypatch.setattr(addon_loader, "_resolve_hook_handler", resolve_while_observing)
    addon_loader._register_addon_server_hooks_for(directory)

    assert counts_during_resolve == [1]
    assert len(addon_hooks._registered_handlers("persona_speak")) == 1


def test_unregister_failure_keeps_the_pair_tracked(tmp_path, monkeypatch):
    """外しに失敗した登録は登録簿に書き戻す (落とすと handler は生きているのに
    追えなくなり、次の再登録で二重が静かに復活する)。"""
    directory = _hook_addon(tmp_path)
    addon_loader._register_addon_server_hooks_for(directory)
    (tracked_pair,) = addon_loader._addon_server_hooks_registry["fixture-addon"]

    def raising_unregister(event, handler):
        raise RuntimeError("boom")

    monkeypatch.setattr(addon_hooks, "unregister_hook", raising_unregister)
    removed = addon_loader._unregister_server_hook_pairs("fixture-addon")

    assert removed == 0
    assert addon_loader._addon_server_hooks_registry["fixture-addon"] == [tracked_pair]


def test_unregister_after_reregister_removes_the_hook(tmp_path):
    """再登録を挟んでも、無効化 (unregister) で handler が残らない。"""
    directory = _hook_addon(tmp_path)
    addon_loader._register_addon_server_hooks_for(directory)
    addon_loader._register_addon_server_hooks_for(directory)
    addon_loader.unregister_addon_server_hooks("fixture-addon")

    assert addon_hooks._registered_handlers("persona_speak") == []
    assert "fixture-addon" not in addon_loader._addon_server_hooks_registry


def _integration_addon(tmp_path, name="poll-addon"):
    directory = tmp_path / name
    (directory / "integrations").mkdir(parents=True, exist_ok=True)
    (directory / "addon.json").write_text(
        json.dumps({"name": name, "version": "0.0.1"}), encoding="utf8",
    )
    (directory / "integrations" / "main.py").write_text(
        "from saiverse.integrations.base import BaseIntegration\n"
        "\n"
        "\n"
        "class PollFixture(BaseIntegration):\n"
        f"    name = \"{name}-poll\"\n"
        "    poll_interval_seconds = 60\n"
        "\n"
        "    def poll(self, manager):\n"
        "        return []\n",
        encoding="utf8",
    )
    return directory


def test_reregistering_integrations_does_not_duplicate_polling(tmp_path):
    """二度 register しても IntegrationManager に積まれる instance は 1 つ。"""
    from saiverse.integration_manager import IntegrationManager

    directory = _integration_addon(tmp_path)
    manager = IntegrationManager(saiverse_manager=None, tick_interval=1)
    assert addon_loader._register_addon_integrations_for(manager, directory) == 1
    assert addon_loader._register_addon_integrations_for(manager, directory) == 1

    names = [i.name for i in manager._integrations]
    assert names == ["poll-addon-poll"]
    assert addon_loader._addon_integration_registry["poll-addon"] == ["poll-addon-poll"]


def test_reregistering_removes_every_stale_integration(tmp_path):
    """integration が複数あるアドオンでも、再登録は古い分を全部外す。"""
    from saiverse.integration_manager import IntegrationManager

    directory = _integration_addon(tmp_path, "multi-addon")
    (directory / "integrations" / "second.py").write_text(
        "from saiverse.integrations.base import BaseIntegration\n"
        "\n"
        "\n"
        "class SecondFixture(BaseIntegration):\n"
        "    name = \"multi-addon-second\"\n"
        "    poll_interval_seconds = 60\n"
        "\n"
        "    def poll(self, manager):\n"
        "        return []\n",
        encoding="utf8",
    )
    manager = IntegrationManager(saiverse_manager=None, tick_interval=1)
    assert addon_loader._register_addon_integrations_for(manager, directory) == 2
    assert addon_loader._register_addon_integrations_for(manager, directory) == 2

    names = sorted(i.name for i in manager._integrations)
    assert names == ["multi-addon-poll", "multi-addon-second"]
    assert sorted(addon_loader._addon_integration_registry["multi-addon"]) == [
        "multi-addon-poll", "multi-addon-second",
    ]
