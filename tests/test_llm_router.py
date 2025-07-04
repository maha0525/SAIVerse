import unittest
from unittest.mock import patch, MagicMock

from llm_router import build_tools_block, route
from tools import OPENAI_TOOLS_SPEC, GEMINI_TOOLS_SPEC, TOOL_SCHEMAS

class TestLLMRouter(unittest.TestCase):
    def test_build_tools_block(self):
        block = build_tools_block(OPENAI_TOOLS_SPEC)
        self.assertIn('generate_image', block)
        block2 = build_tools_block(GEMINI_TOOLS_SPEC)
        self.assertIn('generate_image', block2)
        block3 = build_tools_block(TOOL_SCHEMAS)
        self.assertIn('generate_image', block3)

    @patch('llm_router.client')
    def test_route_image(self, mock_client):
        mock_resp = MagicMock()
        cand = MagicMock()
        cand.text = None
        cand.content = MagicMock()
        part = MagicMock()
        part.text = '{"call":"yes","tool":"generate_image","args":{"prompt":"cat"}}'
        cand.content.parts = [part]
        mock_resp.candidates = [cand]
        mock_client.models.generate_content.return_value = mock_resp

        decision = route('猫の画像を生成して', OPENAI_TOOLS_SPEC)
        self.assertEqual(decision['tool'], 'generate_image')
        mock_client.models.generate_content.assert_called_once()

    @patch('llm_router.client')
    def test_route_invalid_call(self, mock_client):
        mock_resp = MagicMock()
        cand = MagicMock()
        cand.text = None
        cand.content = MagicMock()
        part = MagicMock()
        part.text = '{"call":"generate_image","tool":"generate_image","args":{"prompt":"cat"}}'
        cand.content.parts = [part]
        mock_resp.candidates = [cand]
        mock_client.models.generate_content.return_value = mock_resp

        decision = route('猫の画像を生成して', OPENAI_TOOLS_SPEC)
        self.assertEqual(decision['call'], 'yes')
        self.assertEqual(decision['tool'], 'generate_image')

    @patch('llm_router.client')
    def test_route_invalid_call_unknown_tool(self, mock_client):
        mock_resp = MagicMock()
        cand = MagicMock()
        cand.text = None
        cand.content = MagicMock()
        part = MagicMock()
        part.text = '{"call":"maybe","tool":"nonexistent","args":{}}'
        cand.content.parts = [part]
        mock_resp.candidates = [cand]
        mock_client.models.generate_content.return_value = mock_resp

        decision = route('テスト', OPENAI_TOOLS_SPEC)
        self.assertEqual(decision['call'], 'no')
        self.assertEqual(decision['tool'], 'nonexistent')

if __name__ == '__main__':
    unittest.main()
