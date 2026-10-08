import base64
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import get_args
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

    def test_xai_quality_params(self):
        # Imagine 2.0 は (quality, resolution) の組で課金され、quality は low/medium のみ。
        # 最上位のツール品質は medium@2k に寄せる。
        cases = [
            ("low", {"quality": "low", "resolution": "1k"}),
            ("medium", {"quality": "medium", "resolution": "1k"}),
            ("high", {"quality": "medium", "resolution": "2k"}),
            ("auto", {"quality": "medium", "resolution": "2k"}),
            ("xhigh", {"quality": "medium", "resolution": "2k"}),
            ("max", {"quality": "medium", "resolution": "2k"}),
        ]
        for requested, expected in cases:
            with self.subTest(quality=requested):
                self.assertEqual(_mod._xai_quality_params(requested), expected)

    @patch.dict(os.environ, {"XAI_API_KEY": "test-key"})
    @patch('xai_sdk.Client')
    def test_grok_imagine_request_shape(self, mock_client):
        response = MagicMock()
        response.data = base64.b64encode(b'img').decode()
        mock_client.return_value.image.sample.return_value = response

        image_bytes, mime = _mod._generate_with_grok_imagine('p', '16:9', 'medium')

        self.assertEqual((image_bytes, mime), (b'img', 'image/png'))
        kwargs = mock_client.return_value.image.sample.call_args.kwargs
        self.assertEqual(kwargs['model'], 'grok-imagine-image-2.0')
        self.assertEqual(kwargs['aspect_ratio'], '16:9')
        self.assertEqual(kwargs['quality'], 'medium')
        self.assertEqual(kwargs['resolution'], '1k')
        self.assertEqual(kwargs['image_format'], 'base64')

    def test_sniff_image_mime(self):
        # xAI は JPEG を返す。中身から MIME を決めないと JPEG が .png / image/png で保存される。
        cases = [
            (b"\xff\xd8\xff\xe0" + b"x" * 20, "image/jpeg"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
            (b"\x89PNG\r\n\x1a\n" + b"x" * 20, "image/png"),
            (b"not an image", "image/png"),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected, head=data[:4]):
                self.assertEqual(_mod._sniff_image_mime(data), expected)

    @patch.dict(os.environ, {"XAI_API_KEY": "test-key"})
    @patch('xai_sdk.Client')
    def test_grok_returns_mime_of_the_actual_bytes(self, mock_client):
        jpeg = b"\xff\xd8\xff\xe0" + b"x" * 20
        response = MagicMock()
        response.data = base64.b64encode(jpeg).decode()
        mock_client.return_value.image.sample.return_value = response

        _, mime = _mod._generate_with_grok_imagine('p')

        self.assertEqual(mime, 'image/jpeg')

    @patch.object(_mod, '_is_image_model_available', return_value=True)
    @patch.object(_mod, 'store_image_bytes')
    @patch.object(_mod, '_generate_with_grok_imagine')
    def test_generate_image_grok_imagine_dispatch(self, mock_gen, mock_store, mock_avail):
        # バックエンドが返した MIME (xAI は JPEG) が、そのまま保存側へ渡ること。
        mock_gen.return_value = (b'imgdata', 'image/jpeg')

        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(b'imgdata')
            temp_path = Path(tmp.name)
        mock_store.return_value = ({"uri": "saiverse://image/test", "mime_type": "image/png"}, temp_path)

        with patch.object(_mod, 'get_active_persona_id', return_value=None, create=True), \
             patch.object(_mod, 'get_active_manager', return_value=None, create=True), \
             patch('tools.context.get_active_persona_id', return_value=None), \
             patch('tools.context.get_active_manager', return_value=None):
            text, info, path, metadata, item_id = generate_image(
                'a cat', model='grok_imagine', aspect_ratio='16:9', quality='low'
            )

        self.assertIn('grok_imagine', text)
        self.assertEqual(Path(path), temp_path)
        mock_gen.assert_called_once()
        # プロンプト・縦横比・品質が、この順でバックエンドへ届くこと。
        self.assertEqual(mock_gen.call_args.args[:3], ('a cat', '16:9', 'low'))
        self.assertEqual(mock_store.call_args.args[1], 'image/jpeg')
        temp_path.unlink(missing_ok=True)

    @patch('llm_clients.gemini_utils.build_gemini_clients')
    @patch.object(_mod, '_load_image_bytes', return_value=(b'reference', 'image/jpeg'))
    def test_nano_banana_request_shape(self, mock_load, mock_clients):
        client = MagicMock()
        mock_clients.return_value = (None, client, client)
        part = MagicMock()
        part.inline_data.data = b'generated'
        part.inline_data.mime_type = 'image/webp'
        response = MagicMock()
        response.candidates = [MagicMock()]
        response.candidates[0].content.parts = [part]
        response.prompt_feedback = None
        client.models.generate_content.return_value = response

        for model_id in ('gemini-3.1-flash-image-preview', 'gemini-nano-banana-2.1'):
            for quality, resolution in [('low', '1K'), ('medium', '2K'), ('high', '4K'),
                                        ('xhigh', '4K'), ('max', '4K')]:
                with self.subTest(model=model_id, quality=quality):
                    result = _mod._generate_with_nano_banana_2(
                        'a cat', '16:9', quality, [Path('reference.jpg')], model_id=model_id
                    )
                    self.assertEqual(result, (b'generated', 'image/webp'))
                    mock_clients.assert_called_with(prefer_paid=True)
                    kwargs = client.models.generate_content.call_args.kwargs
                    self.assertEqual(kwargs['model'], model_id)
                    self.assertEqual(kwargs['contents'][-1], 'a cat')
                    self.assertEqual(kwargs['contents'][0].inline_data.data, b'reference')
                    self.assertEqual(kwargs['contents'][0].inline_data.mime_type, 'image/jpeg')
                    config = kwargs['config']
                    self.assertEqual(config.response_modalities, ['TEXT', 'IMAGE'])
                    self.assertEqual(config.image_config.aspect_ratio, '16:9')
                    self.assertEqual(config.image_config.image_size, resolution)
                    self.assertTrue(config.automatic_function_calling.disable)
                    self.assertIsNone(config.tools)
                    self.assertIsNone(config.response_schema)
                    self.assertIsNone(config.cached_content)

        _mod._generate_with_nano_banana_2('legacy default')
        self.assertEqual(client.models.generate_content.call_args.kwargs['model'],
                         'gemini-3.1-flash-image-preview')

    @patch('llm_clients.gemini_utils.build_gemini_clients')
    def test_nano_banana_2_1_requires_paid_client(self, mock_clients):
        free_client = MagicMock()
        mock_clients.return_value = (free_client, None, free_client)
        with self.assertRaisesRegex(RuntimeError, 'paid tier'):
            _mod._generate_with_nano_banana_2('a cat', model_id='gemini-nano-banana-2.1')
        free_client.models.generate_content.assert_not_called()

    @patch.object(_mod, '_is_image_model_available', return_value=True)
    @patch.object(_mod, 'store_image_bytes')
    @patch.object(_mod, '_generate_with_nano_banana_pro', return_value=(b'img', 'image/webp'))
    @patch.object(_mod, '_generate_with_nano_banana_2')
    def test_nano_banana_2_1_dispatch_and_fallback(self, mock_gen, mock_pro, mock_store, mock_avail):
        with tempfile.TemporaryDirectory() as directory:
            temp_path = Path(directory) / 'generated.webp'
            mock_store.return_value = ({'uri': 'saiverse://image/test', 'mime_type': 'image/webp'}, temp_path)
            for fail_first in (False, True):
                with self.subTest(fallback=fail_first), \
                     patch('tools.context.get_active_persona_id', return_value=None), \
                     patch('tools.context.get_active_manager', return_value=None):
                    mock_gen.reset_mock()
                    mock_pro.reset_mock()
                    mock_gen.side_effect = ([RuntimeError('unavailable'), (b'img', 'image/webp')]
                                            if fail_first else [(b'img', 'image/webp')])
                    text, info, path, metadata, item_id = generate_image(
                        'a cat', model='nano_banana_2_1', aspect_ratio='16:9', quality='low'
                    )
                    self.assertEqual(Path(path), temp_path)
                    self.assertIsInstance(info, ToolResult)
                    self.assertIsNone(item_id)
                    self.assertIn('media', metadata)
                    first = mock_gen.call_args_list[0]
                    self.assertEqual(first.args, ('a cat', '16:9', 'low', []))
                    self.assertEqual(first.kwargs, {'model_id': 'gemini-nano-banana-2.1'})
                    self.assertEqual(mock_store.call_args.args[:2], (b'img', 'image/webp'))
                    if fail_first:
                        self.assertEqual(mock_gen.call_count, 1)
                        mock_pro.assert_called_once_with('a cat', '16:9', 'low', [])
                        self.assertIn('サーバーエラー', text)
                    else:
                        self.assertEqual(mock_gen.call_count, 1)
                        self.assertIn('nano_banana_2_1', text)

    def test_nano_banana_2_1_replaces_default_and_fallback(self):
        name = 'nano_banana_2_1'
        model_schema = _mod.schema().parameters['properties']['model']
        self.assertIn(name, model_schema['enum'])
        self.assertIn(name, get_args(_mod.ModelType))
        self.assertEqual(model_schema['default'], name)
        self.assertEqual(inspect.signature(generate_image).parameters['model'].default, name)
        self.assertEqual(_mod._FALLBACK_ORDER, [
            'nano_banana_2_1', 'nano_banana_pro', 'gpt_image_2_5_flare',
            'gpt_image_2_5_sunburst', 'gpt_image_1_5', 'gpt_image_2', 'grok_imagine',
        ])
        self.assertEqual(_mod._get_model_api_key_env(name), 'GEMINI_API_KEY')
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'synthetic'}, clear=True):
            self.assertIn(name, _mod.get_available_image_models())
        with patch.dict(os.environ, {'GEMINI_FREE_API_KEY': 'synthetic'}, clear=True):
            self.assertNotIn(name, _mod.get_available_image_models())
        playbook_path = Path(__file__).resolve().parents[1] / 'builtin_data/playbooks/public/generate_image_playbook.json'
        playbook = json.loads(playbook_path.read_text(encoding='utf-8'))
        from sea.playbook_models import PlaybookSchema, validate_playbook_graph
        validate_playbook_graph(PlaybookSchema(**playbook))
        decide = next(node for node in playbook['nodes'] if node['id'] == 'decide_prompt')
        self.assertIn(name, decide['response_schema']['properties']['model']['enum'])
        self.assertNotIn('nano_banana_2', decide['response_schema']['properties']['model']['enum'])
        self.assertIn('デフォルトはnano_banana_2_1', decide['response_schema']['properties']['model']['description'])

    def test_tool_registration(self):
        from tools import TOOL_REGISTRY
        self.assertIn('generate_image', TOOL_REGISTRY)


if __name__ == '__main__':
    unittest.main()
