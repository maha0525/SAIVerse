import unittest
from unittest.mock import patch, MagicMock

from tools.defs.image_generator import generate_image
from tools import TOOL_REGISTRY, OPENAI_TOOLS_SPEC, GEMINI_TOOLS_SPEC

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

        result = generate_image('a cat')
        self.assertTrue(result.startswith('data:image/png;base64,'))
        mock_genai.Client.assert_called_once()
        mock_client.models.generate_content.assert_called_once()

    def test_tool_registration(self):
        self.assertIn('generate_image', TOOL_REGISTRY)
        oa_names = [t['function']['name'] for t in OPENAI_TOOLS_SPEC]
        gm_names = [t.function_declarations[0].name for t in GEMINI_TOOLS_SPEC]
        self.assertIn('generate_image', oa_names)
        self.assertIn('generate_image', gm_names)

if __name__ == '__main__':
    unittest.main()
