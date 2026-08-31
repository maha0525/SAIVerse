"""addon_external_loader の動作検証 (再発防止テスト)。

検証ポイント:
1. 隔離機構なしで GPT-SoVITS の `from tools.audio_sr` を再現するとホスト側
   tools と衝突して ImportError になる(ベースライン)
2. register_addon_external を呼んだ後は同じ import が `addons.<addon>.tools.X`
   へリダイレクトされて成功する
3. **同じプロセス内**でホスト側コードからの `import tools` は引き続きホスト
   tools を返す(リダイレクトは external/ 配下からの呼び出しのみ)
4. **複数スレッドでの並列 import** で本体 tools とパック側 tools が混ざらない
   (今回の不具合の再発検証)

テストでは、ホスト側 ``tools`` パッケージを実物ではなくスタブで再現する
(実物は重い依存をロードするため、テスト環境では不安定 + 副作用大)。
スタブの責務は "host top-level パッケージとして検出される + import 経路上
存在する" だけで十分。
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from textwrap import dedent

# プロジェクトルートを sys.path へ
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _make_fake_addon(tmp: Path, addon_name: str = "fake-pack") -> Path:
    """テスト用に minimal な expansion_data 構造を作る。

    layout:
        tmp/
          expansion_data/
            fake-pack/
              external/
                FakeUpstream/
                  tools/
                    __init__.py        ← from tools.audio_sr import X が成立するか検証
                    audio_sr.py        ← 'PACK_TOOLS_AUDIO_SR' を export
                  consumer.py          ← 上流コードの代理(absolute import を行う)
    """
    addon_dir = tmp / "expansion_data" / addon_name
    external_dir = addon_dir / "external" / "FakeUpstream"
    tools_dir = external_dir / "tools"
    tools_dir.mkdir(parents=True)

    (tools_dir / "__init__.py").write_text(
        "PACK_TOOLS_INIT = 'pack-tools'\n", encoding="utf-8"
    )
    (tools_dir / "audio_sr.py").write_text(
        "PACK_TOOLS_AUDIO_SR = 'pack-audio-sr'\n", encoding="utf-8"
    )

    consumer_py = external_dir / "consumer.py"
    consumer_py.write_text(
        dedent(
            """
            # 上流ライブラリの代理。本体 tools/ ではなく自分の tools/ を見たい。
            from tools.audio_sr import PACK_TOOLS_AUDIO_SR
            from tools import PACK_TOOLS_INIT

            def get_value():
                return (PACK_TOOLS_INIT, PACK_TOOLS_AUDIO_SR)
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return external_dir


def _install_host_tools_stub() -> types.ModuleType:
    """ホスト側 ``tools`` パッケージのスタブを sys.modules に注入する。

    本物の ``tools/`` は重い依存を伴うため、テストでは衝突再現の最小条件
    (sys.modules に存在し、HOST_TOOLS_INIT 属性を持つ)だけ満たすスタブを使う。
    """
    if "tools" in sys.modules:
        existing = sys.modules["tools"]
        if hasattr(existing, "HOST_TOOLS_INIT"):
            return existing
    stub = types.ModuleType("tools")
    stub.HOST_TOOLS_INIT = "host-tools"  # type: ignore[attr-defined]
    stub.__path__ = []  # type: ignore[attr-defined]  # 名前空間パッケージ扱い
    sys.modules["tools"] = stub
    return stub


class _SaveImportEnv:
    """テスト前後で sys.modules / sys.path / __import__ を保存・復元する。"""

    def __enter__(self):
        import builtins
        self._modules_backup = dict(sys.modules)
        self._path_backup = list(sys.path)
        self._import_backup = builtins.__import__
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        import builtins
        builtins.__import__ = self._import_backup
        # 新規追加分を削除
        for name in list(sys.modules):
            if name not in self._modules_backup:
                del sys.modules[name]
        # 元の状態に戻す
        for name, mod in self._modules_backup.items():
            sys.modules[name] = mod
        sys.path[:] = self._path_backup
        # addon_external_loader の内部状態をリセット
        ael = sys.modules.get("saiverse.addon_external_loader")
        if ael is not None:
            ael._registrations.clear()
            ael._import_patched = False
            ael._HOST_TOP_LEVEL_CACHE = None


class AddonExternalLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp_handle = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_handle.name)
        self.external_dir = _make_fake_addon(self.tmp)
        self.expansion_addon_root = self.tmp / "expansion_data" / "fake-pack" / "external"

    def tearDown(self):
        self.tmp_handle.cleanup()

    def _patch_host_top_level(self, ael) -> None:
        """テスト用に host top-level に 'tools' を含めさせる。

        本物の SAIVerse/ には tools/ があるが、テストでは saiverse 自体を
        sys.path 経由でロードしている可能性があり、自動検出が安定しない。
        """
        ael._HOST_TOP_LEVEL_CACHE = {"tools"}

    def _import_consumer(self) -> types.ModuleType:
        """external/FakeUpstream/consumer.py を毎回ユニーク名で import。"""
        import uuid
        unique_name = f"fakepack_consumer_{uuid.uuid4().hex}"
        sys.path.insert(0, str(self.external_dir))
        try:
            spec = importlib.util.spec_from_file_location(
                unique_name, str(self.external_dir / "consumer.py")
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[unique_name] = module
            spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(self.external_dir))

    # ------------------------------------------------------------------
    # baseline: without isolation, host's `tools` shadows pack's tools
    # ------------------------------------------------------------------
    def test_baseline_collision_without_isolation(self):
        """隔離機構なしでは、ホスト側 tools (スタブ、audio_sr 無し) が
        sys.modules に存在する状態で consumer.py の `from tools.audio_sr` が
        ImportError になることを再現する。"""
        with _SaveImportEnv():
            _install_host_tools_stub()

            with self.assertRaises((ImportError, ModuleNotFoundError)):
                self._import_consumer()

    # ------------------------------------------------------------------
    # with isolation: redirect resolves
    # ------------------------------------------------------------------
    def test_isolation_redirects_to_addon_namespace(self):
        with _SaveImportEnv():
            _install_host_tools_stub()
            ael = importlib.import_module("saiverse.addon_external_loader")
            self._patch_host_top_level(ael)

            ael.register_addon_external("fake-pack", self.expansion_addon_root)

            consumer = self._import_consumer()
            self.assertEqual(
                consumer.get_value(), ("pack-tools", "pack-audio-sr")
            )
            self.assertIn("addons.fake_pack.tools", sys.modules)

    # ------------------------------------------------------------------
    # host-side `import tools` still resolves to host's tools after registration
    # ------------------------------------------------------------------
    def test_host_imports_unaffected(self):
        with _SaveImportEnv():
            host_tools_before = _install_host_tools_stub()
            ael = importlib.import_module("saiverse.addon_external_loader")
            self._patch_host_top_level(ael)

            ael.register_addon_external("fake-pack", self.expansion_addon_root)

            # ホスト側コード(本テストファイル自身)からの import は本体 tools を返す
            import tools as host_tools_after
            self.assertIs(host_tools_after, host_tools_before)
            self.assertEqual(host_tools_after.HOST_TOOLS_INIT, "host-tools")
            self.assertFalse(hasattr(host_tools_after, "PACK_TOOLS_INIT"))

    # ------------------------------------------------------------------
    # parallel: thread A loads pack repeatedly, thread B imports host tools
    # — neither sees contamination
    # ------------------------------------------------------------------
    def test_parallel_safety(self):
        """今回再発した不具合の検証: 並列で external/ ロードと host import が
        走っても、互いに名前空間を混染しない。"""
        with _SaveImportEnv():
            _install_host_tools_stub()
            ael = importlib.import_module("saiverse.addon_external_loader")
            self._patch_host_top_level(ael)

            ael.register_addon_external("fake-pack", self.expansion_addon_root)

            errors: list = []
            barrier = threading.Barrier(2)

            def thread_a_loads_pack():
                """external/ から import — addon-namespaced tools を期待"""
                try:
                    barrier.wait()
                    for _ in range(50):
                        consumer = self._import_consumer()
                        if consumer.get_value() != ("pack-tools", "pack-audio-sr"):
                            errors.append(("thread_a", consumer.get_value()))
                except Exception as e:
                    errors.append(("thread_a_exc", repr(e)))

            def thread_b_imports_host_tools():
                """host 側コードから import — host tools を期待"""
                try:
                    barrier.wait()
                    for _ in range(50):
                        host_tools = importlib.import_module("tools")
                        if hasattr(host_tools, "PACK_TOOLS_INIT"):
                            errors.append(("thread_b_contamination",))
                        if not hasattr(host_tools, "HOST_TOOLS_INIT"):
                            errors.append(("thread_b_lost_host_attr",))
                except Exception as e:
                    errors.append(("thread_b_exc", repr(e)))

            t1 = threading.Thread(target=thread_a_loads_pack)
            t2 = threading.Thread(target=thread_b_imports_host_tools)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(errors, [], f"unexpected errors: {errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
