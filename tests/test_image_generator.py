import unittest
from unittest.mock import patch, MagicMock

from tools.defs.image_generator import generate_image
from tools.defs import ToolResult
from tools import TOOL_REGISTRY, OPENAI_TOOLS_SPEC, GEMINI_TOOLS_SPEC
from pathlib import Path
import os

class TestImageGenerator(unittest.TestCase):
    @patch('tools.defs.image_generator.genai')
    def test_generate_image(self, mock_genai):
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

        with patch.dict(os.environ, {"GEMINI_FREE_API_KEY": "FREE", "GEMINI_API_KEY": ""}):
            result = generate_image('a cat')
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.content.startswith('data:image/png;base64,'))
        self.assertTrue(result.history_snippet)
        path = result.history_snippet.split('(')[-1].rstrip(')')
        self.assertTrue(Path(path).exists())
        mock_genai.Client.assert_called_once_with(api_key='FREE')
        mock_client.models.generate_content.assert_called_once()

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
