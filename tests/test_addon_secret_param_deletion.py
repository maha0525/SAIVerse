"""Tests for deleting secret addon params (API key / token).

API キーやトークンはサーバーが伏せ字 (``********``) にして返すので、画面に
見えている文字は本物の値ではない。 そのため空欄や伏せ字が送られてきた時は
「触っていない」とみなして既存値を温存する — 画面を開いただけの人が伏せ字を
保存し直して鍵を壊す事故を防ぐための規約。

その裏返しとして「消す」意思表示の口が別に要る。 それが値 ``None`` で、UI の
削除ボタンがこの経路を使う。 この区別が壊れると、伏せ字の保存で鍵が壊れるか、
鍵を消す手段が一切無くなるかのどちらかになる。

2026-08-25、Elyth アドオンの実機検証で「鍵を消せない」ことが判明して追加した
(UI に削除ボタンが無く、グローバル側は API にも削除経路が無かった)。
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _SecretDeletionTestBase(unittest.TestCase):
    """DB を MagicMock に差し替えて、ルート関数を直接呼ぶ共通土台。"""

    #: 保存済みの初期状態
    INITIAL_PARAMS: dict = {}

    def _run(self, coro):
        return asyncio.run(coro)

    def setUp(self):
        from api.routes import addon as addon_module

        self.addon_module = addon_module

        self._db_row = MagicMock()
        self._db_row.params_json = json.dumps(self.INITIAL_PARAMS, ensure_ascii=False)

        self._db = MagicMock()
        self._db.query.return_value.filter.return_value.first.return_value = self._db_row

        self._session_patch = patch.object(addon_module, "_get_session", return_value=self._db)
        self._session_patch.start()

        self._goc_patch = patch.object(
            addon_module, "_get_or_create_config", return_value=self._db_row,
        )
        self._goc_patch.start()

        # 実際の MCP 再接続は走らせない
        self._reconnect_patch = patch.object(
            addon_module, "_reconnect_addon_mcp", new_callable=AsyncMock,
        )
        self._reconnect_patch.start()

    def tearDown(self):
        self._session_patch.stop()
        self._goc_patch.stop()
        self._reconnect_patch.stop()

    def saved_params(self) -> dict:
        """DB に書き戻された params_json を dict で返す。"""
        return json.loads(self._db_row.params_json)


class GlobalSecretDeletionTests(_SecretDeletionTestBase):
    """``PUT /{addon}/config`` (全ペルソナ共通の設定)。"""

    INITIAL_PARAMS = {"api_key": "real-secret-value", "mcp_url": "https://example.test/mcp"}

    def _update(self, params: dict):
        from api.routes.addon import UpdateParamsRequest, update_addon_config

        return self._run(
            update_addon_config("addonA", UpdateParamsRequest(params=params), _manager=MagicMock())
        )

    def test_none_deletes_the_secret(self):
        """値 None は削除の意思表示。キーごと消える。"""
        self._update({"api_key": None, "mcp_url": "https://example.test/mcp"})

        saved = self.saved_params()
        self.assertNotIn("api_key", saved)
        # 巻き添えで他のキーが消えないこと
        self.assertEqual(saved["mcp_url"], "https://example.test/mcp")

    def test_empty_string_keeps_existing_secret(self):
        """空欄は「触っていない」。UI で欄を消しただけでは鍵は消えない。"""
        self._update({"api_key": "", "mcp_url": "https://example.test/mcp"})

        self.assertEqual(self.saved_params()["api_key"], "real-secret-value")

    def test_masked_placeholder_keeps_existing_secret(self):
        """伏せ字をそのまま保存し直しても鍵は壊れない。"""
        self._update({"api_key": "********", "mcp_url": "https://example.test/mcp"})

        self.assertEqual(self.saved_params()["api_key"], "real-secret-value")

    def test_unsent_secret_key_survives(self):
        """secret を含まない保存 (他項目の編集) で鍵が消えないこと。"""
        self._update({"mcp_url": "https://changed.test/mcp"})

        saved = self.saved_params()
        self.assertEqual(saved["api_key"], "real-secret-value")
        self.assertEqual(saved["mcp_url"], "https://changed.test/mcp")

    def test_new_value_overwrites(self):
        """実値を送れば普通に上書きされる (入れ直しの経路)。"""
        self._update({"api_key": "new-secret-value", "mcp_url": "https://example.test/mcp"})

        self.assertEqual(self.saved_params()["api_key"], "new-secret-value")

    def test_response_reports_secret_as_unset_after_deletion(self):
        """削除後のレスポンスで secret_is_set が false になること。

        UI はこの値だけを見て削除ボタンの出し分けを決めるので、ここが
        true のままだと消したはずの鍵にボタンが残る。
        """
        result = self._update({"api_key": None, "mcp_url": "https://example.test/mcp"})

        self.assertFalse(result["secret_is_set"].get("api_key", False))


class PersonaSecretDeletionTests(_SecretDeletionTestBase):
    """``PUT /{addon}/config/persona/{persona_id}`` (ペルソナ別の設定)。"""

    INITIAL_PARAMS = {"api_key": "real-secret-value", "auto_speak": True}

    def _update(self, params: dict):
        from api.routes.addon import UpdateParamsRequest, update_addon_persona_config

        return self._run(
            update_addon_persona_config(
                "addonA", "persona1", UpdateParamsRequest(params=params), _manager=MagicMock(),
            )
        )

    def test_none_deletes_the_secret(self):
        self._update({"api_key": None})

        saved = self.saved_params()
        self.assertNotIn("api_key", saved)
        # merge セマンティクスなので、送っていないキーは残る
        self.assertEqual(saved["auto_speak"], True)

    def test_empty_string_keeps_existing_secret(self):
        self._update({"api_key": ""})

        self.assertEqual(self.saved_params()["api_key"], "real-secret-value")

    def test_masked_placeholder_keeps_existing_secret(self):
        self._update({"api_key": "********"})

        self.assertEqual(self.saved_params()["api_key"], "real-secret-value")

    def test_new_value_overwrites(self):
        self._update({"api_key": "new-secret-value"})

        self.assertEqual(self.saved_params()["api_key"], "new-secret-value")

    def test_response_reports_secret_as_unset_after_deletion(self):
        result = self._update({"api_key": None})

        self.assertFalse(result["secret_is_set"].get("api_key", False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
