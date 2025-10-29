import tempfile
import unittest
from unittest.mock import patch, MagicMock

from tools.defs.image_generator import generate_image
from tools.defs import ToolResult
from tools import TOOL_REGISTRY, OPENAI_TOOLS_SPEC, GEMINI_TOOLS_SPEC
from pathlib import Path
import os

class TestImageGenerator(unittest.TestCase):
    @patch('tools.defs.image_generator.store_image_bytes')
    @patch('tools.defs.image_generator.genai')
    def test_generate_image(self, mock_genai, mock_store):
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_resp = MagicMock()
        part = MagicMock()
        part.inline_data = MagicMock()
        part.inline_data.data = b'imgdata'
        part.inline_data.mime_type = 'image/png'
        mock_candidate = MagicMock()
        mock_candidate.content.parts = [part]
        mock_resp.candidates = [mock_candidate]
        mock_client.models.generate_content.return_value = mock_resp
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b'imgdata')
            temp_path = Path(tmp.name)
        mock_store.return_value = ({"uri": "saiverse://image/test", "mime_type": "image/png"}, temp_path)

        with patch.dict(os.environ, {"GEMINI_FREE_API_KEY": "FREE", "GEMINI_API_KEY": ""}):
            text, info, path, metadata = generate_image('a cat')
        self.assertIsInstance(info, ToolResult)
        self.assertIn('a cat', text)
        self.assertTrue(info.history_snippet)
        self.assertEqual(Path(path), temp_path)
        self.assertIsInstance(metadata, dict)
        self.assertIn("media", metadata)
        mock_genai.Client.assert_called_once_with(api_key='FREE')
        mock_client.models.generate_content.assert_called_once()
        temp_path.unlink(missing_ok=True)

    @patch('tools.defs.image_generator.genai')
    def test_generate_image_fallback(self, mock_genai):
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.candidates = []
        mock_client.models.generate_content.return_value = mock_resp

        with patch.dict(os.environ, {"GEMINI_FREE_API_KEY": "", "GEMINI_API_KEY": "PAID"}):
            generate_image('nothing')
        mock_genai.Client.assert_called_with(api_key='PAID')

    def test_tool_registration(self):
        self.assertIn('generate_image', TOOL_REGISTRY)
        oa_names = [t['function']['name'] for t in OPENAI_TOOLS_SPEC]
        gm_names = [t.function_declarations[0].name for t in GEMINI_TOOLS_SPEC]
        self.assertIn('generate_image', oa_names)
        self.assertIn('generate_image', gm_names)

if __name__ == '__main__':
    unittest.main()
