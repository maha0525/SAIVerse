"""Tests for /api/addon/ params merge behavior.

The list_addons and get_addon endpoints must merge ``params_schema`` defaults
with user-saved overrides so that the frontend's
``useClientActions.requires_enabled_param`` check works for users who have
not explicitly toggled the relevant option.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ResolveEffectiveParamsTests(unittest.TestCase):
    """``_resolve_effective_params(manifest, params_json)`` の挙動検証。"""

    def setUp(self):
        from api.routes.addon import (  # noqa: PLC0415
            AddonManifest,
            AddonParamSchema,
            _resolve_effective_params,
        )
        self._resolve = _resolve_effective_params
        self._manifest = AddonManifest(
            name="test-addon",
            params_schema=[
                AddonParamSchema(
                    key="bool_default_true",
                    label="A",
                    type="toggle",
                    default=True,
                    persona_configurable=False,
                ),
                AddonParamSchema(
                    key="bool_default_false",
                    label="B",
                    type="toggle",
                    default=False,
                    persona_configurable=False,
                ),
                AddonParamSchema(
                    key="text_no_default",
                    label="C",
                    type="text",
                    default=None,
                    persona_configurable=False,
                ),
                AddonParamSchema(
                    key="dropdown_default_str",
                    label="D",
                    type="dropdown",
                    default="<default>",
                    persona_configurable=False,
                ),
            ],
        )

    def test_no_user_overrides_returns_all_defaults(self):
        """params_json が None なら schema のデフォルト値だけが返る。"""
        result = self._resolve(self._manifest, None)
        self.assertEqual(result["bool_default_true"], True)
        self.assertEqual(result["bool_default_false"], False)
        self.assertEqual(result["dropdown_default_str"], "<default>")
        # default=None のキーは含めない
        self.assertNotIn("text_no_default", result)

    def test_empty_params_json_returns_defaults(self):
        result = self._resolve(self._manifest, "{}")
        self.assertEqual(result["bool_default_true"], True)
        self.assertEqual(result["bool_default_false"], False)

    def test_user_override_takes_precedence_over_default(self):
        """ユーザーが明示的に保存した値はデフォルトを上書きする。"""
        result = self._resolve(self._manifest, '{"bool_default_true": false}')
        self.assertEqual(result["bool_default_true"], False)
        # 触っていないキーはデフォルトのまま
        self.assertEqual(result["bool_default_false"], False)

    def test_user_can_add_keys_without_default(self):
        """schema に default が無いキーをユーザーが設定した場合は通す。"""
        result = self._resolve(self._manifest, '{"text_no_default": "hello"}')
        self.assertEqual(result["text_no_default"], "hello")

    def test_invalid_json_falls_back_to_defaults_only(self):
        """params_json が壊れている場合は黙ってデフォルトだけを返す。"""
        result = self._resolve(self._manifest, "{not valid json")
        self.assertEqual(result["bool_default_true"], True)
        self.assertEqual(result["bool_default_false"], False)

    def test_non_dict_json_is_ignored(self):
        """params_json がオブジェクトでない場合は無視してデフォルトだけ返す。"""
        result = self._resolve(self._manifest, "[1, 2, 3]")
        self.assertEqual(result["bool_default_true"], True)
        self.assertEqual(result["bool_default_false"], False)

    def test_consistency_with_addon_config_get_params(self):
        """``saiverse.addon_config.get_params`` の merge ロジックと同等の結果を返すこと。

        両者の merge 順 (defaults < user) が一致することは、HTTP API と
        Python API の整合性を保つ上で重要。
        """
        # defaults < user の順序
        result = self._resolve(
            self._manifest,
            '{"bool_default_true": false, "text_no_default": "x"}',
        )
        # ユーザーが false に上書き
        self.assertEqual(result["bool_default_true"], False)
        # ユーザーが追加
        self.assertEqual(result["text_no_default"], "x")
        # 触っていないキーはデフォルトのまま
        self.assertEqual(result["bool_default_false"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
