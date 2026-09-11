import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tool_loader import load_builtin_tool
from tools.core import ToolResult

_mod = load_builtin_tool("image_generator")
generate_image = _mod.generate_image


class TestImageGenerator(unittest.TestCase):
    @patch.object(_mod, '_is_image_model_available', return_value=True)
    @patch.object(_mod, 'store_image_bytes')
    @patch.object(_mod, '_generate_with_nano_banana_2')
    def test_generate_image(self, mock_gen, mock_store, mock_avail):
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
            text, info, path, metadata, item_id = generate_image('a cat')

        self.assertIsInstance(info, ToolResult)
        self.assertIn('a cat', text)
        self.assertTrue(info.history_snippet)
        self.assertEqual(Path(path), temp_path)
        self.assertIsInstance(metadata, dict)
        self.assertIn("media", metadata)
        # persona_id/manager が None の場合 item は作成されないので item_id は None
        self.assertIsNone(item_id)
        mock_gen.assert_called_once()
        temp_path.unlink(missing_ok=True)

    @patch.object(_mod, '_is_image_model_available', return_value=True)
    @patch.object(_mod, '_generate_with_grok_imagine', side_effect=RuntimeError("No key"))
    @patch.object(_mod, '_generate_with_gpt_image_1_5', side_effect=RuntimeError("No key"))
    @patch.object(_mod, '_generate_with_gpt_image_2', side_effect=RuntimeError("No key"))
    @patch.object(_mod, '_generate_with_gpt_image_2_5_flare', side_effect=RuntimeError("No key"))
    @patch.object(_mod, '_generate_with_gpt_image_2_5_sunburst', side_effect=RuntimeError("No key"))
    @patch.object(_mod, '_generate_with_nano_banana_pro', side_effect=RuntimeError("No candidates"))
    @patch.object(_mod, '_generate_with_nano_banana_2', side_effect=RuntimeError("No candidates"))
    def test_generate_image_error_returns_error_text(
        self, mock_nb2, mock_nbp, mock_sunburst, mock_flare, mock_gpt2, mock_gpt15, mock_grok, mock_avail
    ):
        # When all backends fail, should return error text without raising
        with patch('tools.context.get_active_persona_id', return_value=None), \
             patch('tools.context.get_active_manager', return_value=None):
            text, info, path, metadata, item_id = generate_image('nothing')

        self.assertIn('失敗', text)
        self.assertIsNone(path)
        self.assertIsNone(metadata)
        self.assertIsNone(item_id)

    @patch.object(_mod, '_generate_with_gpt_image')
    def test_gpt_image_wrappers_pass_model_id(self, mock_common):
        mock_common.return_value = (b'imgdata', 'image/png')

        cases = [
            (_mod._generate_with_gpt_image_2_5_flare, "gpt-image-2.5-flare"),
            (_mod._generate_with_gpt_image_2_5_sunburst, "gpt-image-2.5-sunburst"),
            (_mod._generate_with_gpt_image_1_5, "gpt-image-1.5"),
            (_mod._generate_with_gpt_image_2, "gpt-image-2"),
        ]
        for wrapper, expected_model in cases:
            with self.subTest(model=expected_model):
                mock_common.reset_mock()
                wrapper('p')
                mock_common.assert_called_once()
                self.assertEqual(mock_common.call_args.args[0], expected_model)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    @patch('openai.OpenAI')
    def test_gpt_image_quality_clamp(self, mock_openai):
        # xhigh/max are GPT Image 2.5-only quality levels: legacy models must
        # receive 'high', 2.5 models must receive the value unchanged.
        client = mock_openai.return_value
        fake_result = MagicMock()
        fake_result.data = [MagicMock(b64_json=base64.b64encode(b'img').decode())]
        client.images.generate.return_value = fake_result

        cases = [
            ("gpt-image-2", "xhigh", "high"),
            ("gpt-image-2", "max", "high"),
            ("gpt-image-1.5", "max", "high"),
            ("gpt-image-2.5-flare", "xhigh", "xhigh"),
            ("gpt-image-2.5-sunburst", "max", "max"),
        ]
        for openai_model, requested, expected in cases:
            with self.subTest(model=openai_model, quality=requested):
                client.images.generate.reset_mock()
                client.images.generate.return_value = fake_result
                _mod._generate_with_gpt_image(openai_model, 'p', quality=requested)
                self.assertEqual(
                    client.images.generate.call_args.kwargs['quality'], expected
                )

    @patch.object(_mod, '_is_image_model_available', return_value=True)
    @patch.object(_mod, 'store_image_bytes')
    @patch.object(_mod, '_generate_with_gpt_image_2_5_flare')
    def test_generate_image_gpt_image_2_5_flare_dispatch(self, mock_gen, mock_store, mock_avail):
        mock_gen.return_value = (b'imgdata', 'image/png')

        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(b'imgdata')
            temp_path = Path(tmp.name)
        mock_store.return_value = ({"uri": "saiverse://image/test", "mime_type": "image/png"}, temp_path)

        with patch.object(_mod, 'get_active_persona_id', return_value=None, create=True), \
             patch.object(_mod, 'get_active_manager', return_value=None, create=True), \
             patch('tools.context.get_active_persona_id', return_value=None), \
             patch('tools.context.get_active_manager', return_value=None):
            text, info, path, metadata, item_id = generate_image(
                'a cat', model='gpt_image_2_5_flare'
            )

        self.assertIsInstance(info, ToolResult)
        self.assertIn('gpt_image_2_5_flare', text)
        self.assertEqual(Path(path), temp_path)
        mock_gen.assert_called_once()
        temp_path.unlink(missing_ok=True)

    def test_tool_registration(self):
        from tools import TOOL_REGISTRY
        self.assertIn('generate_image', TOOL_REGISTRY)


if __name__ == '__main__':
    unittest.main()
