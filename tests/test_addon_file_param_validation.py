"""Tests for persona_id / name validation on the per-persona file param endpoints.

persona_id は 2026-08-16 の文字種契約より前に作られた日本語 ID が在籍し続けるため、
``_validate_persona_id`` はフォルダ名としての安全性だけを問う。addon_name / param_key
は ``_validate_names`` で従来通り ASCII 英数字 + ``_`` ``-`` に限る。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ValidatePersonaIdTests(unittest.TestCase):
    def setUp(self):
        from fastapi import HTTPException  # noqa: PLC0415

        from api.routes.addon import _validate_persona_id  # noqa: PLC0415

        self._validate = _validate_persona_id
        self._http_exc = HTTPException

    def test_japanese_ids_pass(self):
        for value in ["そら_city_a", "空_city_a", "エア", ".hidden"]:
            with self.subTest(value=value):
                self._validate(value)

    def test_ascii_id_passes(self):
        self._validate("air_city_a")

    def test_unsafe_ids_rejected(self):
        for value in [
            "a/b",
            "a\\b",
            "..",
            "a\x00b",
            "a\nb",
            "alice.",
            "alice ",
            ".",
            "CON",
            "con.txt",
            "NUL",
            "",
        ]:
            with self.subTest(value=value):
                with self.assertRaises(self._http_exc) as ctx:
                    self._validate(value)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("persona_id", ctx.exception.detail)


class ValidateNamesTests(unittest.TestCase):
    def test_non_ascii_addon_name_rejected(self):
        from fastapi import HTTPException  # noqa: PLC0415

        from api.routes.addon import _validate_names  # noqa: PLC0415

        with self.assertRaises(HTTPException) as ctx:
            _validate_names("音声アドオン", "ref_audio")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_ascii_names_pass(self):
        from api.routes.addon import _validate_names  # noqa: PLC0415

        _validate_names("saiverse-voice-tts", "ref_audio")


if __name__ == "__main__":
    unittest.main()
