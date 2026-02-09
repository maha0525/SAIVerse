import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import load_builtin_tool
from tools.core import ToolResult

_mod = load_builtin_tool("image_generator")
generate_image = _mod.generate_image


class TestImageGenerator(unittest.TestCase):
    @patch.object(_mod, 'store_image_bytes')
    @patch.object(_mod, '_generate_with_nano_banana_pro')
    def test_generate_image(self, mock_gen, mock_store):
        # Mock generation backend to return image bytes
        mock_gen.return_value = (b'imgdata', 'image/png')

        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(b'imgdata')
            temp_path = Path(tmp.name)
        mock_store.return_value = ({"uri": "saiverse://image/test", "mime_type": "image/png"}, temp_path)

        # Mock persona context (generate_image calls get_active_persona_id/get_active_manager)
        with patch.object(_mod, 'get_active_persona_id', return_value=None, create=True), \
             patch.object(_mod, 'get_active_manager', return_value=None, create=True), \
             patch('tools.context.get_active_persona_id', return_value=None), \
             patch('tools.context.get_active_manager', return_value=None):
            text, info, path, metadata = generate_image('a cat')

        self.assertIsInstance(info, ToolResult)
        self.assertIn('a cat', text)
        self.assertTrue(info.history_snippet)
        self.assertEqual(Path(path), temp_path)
        self.assertIsInstance(metadata, dict)
        self.assertIn("media", metadata)
        mock_gen.assert_called_once()
        temp_path.unlink(missing_ok=True)

    @patch.object(_mod, '_generate_with_nano_banana_pro')
    def test_generate_image_error_returns_error_text(self, mock_gen):
        # When generation fails, should return error text without raising
        mock_gen.side_effect = RuntimeError("No candidates")

        with patch('tools.context.get_active_persona_id', return_value=None), \
             patch('tools.context.get_active_manager', return_value=None):
            text, info, path, metadata = generate_image('nothing')

        self.assertIn('失敗', text)
        self.assertIsNone(path)
        self.assertIsNone(metadata)

    def test_tool_registration(self):
        from tools import TOOL_REGISTRY
        self.assertIn('generate_image', TOOL_REGISTRY)


if __name__ == '__main__':
    unittest.main()
