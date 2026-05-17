"""Tests for saiverse-stackchan-addon の avatar_pipeline.py + api_routes.py
(Phase 4.5-d-1: WIP storage + REST API skeleton)。

addon ファイルは spec_from_file_location 経路でロードされるため、 sys.path
に addon ディレクトリを追加してから import する。 conftest.py の
load_builtin_tool は builtin_data/tools/ 専用なので、 ここでは inline で
ロードする。
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Repo root を sys.path に確保 (= saiverse.addon_paths を import するため)。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_ADDON_DIR = _REPO_ROOT / "expansion_data" / "saiverse-stackchan-addon"


def _load_module(name: str, file_path: Path):
    """addon 内のモジュールを spec_from_file_location でロードする。

    既存の sys.modules entry は削除してから fresh load する (= テスト間で
    モジュール singleton 汚染を回避)。
    """
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to spec module: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_avatar_pipeline():
    """avatar_pipeline を `avatar_pipeline` 名でロード。

    api_routes.py が `from avatar_pipeline import ...` で同名 import するので、
    モジュール名を揃えておかないと別インスタンスが生まれて patch が片側にし
    か効かなくなる。
    """
    return _load_module("avatar_pipeline", _ADDON_DIR / "avatar_pipeline.py")


def _load_api_routes():
    # api_routes が import する `avatar_pipeline` を先に sys.modules に確保。
    _load_avatar_pipeline()
    return _load_module("api_routes", _ADDON_DIR / "api_routes.py")


class AvatarPipelineStorageTests(unittest.TestCase):
    """AvatarPipelineManager のストレージ層ラウンドトリップ。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        # avatar_pipeline モジュールを fresh ロード。
        self.ap = _load_avatar_pipeline()

        # get_addon_storage_path を patch して tempdir を返す。
        self._patch = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.mgr = self.ap.AvatarPipelineManager()

    def test_storage_root_uses_patched_path(self) -> None:
        root = self.mgr.storage_root()
        self.assertEqual(root, self.storage_root / "avatar_sets")

    def test_list_sets_empty(self) -> None:
        sets = self.mgr.list_sets("air_test")
        self.assertEqual(sets, [])

    def test_create_set_then_get(self) -> None:
        info = self.mgr.create_set(
            "air_test", "default", mode="matrix",
            common_prompt="silver hair",
            image_model="nano_banana_2",
        )
        self.assertEqual(info.set_name, "default")
        self.assertEqual(info.persona_id, "air_test")
        self.assertFalse(info.has_finalized)
        self.assertTrue(info.has_wip)
        self.assertIsNotNone(info.wip_metadata)
        self.assertEqual(info.wip_metadata.mode, "matrix")
        self.assertEqual(info.wip_metadata.common_prompt, "silver hair")
        self.assertEqual(info.wip_metadata.image_model, "nano_banana_2")
        # 段階 4 つ (matrix mode) 分の StageState がある。
        self.assertEqual(len(info.wip_stages), 4)
        # 各段階の files は空 (= まだ生成してない)。
        for s in info.wip_stages:
            self.assertEqual(s.files, [])
            self.assertFalse(s.completed)

        # 再取得しても同じ。
        info2 = self.mgr.get_set("air_test", "default")
        self.assertIsNotNone(info2)
        self.assertEqual(info2.wip_metadata.common_prompt, "silver hair")

    def test_create_set_duplicate_raises(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        with self.assertRaises(ValueError) as cm:
            self.mgr.create_set("air_test", "default", mode="matrix")
        self.assertIn("already exists", str(cm.exception))

    def test_create_set_invalid_mode(self) -> None:
        with self.assertRaises(ValueError):
            self.mgr.create_set("air_test", "default", mode="not_a_mode")

    def test_layered_mode_stage_order(self) -> None:
        info = self.mgr.create_set("air_test", "layered_set", mode="layered")
        stage_ids = [s.stage_id for s in info.wip_stages]
        self.assertEqual(stage_ids, [
            self.ap.STAGE_FACE,
            self.ap.STAGE_EXPRESSIONS,
            self.ap.STAGE_LAYERED,
            self.ap.STAGE_TRIMMED,
        ])

    def test_list_sets_after_create(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.create_set("air_test", "yukata", mode="matrix")
        self.mgr.create_set("other_persona", "default", mode="layered")
        sets = self.mgr.list_sets("air_test")
        names = sorted(s.set_name for s in sets)
        self.assertEqual(names, ["default", "yukata"])

    def test_update_metadata(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        old_updated = self.mgr.read_metadata("air_test", "default").updated_at
        # 1 秒未満の差を許容するために sleep 入れずに直接書き換え検証。
        updated = self.mgr.update_metadata(
            "air_test", "default",
            common_prompt="updated prompt",
            parallelism=10,
            trim_rect={"x": 100, "y": 50, "width": 800, "height": 600},
        )
        self.assertEqual(updated.common_prompt, "updated prompt")
        self.assertEqual(updated.parallelism, 10)
        self.assertEqual(updated.trim_rect["x"], 100)
        # updated_at は touch される (= 値が違うか同じ ISO 文字列で OK)。
        self.assertGreaterEqual(updated.updated_at, old_updated)

    def test_update_metadata_unknown_field(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        with self.assertRaises(ValueError):
            self.mgr.update_metadata("air_test", "default", bogus_field=1)

    def test_update_metadata_missing_wip(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.mgr.update_metadata(
                "air_test", "default", common_prompt="x",
            )

    def test_mark_stage_completed(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        meta = self.mgr.mark_stage_completed(
            "air_test", "default", self.ap.STAGE_FACE,
        )
        self.assertIn(self.ap.STAGE_FACE, meta.completed_stages)
        # 二重に呼んでも重複しない。
        meta2 = self.mgr.mark_stage_completed(
            "air_test", "default", self.ap.STAGE_FACE,
        )
        self.assertEqual(
            meta2.completed_stages.count(self.ap.STAGE_FACE), 1,
        )

    def test_mark_stage_invalid_id(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        with self.assertRaises(ValueError):
            self.mgr.mark_stage_completed(
                "air_test", "default", "99_bogus",
            )

    def test_delete_set(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        deleted = self.mgr.delete_set("air_test", "default")
        self.assertTrue(deleted)
        self.assertIsNone(self.mgr.get_set("air_test", "default"))
        # 再度削除しても False (= 404 相当)。
        self.assertFalse(self.mgr.delete_set("air_test", "default"))

    def test_delete_wip_only_keeps_finalized(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        # 確定品ファイルを手で作る (= Phase 4.5-d-3 で作られる形を模擬)。
        sdir = self.mgr.set_dir("air_test", "default")
        (sdir / "avatar.bin").write_bytes(b"\x00" * 100)
        (sdir / "manifest.json").write_text(
            json.dumps({"mode": "matrix", "checksum": "sha256:test"}),
            encoding="utf-8",
        )
        deleted = self.mgr.delete_set(
            "air_test", "default", wip_only=True,
        )
        self.assertTrue(deleted)
        info = self.mgr.get_set("air_test", "default")
        self.assertIsNotNone(info)
        self.assertTrue(info.has_finalized)
        self.assertFalse(info.has_wip)

    def test_delete_set_clears_active(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.set_active("air_test", "default")
        self.assertEqual(self.mgr.get_active("air_test"), "default")
        self.mgr.delete_set("air_test", "default")
        self.assertIsNone(self.mgr.get_active("air_test"))

    def test_active_set(self) -> None:
        self.assertIsNone(self.mgr.get_active("air_test"))
        # 存在しないセットをアクティブにすると FileNotFoundError。
        with self.assertRaises(FileNotFoundError):
            self.mgr.set_active("air_test", "default")
        # 作ってからならアクティブ化可能。
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.set_active("air_test", "default")
        self.assertEqual(self.mgr.get_active("air_test"), "default")
        # is_active が反映される。
        info = self.mgr.get_set("air_test", "default")
        self.assertTrue(info.is_active)
        # クリア。
        self.mgr.set_active("air_test", None)
        self.assertIsNone(self.mgr.get_active("air_test"))

    def test_validate_persona_id_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            self.mgr.create_set("../etc", "default", mode="matrix")
        with self.assertRaises(ValueError):
            self.mgr.create_set("ok", "../../etc", mode="matrix")
        with self.assertRaises(ValueError):
            self.mgr.create_set("ok", "a/b", mode="matrix")

    def test_validate_set_name_no_underscore_prefix(self) -> None:
        # _active.json と衝突回避のため "_" 開始は拒否。
        with self.assertRaises(ValueError):
            self.mgr.create_set("air_test", "_active", mode="matrix")

    def test_metadata_persistence_across_managers(self) -> None:
        self.mgr.create_set(
            "air_test", "default", mode="matrix",
            common_prompt="persist test",
        )
        # 新しい manager インスタンスで読み直しても同じ。
        mgr2 = self.ap.AvatarPipelineManager()
        info = mgr2.get_set("air_test", "default")
        self.assertIsNotNone(info)
        self.assertEqual(info.wip_metadata.common_prompt, "persist test")

    def test_finalized_reflected_in_set_info(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        sdir = self.mgr.set_dir("air_test", "default")
        (sdir / "avatar.bin").write_bytes(b"\x00" * 100)
        (sdir / "manifest.json").write_text(
            json.dumps({
                "mode": "matrix",
                "checksum": "sha256:abc123",
            }),
            encoding="utf-8",
        )
        info = self.mgr.get_set("air_test", "default")
        self.assertTrue(info.has_finalized)
        self.assertEqual(info.finalized_mode, "matrix")
        self.assertEqual(info.finalized_checksum, "sha256:abc123")


class StageExecutorHookTests(unittest.TestCase):
    """Phase 4.5-d-2 で hook 注入される段階実行 / 単発再生成の動作。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        self.ap = _load_avatar_pipeline()
        self._patch = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.mgr = self.ap.AvatarPipelineManager()
        self.mgr.create_set("air_test", "default", mode="matrix")

    def test_execute_stage_without_hook_raises(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_FACE,
            )

    def test_regenerate_without_hook_raises(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.mgr.regenerate_target(
                "air_test", "default", self.ap.STAGE_FACE, target="face",
            )

    def test_register_stage_executor_then_invoked(self) -> None:
        calls = []

        def fake_executor(mgr, persona_id, set_name, stage_id, params):
            calls.append((persona_id, set_name, stage_id, params))
            return {"ok": True, "stage": stage_id}

        self.mgr.register_stage_executor(fake_executor)
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_FACE,
            params={"prompt": "test"},
        )
        self.assertEqual(result, {"ok": True, "stage": self.ap.STAGE_FACE})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "air_test")
        self.assertEqual(calls[0][1], "default")
        self.assertEqual(calls[0][2], self.ap.STAGE_FACE)
        self.assertEqual(calls[0][3], {"prompt": "test"})

    def test_register_regenerate_executor_then_invoked(self) -> None:
        calls = []

        def fake_regen(mgr, persona_id, set_name, stage_id, target, params):
            calls.append((persona_id, set_name, stage_id, target, params))
            return {"ok": True, "target": target}

        self.mgr.register_regenerate_executor(fake_regen)
        result = self.mgr.regenerate_target(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
            target="happy", params={"extra": "smile"},
        )
        self.assertEqual(result, {"ok": True, "target": "happy"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], "happy")


class ApiRoutesTests(unittest.TestCase):
    """api_routes.py を FastAPI TestClient 経由でラウンドトリップ。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        # 順序が重要: _load_api_routes が内部で avatar_pipeline を fresh
        # load するので、 先に api_routes をロードしてから sys.modules から
        # 同一インスタンスを取得。 これをやらないと self.ap への patch が
        # api_routes 経由で参照される `avatar_pipeline` (= 別インスタンス)
        # に効かない。
        self.api = _load_api_routes()
        self.ap = sys.modules["avatar_pipeline"]

        # avatar_pipeline の get_addon_storage_path を patch。
        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        # singleton をリセット (= patched storage を見る新インスタンスを作る)。
        self.ap._singleton = None

        # FastAPI app に router を含めて TestClient で叩く。
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)

    def test_list_sets_empty(self) -> None:
        resp = self.client.get("/avatar_sets/air_test")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["persona_id"], "air_test")
        self.assertIsNone(data["active_set_name"])
        self.assertEqual(data["sets"], [])

    def test_create_get_update_delete_roundtrip(self) -> None:
        # Create
        resp = self.client.post(
            "/avatar_sets/air_test/default",
            json={
                "mode": "matrix",
                "common_prompt": "silver hair",
                "image_model": "nano_banana_2",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        info = resp.json()
        self.assertEqual(info["set_name"], "default")
        self.assertEqual(info["wip_metadata"]["mode"], "matrix")

        # Get
        resp = self.client.get("/avatar_sets/air_test/default")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["wip_metadata"]["common_prompt"], "silver hair",
        )

        # Update metadata
        resp = self.client.patch(
            "/avatar_sets/air_test/default/metadata",
            json={
                "common_prompt": "updated",
                "parallelism": 8,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        meta = resp.json()
        self.assertEqual(meta["common_prompt"], "updated")
        self.assertEqual(meta["parallelism"], 8)

        # List
        resp = self.client.get("/avatar_sets/air_test")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["sets"]), 1)

        # Delete
        resp = self.client.delete("/avatar_sets/air_test/default")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])

        # Get after delete -> 404
        resp = self.client.get("/avatar_sets/air_test/default")
        self.assertEqual(resp.status_code, 404)

    def test_create_duplicate_returns_409(self) -> None:
        resp1 = self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "matrix"},
        )
        self.assertEqual(resp1.status_code, 201)
        resp2 = self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "matrix"},
        )
        self.assertEqual(resp2.status_code, 409)

    def test_create_invalid_mode_returns_400(self) -> None:
        resp = self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "bogus"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_wip_only(self) -> None:
        self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "matrix"},
        )
        # 確定品ファイルを手で作る (= 4.5-d-3 で作られる形を模擬)。
        mgr = self.ap.get_avatar_pipeline_manager()
        sdir = mgr.set_dir("air_test", "default")
        (sdir / "avatar.bin").write_bytes(b"\x00" * 100)
        (sdir / "manifest.json").write_text(
            json.dumps({"mode": "matrix", "checksum": "sha256:x"}),
            encoding="utf-8",
        )
        resp = self.client.delete(
            "/avatar_sets/air_test/default?wip_only=true",
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        # 確定品は残っている。
        resp2 = self.client.get("/avatar_sets/air_test/default")
        self.assertEqual(resp2.status_code, 200)
        info = resp2.json()
        self.assertTrue(info["has_finalized"])
        self.assertFalse(info["has_wip"])

    def test_active_set_endpoints(self) -> None:
        # 未設定 -> None。
        resp = self.client.get("/avatar_sets/air_test/active")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["set_name"])

        # 存在しないセットをアクティブにしようとすると 404。
        resp = self.client.post(
            "/avatar_sets/air_test/active",
            json={"set_name": "default"},
        )
        self.assertEqual(resp.status_code, 404)

        # 作ってからアクティブにする。
        self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "matrix"},
        )
        resp = self.client.post(
            "/avatar_sets/air_test/active",
            json={"set_name": "default"},
        )
        self.assertEqual(resp.status_code, 200)
        # 確認。
        resp = self.client.get("/avatar_sets/air_test/active")
        self.assertEqual(resp.json()["set_name"], "default")
        # クリア。
        resp = self.client.post(
            "/avatar_sets/air_test/active", json={"set_name": None},
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/avatar_sets/air_test/active")
        self.assertIsNone(resp.json()["set_name"])

    def test_stage_complete(self) -> None:
        self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "matrix"},
        )
        resp = self.client.post(
            "/avatar_sets/air_test/default/stages/01_face/complete",
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("01_face", resp.json()["completed_stages"])

    def test_stage_complete_invalid_stage(self) -> None:
        self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "matrix"},
        )
        resp = self.client.post(
            "/avatar_sets/air_test/default/stages/99_bogus/complete",
        )
        self.assertEqual(resp.status_code, 400)

    def test_execute_stage_without_hook_returns_501(self) -> None:
        self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "matrix"},
        )
        resp = self.client.post(
            "/avatar_sets/air_test/default/stages/01_face",
            json={"params": {}},
        )
        self.assertEqual(resp.status_code, 501)

    def test_regenerate_without_hook_returns_501(self) -> None:
        self.client.post(
            "/avatar_sets/air_test/default", json={"mode": "matrix"},
        )
        resp = self.client.post(
            "/avatar_sets/air_test/default/stages/02_expressions/regenerate",
            json={"target": "happy", "params": {}},
        )
        self.assertEqual(resp.status_code, 501)


class AvatarGeneratorTests(unittest.TestCase):
    """avatar_generator (Phase 4.5-d-2) を mock backend で end-to-end 検証。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        # 順序重要 (= ApiRoutesTests と同じ)。
        self.ap = _load_avatar_pipeline()
        self.gen = _load_module(
            "avatar_generator", _ADDON_DIR / "avatar_generator.py",
        )

        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        # avatar_generator の _generate_one (= image_generator backend) を mock。
        # 各呼び出しでカウンタを増やして PNG bytes を返す。
        self._gen_calls: list[dict] = []
        self._gen_counter = [0]

        def fake_generate_one(prompt, model, input_image_paths=None,
                              aspect_ratio="1:1", quality="high"):
            self._gen_counter[0] += 1
            self._gen_calls.append({
                "prompt": prompt,
                "model": model,
                "input_image_paths": [
                    str(p) for p in (input_image_paths or [])
                ],
            })
            # ダミー PNG (PNG signature + 適当な bytes)。
            return (
                b"\x89PNG\r\n\x1a\n" + bytes([self._gen_counter[0] % 256]) * 32,
                "image/png",
            )

        self._patch_gen = patch.object(
            self.gen, "_generate_one", fake_generate_one,
        )
        self._patch_gen.start()
        self.addCleanup(self._patch_gen.stop)

        # singleton リセット (= patched storage を見る新インスタンス) と
        # executor 注入。
        self.ap._singleton = None
        self.mgr = self.ap.get_avatar_pipeline_manager()
        self.gen.register_avatar_executors(self.mgr)

        # テスト用セット。
        self.mgr.create_set(
            "air_test", "default", mode="matrix",
            common_prompt="silver hair, blue eyes",
        )

    def test_generate_stage_face_writes_png(self) -> None:
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_FACE,
        )
        self.assertEqual(result["stage_id"], self.ap.STAGE_FACE)
        self.assertEqual(len(result["files"]), 1)
        out = Path(result["files"][0]["path"])
        self.assertTrue(out.exists())
        self.assertGreater(out.stat().st_size, 0)
        self.assertEqual(len(self._gen_calls), 1)
        # 共通プロンプトが連結されている。
        self.assertIn("silver hair", self._gen_calls[0]["prompt"])

    def test_generate_stage_face_with_extra_prompt_override(self) -> None:
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_FACE,
            params={"extra_prompt": "wearing a red yukata"},
        )
        self.assertEqual(len(result["files"]), 1)
        self.assertIn("wearing a red yukata", self._gen_calls[0]["prompt"])
        # metadata に永続化されている (= 次回 execute_stage でも同じプロンプト)。
        meta = self.mgr.read_metadata("air_test", "default")
        self.assertEqual(
            meta.extra_prompts[self.ap.STAGE_FACE]["face"],
            "wearing a red yukata",
        )

    def test_generate_stage_expressions_requires_face(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_EXPRESSIONS,
            )

    def test_generate_stage_expressions_creates_5_files(self) -> None:
        # ① を先に実行。
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self._gen_calls.clear()

        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        self.assertEqual(result["stage_id"], self.ap.STAGE_EXPRESSIONS)
        self.assertEqual(len(result["files"]), 5)
        self.assertEqual(result["errors"], [])
        # 各表情ファイルが存在する。
        for expr in self.ap.EXPRESSION_NAMES:
            p = self.mgr.stage_dir(
                "air_test", "default", self.ap.STAGE_EXPRESSIONS,
            ) / f"{expr}.png"
            self.assertTrue(p.exists(), f"missing {p}")
        # 5 回 generate 呼ばれた + 各々で face.png を input_images に渡してる。
        self.assertEqual(len(self._gen_calls), 5)
        for call in self._gen_calls:
            self.assertEqual(len(call["input_image_paths"]), 1)
            self.assertTrue(
                call["input_image_paths"][0].endswith("face.png"),
            )

    def test_generate_stage_matrix_creates_90_files(self) -> None:
        # ①② を先に実行。
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_EXPRESSIONS)
        self._gen_calls.clear()

        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_MATRIX,
        )
        self.assertEqual(result["stage_id"], self.ap.STAGE_MATRIX)
        # 84 枚生成 + 6 枚 base コピー = 90 枚。
        self.assertEqual(len(result["files"]), 90)
        self.assertEqual(result["errors"], [])
        # ファイル実在確認 (= 90 枚全部)。
        stage_dir = self.mgr.stage_dir(
            "air_test", "default", self.ap.STAGE_MATRIX,
        )
        png_files = sorted(stage_dir.glob("*.png"))
        self.assertEqual(len(png_files), 90)
        # 生成回数は 84 (= base 6 はコピーで生成しない)。
        self.assertEqual(len(self._gen_calls), 84)
        # base ファイル名規約: {face}_{eyes}_{mouth}.png
        for face in self.ap.FACE_NAMES:
            base_file = stage_dir / f"{face}_open_closed.png"
            self.assertTrue(base_file.exists())

    def test_layered_mode_creates_8_parts(self) -> None:
        # layered mode の専用セット。
        self.mgr.create_set(
            "air_test", "layered_set", mode="layered",
            common_prompt="silver hair",
        )
        self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_FACE,
        )
        self._gen_calls.clear()

        result = self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_LAYERED,
        )
        self.assertEqual(len(result["files"]), 8)
        self.assertEqual(result["errors"], [])
        # eyes=open / mouth=closed は copy で対応 (2 件)、 残り 6 件生成
        # (= まはー検証 2026-05-17、 「触る指示」 を渡さないことで安定)。
        self.assertEqual(len(self._gen_calls), 6)
        stage_dir = self.mgr.stage_dir(
            "air_test", "layered_set", self.ap.STAGE_LAYERED,
        )
        for eyes_state in self.ap.EYES_STATES:
            self.assertTrue((stage_dir / f"eyes_{eyes_state}.png").exists())
        for mouth_shape in self.ap.MOUTH_SHAPES:
            self.assertTrue((stage_dir / f"mouth_{mouth_shape}.png").exists())

    def test_regenerate_face(self) -> None:
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self._gen_calls.clear()

        result = self.mgr.regenerate_target(
            "air_test", "default", self.ap.STAGE_FACE, target="face",
            params={"extra_prompt": "with cat ears"},
        )
        self.assertEqual(result["stage_id"], self.ap.STAGE_FACE)
        # 1 回 generate 呼ばれて override が反映されてる。
        self.assertEqual(len(self._gen_calls), 1)
        self.assertIn("with cat ears", self._gen_calls[0]["prompt"])

    def test_regenerate_expression(self) -> None:
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_EXPRESSIONS)
        self._gen_calls.clear()

        result = self.mgr.regenerate_target(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
            target="happy", params={"extra_prompt": "huge grin"},
        )
        self.assertEqual(result["target"], "happy")
        self.assertEqual(len(self._gen_calls), 1)
        self.assertIn("huge grin", self._gen_calls[0]["prompt"])
        # metadata 永続化。
        meta = self.mgr.read_metadata("air_test", "default")
        self.assertEqual(
            meta.extra_prompts[self.ap.STAGE_EXPRESSIONS]["happy"],
            "huge grin",
        )

    def test_regenerate_matrix_base_uses_copy(self) -> None:
        """base (eyes=open, mouth=closed) は generate せずコピーで再現。"""
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_EXPRESSIONS)
        self._gen_calls.clear()

        result = self.mgr.regenerate_target(
            "air_test", "default", self.ap.STAGE_MATRIX,
            target="happy_open_closed",
        )
        # generate は呼ばれない (= base コピー)。
        self.assertEqual(len(self._gen_calls), 0)
        self.assertTrue(result.get("copied_from_base"))
        p = Path(result["path"])
        self.assertTrue(p.exists())

    def test_regenerate_matrix_invalid_target_format(self) -> None:
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_EXPRESSIONS)
        with self.assertRaises(ValueError):
            self.mgr.regenerate_target(
                "air_test", "default", self.ap.STAGE_MATRIX,
                target="invalid_format",
            )

    def test_unknown_image_model_raises(self) -> None:
        self.mgr.update_metadata(
            "air_test", "default", image_model="not_a_model",
        )
        # _generate_one が呼ばれる前に validate。
        with patch.object(
            self.gen, "_generate_one",
            side_effect=lambda **kw: (_ for _ in ()).throw(
                ValueError(f"Unknown image model: {kw['model']!r}")
            ),
        ):
            with self.assertRaises(ValueError):
                self.mgr.execute_stage(
                    "air_test", "default", self.ap.STAGE_FACE,
                )

    def test_template_defaults_used_when_extra_missing(self) -> None:
        """meta.extra_prompts が空でも DEFAULT_TEMPLATES が使われる。"""
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        # ② で extra_prompts 未設定でも default テンプレで生成される。
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        self.assertEqual(len(result["files"]), 5)
        # happy の生成 call で default テンプレが含まれているはず。
        happy_prompt = None
        for call in self._gen_calls:
            if "Smiling brightly" in call["prompt"]:
                happy_prompt = call["prompt"]
                break
        self.assertIsNotNone(happy_prompt)
        self.assertIn("silver hair", happy_prompt)  # 共通プロンプトも入ってる


class AvatarFinalizerTests(unittest.TestCase):
    """avatar_finalizer (Phase 4.5-d-3) を mock backend で end-to-end 検証。

    ④ trim と ⑤ finalize は実 Pillow で動かす (= 軽量、 依存は既存)、
    ⑥ transfer は avatar_loader の MCP 呼び出しを mock する。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        self.ap = _load_avatar_pipeline()
        self.gen = _load_module(
            "avatar_generator", _ADDON_DIR / "avatar_generator.py",
        )
        self.fin = _load_module(
            "avatar_finalizer", _ADDON_DIR / "avatar_finalizer.py",
        )

        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        # _generate_one を mock (= 256×256 dummy PNG を返す。
        # ④ trim と ⑤ finalize は実 PIL で動かしたいので、 実 PNG を
        # 生成する fake にする)。
        from PIL import Image
        import io

        def fake_generate_one(prompt, model, input_image_paths=None,
                              aspect_ratio="1:1", quality="high"):
            # 256×256 の単色 PNG を生成 (= 各呼び出しで色を変えて区別可能に)。
            color = (
                hash(prompt) & 0xFF,
                (hash(prompt) >> 8) & 0xFF,
                (hash(prompt) >> 16) & 0xFF,
            )
            im = Image.new("RGB", (256, 256), color)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue(), "image/png"

        self._patch_gen = patch.object(
            self.gen, "_generate_one", fake_generate_one,
        )
        self._patch_gen.start()
        self.addCleanup(self._patch_gen.stop)

        self.ap._singleton = None
        self.mgr = self.ap.get_avatar_pipeline_manager()
        self.gen.register_avatar_executors(self.mgr)

    def _create_matrix_set_with_stages_123(self) -> None:
        """テスト準備: matrix mode セット作成 + ①②③ を回す。"""
        self.mgr.create_set(
            "air_test", "default", mode="matrix",
            common_prompt="silver hair",
        )
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_MATRIX)

    def _create_layered_set_with_stages_123(self) -> None:
        """テスト準備: layered mode セット作成 + ①②③ を回す。"""
        self.mgr.create_set(
            "air_test", "layered_set", mode="layered",
            common_prompt="silver hair",
        )
        self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_FACE,
        )
        self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_EXPRESSIONS,
        )
        self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_LAYERED,
        )

    def test_trim_requires_trim_rect(self) -> None:
        self._create_matrix_set_with_stages_123()
        with self.assertRaises(ValueError) as cm:
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_TRIMMED,
            )
        self.assertIn("trim_rect", str(cm.exception))

    def test_trim_matrix_writes_90_pngs(self) -> None:
        self._create_matrix_set_with_stages_123()
        rect = {"x": 50, "y": 30, "width": 160, "height": 120}
        self.mgr.update_metadata("air_test", "default", trim_rect=rect)
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        self.assertEqual(result["stage_id"], self.ap.STAGE_TRIMMED)
        self.assertEqual(len(result["files"]), 90)
        self.assertEqual(result["errors"], [])
        trimmed_dir = self.mgr.stage_dir(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        pngs = sorted(trimmed_dir.glob("*.png"))
        self.assertEqual(len(pngs), 90)
        # 1 枚開いてサイズ確認 (= rect 通り)。
        from PIL import Image
        with Image.open(pngs[0]) as im:
            self.assertEqual(im.size, (160, 120))

    def test_trim_layered_writes_14_pngs(self) -> None:
        self._create_layered_set_with_stages_123()
        rect = {"x": 10, "y": 10, "width": 200, "height": 200}
        result = self.fin.generate_stage_trim(
            self.mgr, "air_test", "layered_set",
            {"trim_rect": rect},
        )
        self.assertEqual(len(result["files"]), 14)
        # 命名規約: face_<name>, eyes_<state>, mouth_<shape>
        trimmed_dir = self.mgr.stage_dir(
            "air_test", "layered_set", self.ap.STAGE_TRIMMED,
        )
        for face in self.ap.FACE_NAMES:
            self.assertTrue((trimmed_dir / f"face_{face}.png").exists())
        for eyes in self.ap.EYES_STATES:
            self.assertTrue((trimmed_dir / f"eyes_{eyes}.png").exists())
        for mouth in self.ap.MOUTH_SHAPES:
            self.assertTrue((trimmed_dir / f"mouth_{mouth}.png").exists())

    def test_trim_persists_rect_when_passed_via_params(self) -> None:
        self._create_matrix_set_with_stages_123()
        rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.fin.generate_stage_trim(
            self.mgr, "air_test", "default", {"trim_rect": rect},
        )
        meta = self.mgr.read_metadata("air_test", "default")
        self.assertEqual(meta.trim_rect, rect)

    def test_trim_with_per_variant_overrides_matrix(self) -> None:
        """④ matrix の variant 単位 trim_rect_overrides (= face 単位)。

        まはー検証 2026-05-17: 同 face の 15 セルは同 rect で十分なので
        per-target ではなく face name を key にする。
        """
        self._create_matrix_set_with_stages_123()
        default_rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        # default + happy face 全体だけ別 rect (= key は face 名)。
        self.mgr.update_metadata(
            "air_test", "default",
            trim_rect=default_rect,
            trim_rect_overrides={
                "happy": {
                    "x": 50, "y": 50, "width": 100, "height": 100,
                },
            },
        )
        result = self.fin.generate_stage_trim(
            self.mgr, "air_test", "default", {},
        )
        # happy_* は全 15 件 override 使う、 他 face は default。
        by_target = {f["target"]: f for f in result["files"]}
        for eyes in ["open", "half", "closed"]:
            for mouth in ["closed", "half", "open", "e", "u"]:
                t = f"happy_{eyes}_{mouth}"
                self.assertTrue(
                    by_target[t].get("used_override"),
                    f"{t} should use override",
                )
                self.assertEqual(
                    by_target[t].get("variant_key"), "happy",
                )
        # idle face は override なし。
        self.assertFalse(
            by_target["idle_open_closed"].get("used_override"),
        )
        # 実ファイルサイズ確認。
        from PIL import Image
        with Image.open(by_target["happy_open_half"]["path"]) as im:
            self.assertEqual(im.size, (100, 100))
        with Image.open(by_target["idle_open_closed"]["path"]) as im:
            self.assertEqual(im.size, (256, 256))

    def test_trim_with_per_target_overrides_layered(self) -> None:
        """④ layered の variant 単位 trim_rect_overrides (= target 単位)。

        layered では face / eyes / mouth が独立画像なので、 target 名を
        そのまま key にする (= matrix と違って「face」 グルーピングなし)。
        """
        self._create_layered_set_with_stages_123()
        default_rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.mgr.update_metadata(
            "air_test", "layered_set",
            trim_rect=default_rect,
            trim_rect_overrides={
                "eyes_open": {
                    "x": 30, "y": 30, "width": 80, "height": 80,
                },
            },
        )
        result = self.fin.generate_stage_trim(
            self.mgr, "air_test", "layered_set", {},
        )
        by_target = {f["target"]: f for f in result["files"]}
        self.assertTrue(by_target["eyes_open"].get("used_override"))
        self.assertEqual(
            by_target["eyes_open"].get("variant_key"), "eyes_open",
        )
        self.assertFalse(by_target["eyes_half"].get("used_override"))

    def test_trim_face_filter_matrix(self) -> None:
        """④ matrix の face_filter で同 face の 15 セルだけ trim、
        他 face は触らない。"""
        self._create_matrix_set_with_stages_123()
        default_rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.mgr.update_metadata(
            "air_test", "default", trim_rect=default_rect,
        )
        # 全件 trim 実行。
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        # happy だけ rect 上書きして face_filter で再 trim。
        self.mgr.update_metadata(
            "air_test", "default",
            trim_rect_overrides={
                "happy": {
                    "x": 10, "y": 10, "width": 100, "height": 100,
                },
            },
        )
        result = self.fin.generate_stage_trim(
            self.mgr, "air_test", "default",
            {"face_filter": "happy"},
        )
        # 15 件だけ処理される。
        self.assertEqual(len(result["files"]), 15)
        # 全部 happy 系。
        for f in result["files"]:
            self.assertTrue(f["target"].startswith("happy_"))
        # サイズ確認。
        from PIL import Image
        trimmed_dir = self.mgr.stage_dir(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        with Image.open(trimmed_dir / "happy_half_e.png") as im:
            self.assertEqual(im.size, (100, 100))
        # idle 系は前回 trim の (256, 256) のまま (= 触られてない)。
        with Image.open(trimmed_dir / "idle_open_closed.png") as im:
            self.assertEqual(im.size, (256, 256))

    def test_trim_single_target(self) -> None:
        """④ params.only_target で 1 枚だけ trim (= 他の trimmed/ は残る)。"""
        self._create_matrix_set_with_stages_123()
        default_rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.mgr.update_metadata(
            "air_test", "default", trim_rect=default_rect,
        )
        # 全件 trim 実行。
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        trimmed_dir = self.mgr.stage_dir(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        initial_count = len(list(trimmed_dir.glob("*.png")))
        self.assertEqual(initial_count, 90)
        # per-target override 設定 → 単発 trim。
        self.mgr.update_metadata(
            "air_test", "default",
            trim_rect_overrides={
                "happy_open_closed": {
                    "x": 10, "y": 10, "width": 100, "height": 100,
                },
            },
        )
        result = self.fin.generate_stage_trim(
            self.mgr, "air_test", "default",
            {"only_target": "happy_open_closed"},
        )
        # 1 件だけ trim、 他の 89 件は残ってる。
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(
            len(list(trimmed_dir.glob("*.png"))), 90,
        )

    def test_trim_clears_old_trimmed(self) -> None:
        """再 trim 時に古い trimmed を消してから書く (= 部分残し防止)。"""
        self._create_matrix_set_with_stages_123()
        rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.mgr.update_metadata("air_test", "default", trim_rect=rect)
        # 初回 trim。
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        trimmed_dir = self.mgr.stage_dir(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        # 余分なファイルを手動で置く。
        (trimmed_dir / "junk.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        self.assertTrue((trimmed_dir / "junk.png").exists())
        # 再 trim でクリアされる。
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        self.assertFalse((trimmed_dir / "junk.png").exists())

    def test_finalize_matrix_produces_correct_bytes(self) -> None:
        self._create_matrix_set_with_stages_123()
        rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.mgr.update_metadata("air_test", "default", trim_rect=rect)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        result = self.fin.finalize_avatar_set(
            self.mgr, "air_test", "default",
        )
        # サイズ確認。
        self.assertEqual(result["bytes"], self.fin.MATRIX_TOTAL_BYTES)
        self.assertEqual(result["frames"], 90)
        self.assertEqual(result["mode"], "matrix")
        self.assertTrue(result["checksum"].startswith("sha256:"))
        # avatar.bin / manifest.json が書き出されている。
        bin_path = Path(result["bin_path"])
        manifest_path = Path(result["manifest_path"])
        self.assertTrue(bin_path.exists())
        self.assertTrue(manifest_path.exists())
        self.assertEqual(bin_path.stat().st_size, self.fin.MATRIX_TOTAL_BYTES)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "matrix")
        self.assertEqual(manifest["checksum"], result["checksum"])

    def test_finalize_layered_produces_correct_bytes(self) -> None:
        self._create_layered_set_with_stages_123()
        rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.fin.generate_stage_trim(
            self.mgr, "air_test", "layered_set",
            {"trim_rect": rect},
        )
        result = self.fin.finalize_avatar_set(
            self.mgr, "air_test", "layered_set",
        )
        self.assertEqual(result["bytes"], self.fin.LAYERED_TOTAL_BYTES)
        self.assertEqual(result["frames"], 14)
        self.assertEqual(result["mode"], "layered")

    def test_finalize_preserves_existing_tags(self) -> None:
        """旧 manifest に tags があれば finalize で保持される (= D-8)。"""
        self._create_matrix_set_with_stages_123()
        rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.mgr.update_metadata("air_test", "default", trim_rect=rect)
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_TRIMMED)
        # 事前に manifest を書いておく (= tags 含む)。
        manifest_path = self.mgr.finalized_manifest_path(
            "air_test", "default",
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({
                "mode": "matrix",
                "checksum": "sha256:old",
                "tags": ["yukata", "long-hair"],
            }),
            encoding="utf-8",
        )
        self.fin.finalize_avatar_set(self.mgr, "air_test", "default")
        new_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(new_manifest["tags"], ["yukata", "long-hair"])
        self.assertNotEqual(new_manifest["checksum"], "sha256:old")

    def test_finalize_requires_trimmed(self) -> None:
        self._create_matrix_set_with_stages_123()
        with self.assertRaises(FileNotFoundError):
            self.fin.finalize_avatar_set(self.mgr, "air_test", "default")

    def test_transfer_requires_finalized(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        with self.assertRaises(FileNotFoundError):
            self.fin.transfer_avatar_set(self.mgr, "air_test", "default")

    def test_transfer_mocked_mcp_call(self) -> None:
        """⑥ transfer は MCP 呼び出しを mock して動作だけ確認。"""
        self._create_matrix_set_with_stages_123()
        rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.mgr.update_metadata("air_test", "default", trim_rect=rect)
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_TRIMMED)
        finalize_result = self.fin.finalize_avatar_set(
            self.mgr, "air_test", "default",
        )

        # avatar_loader 側の MCP 呼び出しを mock。
        import asyncio

        async def fake_call(archive_path, mode):
            return f"ok: archive={archive_path} mode={mode}"

        def fake_run(coro, timeout_sec):
            # coro を回して結果を返す。 loop を close してリソースリークを防ぐ。
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        # 既にロード済みの avatar_loader に patch を当てる。
        import avatar_loader as al

        with patch.object(al, "_call_load_avatar_set", fake_call), \
                patch.object(al, "_run_async_on_mcp_loop", fake_run):
            result = self.fin.transfer_avatar_set(
                self.mgr, "air_test", "default",
            )
        self.assertEqual(result["stage_id"], "06_transfer")
        self.assertEqual(result["mode"], "matrix")
        self.assertEqual(result["checksum"], finalize_result["checksum"])
        # avatar_loader の cache にも反映されてる。
        loader = al.get_avatar_loader()
        self.assertFalse(
            loader.is_load_required("air_test", finalize_result["checksum"])
        )

    def test_full_chain_matrix(self) -> None:
        """① → ② → ③ → ④ → ⑤ end-to-end (⑥ は別途 mock 必要なので除く)。"""
        self._create_matrix_set_with_stages_123()
        rect = {"x": 0, "y": 0, "width": 256, "height": 256}
        self.mgr.update_metadata("air_test", "default", trim_rect=rect)
        # ④
        trim_result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_TRIMMED,
        )
        self.assertEqual(len(trim_result["files"]), 90)
        # ⑤
        finalize_result = self.fin.finalize_avatar_set(
            self.mgr, "air_test", "default",
        )
        self.assertEqual(finalize_result["frames"], 90)
        # 確定品が SetInfo に反映される。
        info = self.mgr.get_set("air_test", "default")
        self.assertTrue(info.has_finalized)
        self.assertEqual(info.finalized_mode, "matrix")
        self.assertEqual(info.finalized_checksum, finalize_result["checksum"])


class ZipImportTests(unittest.TestCase):
    """Phase 4.5-d-5: zip 直接投入。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        self.ap = _load_avatar_pipeline()
        self.fin = _load_module(
            "avatar_finalizer", _ADDON_DIR / "avatar_finalizer.py",
        )

        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        self.ap._singleton = None
        self.mgr = self.ap.get_avatar_pipeline_manager()
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.create_set("air_test", "layered_set", mode="layered")

    def _make_zip(self, files: dict[str, bytes]) -> bytes:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        return buf.getvalue()

    def _make_dummy_png(self, color: tuple) -> bytes:
        from PIL import Image
        import io
        im = Image.new("RGB", (16, 16), color)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def test_expected_filenames_matrix(self) -> None:
        expected = self.fin.expected_trimmed_filenames("matrix")
        self.assertEqual(len(expected), 90)
        self.assertIn("idle_open_closed.png", expected)
        self.assertIn("embarrassed_closed_u.png", expected)

    def test_expected_filenames_layered(self) -> None:
        expected = self.fin.expected_trimmed_filenames("layered")
        self.assertEqual(len(expected), 14)
        self.assertIn("face_idle.png", expected)
        self.assertIn("eyes_open.png", expected)
        self.assertIn("mouth_e.png", expected)

    def test_expected_filenames_invalid_mode(self) -> None:
        with self.assertRaises(ValueError):
            self.fin.expected_trimmed_filenames("invalid")

    def test_import_zip_layered_complete(self) -> None:
        """layered mode で 14 ファイル全部入った zip を投入 → 全展開 + 完了マーク。"""
        files = {}
        for fn in self.fin.expected_trimmed_filenames("layered"):
            files[fn] = self._make_dummy_png((100, 100, 200))
        zip_bytes = self._make_zip(files)
        result = self.fin.import_trimmed_zip(
            self.mgr, "air_test", "layered_set", zip_bytes,
        )
        self.assertEqual(result["extracted"], 14)
        self.assertEqual(result["expected"], 14)
        self.assertEqual(result["missing"], [])
        # ④ 完了マークが立っている。
        meta = self.mgr.read_metadata("air_test", "layered_set")
        self.assertIn(self.ap.STAGE_TRIMMED, meta.completed_stages)

    def test_import_zip_matrix_complete(self) -> None:
        files = {}
        for fn in self.fin.expected_trimmed_filenames("matrix"):
            files[fn] = self._make_dummy_png((50, 50, 50))
        zip_bytes = self._make_zip(files)
        result = self.fin.import_trimmed_zip(
            self.mgr, "air_test", "default", zip_bytes,
        )
        self.assertEqual(result["extracted"], 90)
        self.assertEqual(result["missing"], [])

    def test_import_zip_missing_files_rolls_back(self) -> None:
        """require_complete=True で不足あり → ロールバック + ValueError。"""
        # 14 個中 13 個だけ。
        all_files = list(self.fin.expected_trimmed_filenames("layered"))
        files = {
            fn: self._make_dummy_png((0, 0, 0))
            for fn in all_files[:13]
        }
        zip_bytes = self._make_zip(files)
        with self.assertRaises(ValueError) as cm:
            self.fin.import_trimmed_zip(
                self.mgr, "air_test", "layered_set", zip_bytes,
                require_complete=True,
            )
        self.assertIn("Missing", str(cm.exception))
        # ロールバックされて trimmed/ が空。
        trimmed_dir = self.mgr.stage_dir(
            "air_test", "layered_set", self.ap.STAGE_TRIMMED,
        )
        self.assertEqual(list(trimmed_dir.glob("*.png")), [])

    def test_import_zip_missing_files_partial_allowed(self) -> None:
        """require_complete=False なら部分投入 OK (= 完了マークは立たない)。"""
        all_files = list(self.fin.expected_trimmed_filenames("layered"))
        files = {
            fn: self._make_dummy_png((0, 0, 0))
            for fn in all_files[:13]
        }
        zip_bytes = self._make_zip(files)
        result = self.fin.import_trimmed_zip(
            self.mgr, "air_test", "layered_set", zip_bytes,
            require_complete=False,
        )
        self.assertEqual(result["extracted"], 13)
        self.assertEqual(len(result["missing"]), 1)
        # 完了マーク立たず。
        meta = self.mgr.read_metadata("air_test", "layered_set")
        self.assertNotIn(self.ap.STAGE_TRIMMED, meta.completed_stages)

    def test_import_zip_skips_unexpected(self) -> None:
        """expected セット外のファイルは skip される。"""
        files = {
            "face_idle.png": self._make_dummy_png((0, 0, 0)),
            "random_garbage.png": self._make_dummy_png((0, 0, 0)),
            "notes.txt": b"this is text",
        }
        zip_bytes = self._make_zip(files)
        result = self.fin.import_trimmed_zip(
            self.mgr, "air_test", "layered_set", zip_bytes,
            require_complete=False,
        )
        self.assertEqual(result["extracted"], 1)
        skipped_names = [s["name"] for s in result["skipped"]]
        self.assertIn("random_garbage.png", skipped_names)
        self.assertIn("notes.txt", skipped_names)

    def test_import_zip_path_traversal_safe(self) -> None:
        """zip 内に `../etc/passwd` 等の path traversal があっても basename
        だけ取って expected セット外なら skip される (= traversal 防止)。"""
        files = {
            "../../etc/passwd": b"malicious",
            "subdir/face_idle.png": self._make_dummy_png((0, 0, 0)),
        }
        zip_bytes = self._make_zip(files)
        self.fin.import_trimmed_zip(
            self.mgr, "air_test", "layered_set", zip_bytes,
            require_complete=False,
        )
        # 期待されない (basename=passwd) は skip、 subdir/face_idle.png は
        # basename=face_idle.png として展開される。
        trimmed_dir = self.mgr.stage_dir(
            "air_test", "layered_set", self.ap.STAGE_TRIMMED,
        )
        self.assertTrue((trimmed_dir / "face_idle.png").exists())
        # passwd は展開されてない (= ../ で外に出ない)。
        self.assertFalse(
            (self.storage_root / ".." / ".." / "etc" / "passwd").exists()
        )

    def test_import_zip_invalid_zip(self) -> None:
        with self.assertRaises(ValueError):
            self.fin.import_trimmed_zip(
                self.mgr, "air_test", "default", b"not a zip file",
                require_complete=False,
            )

    def test_import_zip_clears_old_trimmed(self) -> None:
        """既存 04_trimmed があったら消してから書く。"""
        trimmed_dir = self.mgr.stage_dir(
            "air_test", "layered_set", self.ap.STAGE_TRIMMED,
        )
        trimmed_dir.mkdir(parents=True, exist_ok=True)
        (trimmed_dir / "junk.png").write_bytes(b"old data")
        files = {
            fn: self._make_dummy_png((0, 0, 0))
            for fn in self.fin.expected_trimmed_filenames("layered")
        }
        self.fin.import_trimmed_zip(
            self.mgr, "air_test", "layered_set",
            self._make_zip(files),
        )
        self.assertFalse((trimmed_dir / "junk.png").exists())


class AvatarLoaderActiveSetTests(unittest.TestCase):
    """Phase 4.5-d-5: avatar_loader のアクティブセット引き改修確認。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        self.ap = _load_avatar_pipeline()
        self.loader = _load_module(
            "avatar_loader", _ADDON_DIR / "avatar_loader.py",
        )

        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        # avatar_loader 内の get_addon_storage_path も patch (= 同名 import で別 module attribute)。
        self._patch_loader_storage = patch.object(
            self.loader, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_loader_storage.start()
        self.addCleanup(self._patch_loader_storage.stop)

        self.ap._singleton = None

    def test_no_active_returns_default(self) -> None:
        """アクティブ未設定 → DEFAULT_SET_NAME ("default")。"""
        name = self.loader._get_active_set_name("air_test")
        self.assertEqual(name, self.loader.DEFAULT_SET_NAME)

    def test_active_set_returns_active(self) -> None:
        """セット作成 + アクティブ化 → そのセット名が返る。"""
        mgr = self.ap.get_avatar_pipeline_manager()
        mgr.create_set("air_test", "yukata", mode="matrix")
        mgr.set_active("air_test", "yukata")
        name = self.loader._get_active_set_name("air_test")
        self.assertEqual(name, "yukata")

    def test_pipeline_failure_falls_back_to_default(self) -> None:
        """avatar_pipeline import 失敗時 (= 模擬) でも default で動く。"""
        with patch.object(
            self.ap, "get_avatar_pipeline_manager",
            side_effect=RuntimeError("simulated"),
        ):
            name = self.loader._get_active_set_name("air_test")
        self.assertEqual(name, self.loader.DEFAULT_SET_NAME)


class QualityAspectRatioTests(unittest.TestCase):
    """Phase 4.5-d 追補: quality / aspect_ratio 設定機構。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        self.ap = _load_avatar_pipeline()
        self.gen = _load_module(
            "avatar_generator", _ADDON_DIR / "avatar_generator.py",
        )

        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        # 各呼び出しの quality / aspect_ratio を記録する fake。
        self._gen_kwargs: list[dict] = []

        def fake_generate_one(prompt, model, input_image_paths=None,
                              aspect_ratio="1:1", quality="high"):
            self._gen_kwargs.append({
                "aspect_ratio": aspect_ratio,
                "quality": quality,
            })
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        self._patch_gen = patch.object(
            self.gen, "_generate_one", fake_generate_one,
        )
        self._patch_gen.start()
        self.addCleanup(self._patch_gen.stop)

        self.ap._singleton = None
        self.mgr = self.ap.get_avatar_pipeline_manager()
        self.gen.register_avatar_executors(self.mgr)

    def test_metadata_has_new_fields_with_defaults(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        meta = self.mgr.read_metadata("air_test", "default")
        # Default 確定 2026-05-17 (= まはー検証で gpt_image_2 / low が安価かつ
        # 目パチ口パク用途で十分品質と判明)。
        self.assertEqual(meta.image_quality, "low")
        self.assertEqual(meta.image_model, "gpt_image_2")
        self.assertEqual(meta.aspect_ratio, "1:1")
        self.assertEqual(meta.stage_quality_overrides, {})
        self.assertEqual(meta.stage_aspect_overrides, {})

    def test_metadata_update_quality(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.update_metadata(
            "air_test", "default",
            image_quality="medium",
            aspect_ratio="4:3",
        )
        meta = self.mgr.read_metadata("air_test", "default")
        self.assertEqual(meta.image_quality, "medium")
        self.assertEqual(meta.aspect_ratio, "4:3")

    def test_quality_passed_to_generate(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.update_metadata(
            "air_test", "default",
            image_quality="medium", aspect_ratio="4:3",
        )
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.assertEqual(len(self._gen_kwargs), 1)
        self.assertEqual(self._gen_kwargs[0]["quality"], "medium")
        self.assertEqual(self._gen_kwargs[0]["aspect_ratio"], "4:3")

    def test_stage_override_takes_priority(self) -> None:
        self.mgr.create_set("air_test", "default", mode="matrix")
        # ② だけ low、 セット全体は high
        self.mgr.update_metadata(
            "air_test", "default",
            image_quality="high",
            aspect_ratio="4:3",
            stage_quality_overrides={"02_expressions": "low"},
        )
        # ①生成 → high
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        # ②生成 → low (= override)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        # ①は 1 呼び出し (high)、 ②は 5 呼び出し (low)
        self.assertEqual(self._gen_kwargs[0]["quality"], "high")
        for kw in self._gen_kwargs[1:]:
            self.assertEqual(kw["quality"], "low")

    def test_resolve_quality_helper(self) -> None:
        meta = self.ap.AvatarSetMetadata(
            image_quality="high",
            stage_quality_overrides={"03_matrix": "medium"},
        )
        self.assertEqual(
            self.gen._resolve_quality(meta, self.ap.STAGE_FACE), "high",
        )
        self.assertEqual(
            self.gen._resolve_quality(meta, self.ap.STAGE_MATRIX), "medium",
        )

    def test_resolve_aspect_helper(self) -> None:
        meta = self.ap.AvatarSetMetadata(
            aspect_ratio="4:3",
            stage_aspect_overrides={"03_matrix": "1:1"},
        )
        self.assertEqual(
            self.gen._resolve_aspect_ratio(meta, self.ap.STAGE_FACE),
            "4:3",
        )
        self.assertEqual(
            self.gen._resolve_aspect_ratio(meta, self.ap.STAGE_MATRIX),
            "1:1",
        )

    def test_only_target_generates_single_expression(self) -> None:
        """② 単発テスト経路: params.only_target で 1 枚だけ生成。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self._gen_kwargs.clear()
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
            params={"only_target": "happy"},
        )
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["target"], "happy")
        self.assertEqual(len(self._gen_kwargs), 1)

    def test_matrix_only_target_generates_one(self) -> None:
        """③ matrix の only_target で 1 枚だけ生成。"""
        self.mgr.create_set("air_test", "matrix_set", mode="matrix")
        self.mgr.execute_stage(
            "air_test", "matrix_set", self.ap.STAGE_FACE,
        )
        self.mgr.execute_stage(
            "air_test", "matrix_set", self.ap.STAGE_EXPRESSIONS,
        )
        self._gen_kwargs.clear()
        result = self.mgr.execute_stage(
            "air_test", "matrix_set", self.ap.STAGE_MATRIX,
            params={"only_target": "happy_half_open"},
        )
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["target"], "happy_half_open")
        self.assertEqual(len(self._gen_kwargs), 1)

    def test_matrix_only_target_base_uses_copy(self) -> None:
        """matrix の base (open/closed) は copy で生成なし。"""
        self.mgr.create_set("air_test", "matrix_set", mode="matrix")
        self.mgr.execute_stage(
            "air_test", "matrix_set", self.ap.STAGE_FACE,
        )
        self.mgr.execute_stage(
            "air_test", "matrix_set", self.ap.STAGE_EXPRESSIONS,
        )
        self._gen_kwargs.clear()
        result = self.mgr.execute_stage(
            "air_test", "matrix_set", self.ap.STAGE_MATRIX,
            params={"only_target": "happy_open_closed"},
        )
        self.assertEqual(len(result["files"]), 1)
        self.assertTrue(result["files"][0].get("copied_from_base"))
        self.assertEqual(len(self._gen_kwargs), 0)

    def test_layered_only_target(self) -> None:
        """③ layered の only_target で 1 枚だけ生成。"""
        self.mgr.create_set(
            "air_test", "layered_set", mode="layered",
        )
        self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_FACE,
        )
        self._gen_kwargs.clear()
        result = self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_LAYERED,
            params={"only_target": "mouth_half"},
        )
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["target"], "mouth_half")
        self.assertEqual(len(self._gen_kwargs), 1)

    def test_stage3_constraint_always_prepended(self) -> None:
        """③ matrix 生成時に constraint プロンプトが必ず prepend される。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        self._gen_kwargs.clear()
        # 単発 1 件で確認 (= 84 枚 mock しなくて済む)。
        # _gen_kwargs は aspect/quality だけ記録、 prompt も記録するよう
        # 別 mock 立てる。
        prompts: list[str] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            prompts.append(prompt)
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(self.gen, "_generate_one", fake):
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "happy_half_open"},
            )
        self.assertEqual(len(prompts), 1)
        self.assertIn("参照画像", prompts[0])
        self.assertIn("構図・ポーズ", prompts[0])

    def test_stage3_common_prompt_off_by_default(self) -> None:
        """③ で apply_common_prompt_to_stage3=False (default) なら common
        は prompt に含まれない。"""
        self.mgr.create_set(
            "air_test", "default", mode="matrix",
            common_prompt="silver hair, jewelry",
        )
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        prompts: list[str] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            prompts.append(prompt)
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(self.gen, "_generate_one", fake):
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "happy_half_open"},
            )
        # default OFF: common_prompt の "silver hair" は含まれない。
        self.assertNotIn("silver hair", prompts[0])

    def test_stage3_common_prompt_on_when_enabled(self) -> None:
        """apply_common_prompt_to_stage3=True にすると common が含まれる。"""
        self.mgr.create_set(
            "air_test", "default", mode="matrix",
            common_prompt="silver hair, jewelry",
        )
        self.mgr.update_metadata(
            "air_test", "default", apply_common_prompt_to_stage3=True,
        )
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        prompts: list[str] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            prompts.append(prompt)
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(self.gen, "_generate_one", fake):
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "happy_half_open"},
            )
        self.assertIn("silver hair", prompts[0])

    def test_stage3_does_not_inherit_stage2_long_template(self) -> None:
        """③ matrix で ② の長文 expression テンプレ詳細を持ち込まない
        (= 「mouth open」 と矛盾して AI が 2 枚並び画像を返す事故防止、
        まはー報告 2026-05-17)。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        prompts: list[str] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            prompts.append(prompt)
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(self.gen, "_generate_one", fake):
            # eyes=half, mouth=open で両方変更対象 → どちらも prompt 入る。
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "sad_half_open"},
            )
        self.assertEqual(len(prompts), 1)
        p = prompts[0]
        # 端的な face label は含まれる。
        self.assertIn("sad expression", p)
        # ② の長文詳細 (= 口形状記述) は含まれない。
        self.assertNotIn("lips drawn together", p)
        self.assertNotIn("downcast eyes", p)
        # 目・口指定はちゃんと入る (= eyes=half / mouth=open は変更対象)。
        self.assertIn("mouth open", p)
        self.assertIn("eyes half closed", p)

    def test_stage3_idle_uses_neutral_label(self) -> None:
        """idle face は 'neutral expression' で明示 (= ① の長文テンプレも
        持ち込まない)。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        prompts: list[str] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            prompts.append(prompt)
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(self.gen, "_generate_one", fake):
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "idle_half_e"},
            )
        self.assertIn("neutral expression", prompts[0])
        # ① の長文テンプレ "calm and relaxed" は含まれない。
        self.assertNotIn("calm and relaxed", prompts[0])

    def test_stage3_constraint_includes_kanjou(self) -> None:
        """default constraint 文に「表す感情」 が含まれる
        (= 2 枚並び事故防止、 まはー指摘 2026-05-17)。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        meta = self.mgr.read_metadata("air_test", "default")
        constraint = self.gen._get_stage3_constraint_prompt(meta)
        self.assertIn("表す感情", constraint)
        self.assertIn("構図・ポーズ", constraint)

    def test_stage3_constraint_includes_unchanged_side_clause(self) -> None:
        """default constraint 文に「指定がない側は参照画像から完全に
        そのまま」 が含まれる (= eyes=open 等で目プロンプト省略時の
        AI 解釈ブレ防止、 例: happy_open_half が目閉じになる事故。
        まはー指摘 2026-05-17)。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        meta = self.mgr.read_metadata("air_test", "default")
        constraint = self.gen._get_stage3_constraint_prompt(meta)
        self.assertIn("指定がない側", constraint)
        self.assertIn("参照画像から完全にそのまま", constraint)

    def test_eyes_open_prompt_omitted_in_matrix(self) -> None:
        """eyes=open のセル → eyes プロンプト省略 (= 「触る」 指示なし、
        AI に変更余地を渡さない。 まはー検証 2026-05-17)。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        prompts: list[str] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            prompts.append(prompt)
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(self.gen, "_generate_one", fake):
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "happy_open_half"},
            )
        self.assertEqual(len(prompts), 1)
        # eyes=open なので eyes プロンプト ("eyes fully open") は入らない。
        self.assertNotIn("eyes fully open", prompts[0])
        # mouth=half は変更対象 → プロンプト入る。
        self.assertIn("lips slightly parted", prompts[0])

    def test_mouth_closed_prompt_omitted_in_matrix(self) -> None:
        """mouth=closed のセル → mouth プロンプト省略。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        prompts: list[str] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            prompts.append(prompt)
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(self.gen, "_generate_one", fake):
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "happy_half_closed"},
            )
        self.assertEqual(len(prompts), 1)
        # mouth=closed なので mouth プロンプト ("mouth closed") は入らない。
        self.assertNotIn("mouth closed, lips fully together", prompts[0])
        # eyes=half は変更対象 → プロンプト入る。
        self.assertIn("eyes half closed", prompts[0])

    def test_eye_modify_hint_added_when_eyes_changed(self) -> None:
        """eyes != open のセル → eye_modify_hint 追加 (= 最大開き hint)。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        prompts: list[str] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            prompts.append(prompt)
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(self.gen, "_generate_one", fake):
            # eyes=half (≠ open) → hint 入る
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "happy_half_closed"},
            )
            # eyes=open → hint 入らない
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_MATRIX,
                params={"only_target": "happy_open_half"},
            )
        self.assertIn("最大開き状態", prompts[0])
        self.assertNotIn("最大開き状態", prompts[1])

    def test_layered_eyes_open_is_copied(self) -> None:
        """layered の eyes=open は copy で対応 (= 生成しない)。"""
        self.mgr.create_set(
            "air_test", "layered_set", mode="layered",
        )
        self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_FACE,
        )
        self._gen_kwargs.clear()
        result = self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_LAYERED,
        )
        # 8 個中 eyes=open + mouth=closed の 2 個は copy、 6 個生成。
        self.assertEqual(len(self._gen_kwargs), 6)
        # eyes=open / mouth=closed の result は copied_from_base 付き。
        by_target = {f["target"]: f for f in result["files"]}
        self.assertTrue(by_target["eyes_open"].get("copied_from_base"))
        self.assertTrue(by_target["mouth_closed"].get("copied_from_base"))
        # eyes=half / mouth=open 等は通常生成 (= copied_from_base なし)。
        self.assertFalse(
            by_target["eyes_half"].get("copied_from_base", False),
        )

    def test_layered_regenerate_eyes_open_uses_copy(self) -> None:
        """layered の eyes_open 単発再生成も copy で対応。"""
        self.mgr.create_set(
            "air_test", "layered_set", mode="layered",
        )
        self.mgr.execute_stage(
            "air_test", "layered_set", self.ap.STAGE_FACE,
        )
        self._gen_kwargs.clear()
        result = self.mgr.regenerate_target(
            "air_test", "layered_set", self.ap.STAGE_LAYERED,
            target="eyes_open",
        )
        # 生成は呼ばれない。
        self.assertEqual(len(self._gen_kwargs), 0)
        self.assertTrue(result.get("copied_from_base"))

    def test_skip_existing_in_matrix(self) -> None:
        """matrix で skip_existing=True なら既存 PNG はタスクから除外。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        # happy_half_open だけ事前生成済みに。
        existing = self.mgr.stage_dir(
            "air_test", "default", self.ap.STAGE_MATRIX,
        ) / "happy_half_open.png"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff" * 16)
        self._gen_kwargs.clear()
        # face_filter で happy だけ + skip_existing。
        # happy face = 15 通り、 open_closed (base、 生成対象外) を除いて
        # 14 件生成タスク。 そのうち half_open は既存 skip → 13 件生成。
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_MATRIX,
            params={"face_filter": "happy", "skip_existing": True},
        )
        self.assertEqual(len(self._gen_kwargs), 13)
        # base copy はスキップしない (= existing じゃない場合)
        # results には base copy + 12 件の生成結果。
        targets = {f["target"] for f in result["files"]}
        self.assertIn("happy_open_closed", targets)  # base copy
        self.assertNotIn("happy_half_open", targets)  # skip 済み

    def test_face_filter_in_matrix(self) -> None:
        """face_filter=happy なら happy 関連のみ生成 (= 14 枚 = 1 face)。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
        )
        self._gen_kwargs.clear()
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_MATRIX,
            params={"face_filter": "happy"},
        )
        # 13 件生成 (= 15 通り - base 1 - 自身 1) + base copy 1 (= happy だけ)
        # wait: 15 通り - base 1 = 14 件生成、 + base copy 1 = 15 件 in results
        self.assertEqual(len(self._gen_kwargs), 14)
        # face_filter で他の face は触らない。
        for f in result["files"]:
            self.assertTrue(f["target"].startswith("happy_"))

    def test_only_target_invalid_falls_back_to_all(self) -> None:
        """only_target が unknown なら全 5 表情を生成 (= 安全側)。"""
        self.mgr.create_set("air_test", "default", mode="matrix")
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        self._gen_kwargs.clear()
        result = self.mgr.execute_stage(
            "air_test", "default", self.ap.STAGE_EXPRESSIONS,
            params={"only_target": "nonexistent"},
        )
        self.assertEqual(len(result["files"]), 5)


class UploadFaceImageTests(unittest.TestCase):
    """Phase 4.5-d 追補: ① 手動アップロード経路。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        self.ap = _load_avatar_pipeline()
        self.fin = _load_module(
            "avatar_finalizer", _ADDON_DIR / "avatar_finalizer.py",
        )

        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        self.ap._singleton = None
        self.mgr = self.ap.get_avatar_pipeline_manager()
        self.mgr.create_set(
            "air_test", "default", mode="matrix",
            common_prompt="silver hair",
        )

    def _make_png(self, w: int, h: int, color=(100, 100, 200)) -> bytes:
        from PIL import Image
        import io
        im = Image.new("RGB", (w, h), color)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def test_analyze_image_returns_size_and_suggestion(self) -> None:
        png = self._make_png(800, 600)  # 4:3
        result = self.fin.analyze_image(png)
        self.assertEqual(result["width"], 800)
        self.assertEqual(result["height"], 600)
        self.assertEqual(result["suggested_aspect"], "4:3")
        self.assertIn("1:1", result["supported_aspects"])

    def test_closest_supported_aspect_square(self) -> None:
        self.assertEqual(
            self.fin.closest_supported_aspect(512, 512), "1:1",
        )

    def test_closest_supported_aspect_43(self) -> None:
        self.assertEqual(
            self.fin.closest_supported_aspect(1024, 768), "4:3",
        )

    def test_upload_center_crops_for_aspect(self) -> None:
        """中央クロップで target_aspect に揃える。"""
        # 800×600 (= 4:3) を 1:1 にすると 600×600 中央クロップ。
        png = self._make_png(800, 600)
        result = self.fin.upload_face_image(
            self.mgr, "air_test", "default", png, target_aspect="1:1",
        )
        self.assertEqual(result["original_size"], [800, 600])
        crop = result["crop"]
        self.assertEqual(crop["width"], 600)
        self.assertEqual(crop["height"], 600)
        self.assertEqual(crop["x"], 100)  # (800-600)/2
        self.assertEqual(crop["y"], 0)
        self.assertEqual(result["target_aspect"], "1:1")
        # face.png が書かれてる + サイズ通り。
        from PIL import Image
        face_path = (
            self.mgr.stage_dir("air_test", "default", self.ap.STAGE_FACE)
            / "face.png"
        )
        self.assertTrue(face_path.exists())
        with Image.open(face_path) as im:
            self.assertEqual(im.size, (600, 600))
        # metadata.aspect_ratio が更新されてる。
        meta = self.mgr.read_metadata("air_test", "default")
        self.assertEqual(meta.aspect_ratio, "1:1")

    def test_upload_with_custom_crop_rect(self) -> None:
        png = self._make_png(1000, 1000)
        result = self.fin.upload_face_image(
            self.mgr, "air_test", "default", png,
            target_aspect="1:1",
            crop_rect={"x": 100, "y": 100, "width": 500, "height": 500},
        )
        self.assertEqual(result["crop"]["x"], 100)
        self.assertEqual(result["crop"]["width"], 500)

    def test_upload_invalid_aspect_raises(self) -> None:
        png = self._make_png(800, 600)
        with self.assertRaises(ValueError):
            self.fin.upload_face_image(
                self.mgr, "air_test", "default", png,
                target_aspect="13:9",  # not in SUPPORTED_ASPECTS
            )

    def test_upload_crop_out_of_bounds(self) -> None:
        png = self._make_png(100, 100)
        with self.assertRaises(ValueError):
            self.fin.upload_face_image(
                self.mgr, "air_test", "default", png,
                target_aspect="1:1",
                crop_rect={"x": 50, "y": 50, "width": 100, "height": 100},
            )

    def test_upload_negative_crop(self) -> None:
        png = self._make_png(100, 100)
        with self.assertRaises(ValueError):
            self.fin.upload_face_image(
                self.mgr, "air_test", "default", png,
                target_aspect="1:1",
                crop_rect={"x": -10, "y": 0, "width": 50, "height": 50},
            )

    def test_upload_missing_wip(self) -> None:
        png = self._make_png(100, 100)
        with self.assertRaises(FileNotFoundError):
            self.fin.upload_face_image(
                self.mgr, "air_test", "nonexistent", png,
                target_aspect="1:1",
            )

    def test_aspect_propagates_to_subsequent_generations(self) -> None:
        """upload で aspect_ratio 設定 → ②③ も同アス比で生成される。"""
        # この検証は QualityAspectRatioTests でも実質確認済みだが、
        # upload 経路 → metadata 更新 → 生成 の流れを通す。
        gen = _load_module(
            "avatar_generator", _ADDON_DIR / "avatar_generator.py",
        )
        # 4:3 でアップロード
        png = self._make_png(800, 600)
        self.fin.upload_face_image(
            self.mgr, "air_test", "default", png, target_aspect="4:3",
        )
        # ②生成 (= mock backend)
        gen_calls: list[dict] = []

        def fake(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            gen_calls.append({"aspect_ratio": aspect_ratio})
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        with patch.object(gen, "_generate_one", fake):
            gen.register_avatar_executors(self.mgr)
            self.mgr.execute_stage(
                "air_test", "default", self.ap.STAGE_EXPRESSIONS,
            )
        # 5 表情全部が 4:3 で生成されてる。
        for call in gen_calls:
            self.assertEqual(call["aspect_ratio"], "4:3")


class CacheBusterTouchTests(unittest.TestCase):
    """`metadata.updated_at` の touch 動作確認 (= frontend cache buster invalidate)。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        self.ap = _load_avatar_pipeline()
        self.gen = _load_module(
            "avatar_generator", _ADDON_DIR / "avatar_generator.py",
        )

        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        def fake_generate_one(prompt, model, input_image_paths=None,
                              aspect_ratio="1:1", quality="high"):
            return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

        self._patch_gen = patch.object(
            self.gen, "_generate_one", fake_generate_one,
        )
        self._patch_gen.start()
        self.addCleanup(self._patch_gen.stop)

        self.ap._singleton = None
        self.mgr = self.ap.get_avatar_pipeline_manager()
        self.gen.register_avatar_executors(self.mgr)
        self.mgr.create_set("air_test", "default", mode="matrix")

    def test_touch_updates_updated_at(self) -> None:
        import time
        before = self.mgr.read_metadata("air_test", "default").updated_at
        time.sleep(0.001)  # ISO 文字列の差を確実につくる
        self.mgr.touch_updated_at("air_test", "default")
        after = self.mgr.read_metadata("air_test", "default").updated_at
        self.assertGreater(after, before)

    def test_touch_noop_when_no_wip(self) -> None:
        # 例外を投げずに静かに no-op (= ⑥ transfer 後等)。
        self.mgr.touch_updated_at("air_test", "nonexistent_set")

    def test_execute_stage_touches_updated_at(self) -> None:
        import time
        before = self.mgr.read_metadata("air_test", "default").updated_at
        time.sleep(0.001)
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        after = self.mgr.read_metadata("air_test", "default").updated_at
        self.assertGreater(after, before)

    def test_regenerate_touches_updated_at(self) -> None:
        import time
        self.mgr.execute_stage("air_test", "default", self.ap.STAGE_FACE)
        before = self.mgr.read_metadata("air_test", "default").updated_at
        time.sleep(0.001)
        self.mgr.regenerate_target(
            "air_test", "default", self.ap.STAGE_FACE, target="face",
        )
        after = self.mgr.read_metadata("air_test", "default").updated_at
        self.assertGreater(after, before)

    def test_execute_stage_touches_even_on_failure(self) -> None:
        """例外時 (= 失敗) でも updated_at が touch される (= try/finally)。"""
        import time

        def boom(prompt, model, input_image_paths=None,
                 aspect_ratio="1:1", quality="high"):
            raise RuntimeError("simulated backend failure")

        before = self.mgr.read_metadata("air_test", "default").updated_at
        time.sleep(0.001)
        with patch.object(self.gen, "_generate_one", boom):
            with self.assertRaises(RuntimeError):
                self.mgr.execute_stage(
                    "air_test", "default", self.ap.STAGE_FACE,
                )
        after = self.mgr.read_metadata("air_test", "default").updated_at
        # failure でも touch される (= finally で実行)。
        self.assertGreater(after, before)


class PerSetExecutionLockTests(unittest.TestCase):
    """同 persona+set の execute_stage 並列実行を防ぐ lock の動作確認
    (= chain 中の 502 後 frontend が次 face_filter を投げてきても、 backend で
    順次処理 = OpenAI cost 倍増 + base copy 競合事故の防止、 まはー検証
    2026-05-17)。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.storage_root = Path(self._tmpdir.name)

        self.ap = _load_avatar_pipeline()
        self._patch_storage = patch.object(
            self.ap, "get_addon_storage_path",
            lambda addon_name: self.storage_root,
        )
        self._patch_storage.start()
        self.addCleanup(self._patch_storage.stop)

        self.ap._singleton = None
        self.mgr = self.ap.get_avatar_pipeline_manager()
        self.mgr.create_set("p", "s", mode="matrix")

    def test_same_set_executions_serialize(self) -> None:
        import threading
        import time

        start_times: list[float] = []
        end_times: list[float] = []

        def slow_executor(mgr, persona_id, set_name, stage_id, params):
            start_times.append(time.time())
            time.sleep(0.1)
            end_times.append(time.time())
            return {"stage_id": stage_id, "files": [], "errors": []}

        self.mgr.register_stage_executor(slow_executor)

        threads = [
            threading.Thread(
                target=lambda: self.mgr.execute_stage(
                    "p", "s", self.ap.STAGE_FACE,
                ),
            )
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 全て完了 = 3 回 executor 呼ばれた。
        self.assertEqual(len(start_times), 3)
        # 順次実行: 各 executor の開始時刻が前の終了時刻以後。
        sorted_pairs = sorted(zip(start_times, end_times))
        for i in range(1, len(sorted_pairs)):
            self.assertGreaterEqual(
                sorted_pairs[i][0], sorted_pairs[i - 1][1] - 0.01,
                "executions should not overlap (= lock acquired)",
            )

    def test_different_sets_run_concurrently(self) -> None:
        """別 persona+set は並列実行可能 (= lock scope は per persona+set)。"""
        import threading
        import time

        self.mgr.create_set("q", "s", mode="matrix")
        overlapping = [False]
        active_count = [0]
        lock_check = threading.Lock()

        def slow_executor(mgr, persona_id, set_name, stage_id, params):
            with lock_check:
                active_count[0] += 1
                if active_count[0] > 1:
                    overlapping[0] = True
            time.sleep(0.1)
            with lock_check:
                active_count[0] -= 1
            return {"stage_id": stage_id, "files": [], "errors": []}

        self.mgr.register_stage_executor(slow_executor)

        t1 = threading.Thread(target=lambda: self.mgr.execute_stage(
            "p", "s", self.ap.STAGE_FACE,
        ))
        t2 = threading.Thread(target=lambda: self.mgr.execute_stage(
            "q", "s", self.ap.STAGE_FACE,
        ))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertTrue(
            overlapping[0],
            "different sets should run concurrently",
        )


if __name__ == "__main__":
    unittest.main()
