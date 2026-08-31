"""Attachment save/resolve paths must honor SAIVERSE_HOME.

Regression tests for the docker deployment bug where uploads were written to
``Path.home()/.saiverse/image`` (``/root/.saiverse`` in containers) while
``media_utils.resolve_media_uri`` resolved ``saiverse://image/...`` URIs
against ``SAIVERSE_HOME`` (``/data/.saiverse``), so image attachments never
reached the LLM.
"""
import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class AttachmentPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "saiverse_home"
        self._env = patch.dict(os.environ, {"SAIVERSE_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_store_uploaded_attachment_uses_saiverse_home(self):
        from api.routes.chat import _store_uploaded_attachment

        payload = "data:image/png;base64," + base64.b64encode(b"fake-png-bytes").decode("ascii")
        result = _store_uploaded_attachment(payload)

        self.assertIsNotNone(result)
        dest = Path(result["path"])
        self.assertEqual(dest.parent, self.home / "image")
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"fake-png-bytes")
        self.assertTrue(result["uri"].startswith("saiverse://image/"))

    def test_store_image_attachment_uses_saiverse_home(self):
        from api.routes.chat import AttachmentData, _store_image_attachment

        att = AttachmentData(data="", filename="cat.jpg", type="image", mime_type="image/jpeg")
        manager = MagicMock()
        manager.create_picture_item_for_user.return_value = "item-1"

        result = _store_image_attachment(b"jpeg-bytes", att, manager, "building_x")

        dest = Path(result["path"])
        self.assertEqual(dest.parent, self.home / "image")
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"jpeg-bytes")

    def test_store_document_attachment_uses_saiverse_home(self):
        from api.routes.chat import AttachmentData, _store_document_attachment

        att = AttachmentData(data="", filename="note.txt", type="document", mime_type="text/plain")
        manager = MagicMock()
        manager.create_document_item_for_user.return_value = "item-2"

        result = _store_document_attachment(b"hello doc", att, manager, "building_x")

        dest = Path(result["path"])
        self.assertEqual(dest.parent, self.home / "documents")
        self.assertTrue(dest.exists())

    def test_saved_attachment_uri_resolves_back_to_same_file(self):
        """The URI written into message metadata must resolve to the saved file."""
        from api.routes.chat import _store_uploaded_attachment
        from saiverse.media_utils import resolve_media_uri

        payload = "data:image/jpeg;base64," + base64.b64encode(b"roundtrip").decode("ascii")
        result = _store_uploaded_attachment(payload)

        resolved = resolve_media_uri(result["uri"])
        self.assertIsNotNone(resolved)
        self.assertEqual(Path(result["path"]), resolved)
        self.assertTrue(resolved.exists())


class MediaRecallSyncSummaryTests(unittest.TestCase):
    """グローバル設定「添付したメディアの内容を自動想起に使う」の sync_summary 経路。

    OFF (既定/sync_summary=False) は従来どおりバックグラウンド生成のみで
    metadata に summary が乗らない。ON (sync_summary=True) は同期生成して
    summary を返り値に載せる (sea/auto_recall.py の build_query が拾う入力)。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "saiverse_home"
        self._env = patch.dict(os.environ, {"SAIVERSE_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_sync_summary_off_keeps_background_thread_behavior(self):
        from api.routes.chat import AttachmentData, _store_image_attachment

        att = AttachmentData(data="", filename="cat.jpg", type="image", mime_type="image/jpeg")
        manager = MagicMock()
        manager.create_picture_item_for_user.return_value = "item-1"

        with patch("threading.Thread") as mock_thread:
            result = _store_image_attachment(
                b"jpeg-bytes", att, manager, "building_x",
                user_message="これ覚えてる？", prev_ai_message="",
                sync_summary=False,
            )
            mock_thread.assert_called_once()

        self.assertIsNone(result.get("summary"))
        manager.update_item_description.assert_not_called()

    def test_sync_summary_on_generates_synchronously(self):
        from api.routes.chat import AttachmentData, _store_image_attachment

        att = AttachmentData(data="", filename="cat.jpg", type="image", mime_type="image/jpeg")
        manager = MagicMock()
        manager.create_picture_item_for_user.return_value = "item-1"

        with patch(
            "saiverse.media_summary.generate_contextual_image_description",
            return_value="猫の写真です。",
        ) as mock_gen, patch("threading.Thread") as mock_thread:
            result = _store_image_attachment(
                b"jpeg-bytes", att, manager, "building_x",
                user_message="これ覚えてる？", prev_ai_message="",
                sync_summary=True,
            )
            mock_thread.assert_not_called()

        mock_gen.assert_called_once()
        self.assertEqual(result.get("summary"), "猫の写真です。")
        manager.update_item_description.assert_called_once_with("item-1", "猫の写真です。")

    def test_sync_summary_on_without_context_does_not_generate(self):
        # user_message も prev_ai_message も無ければ、ON でも生成自体が起きない
        # (既存の条件 `if item_id and (user_message or prev_ai_message)` を維持)。
        from api.routes.chat import AttachmentData, _store_image_attachment

        att = AttachmentData(data="", filename="cat.jpg", type="image", mime_type="image/jpeg")
        manager = MagicMock()
        manager.create_picture_item_for_user.return_value = "item-1"

        with patch("saiverse.media_summary.generate_contextual_image_description") as mock_gen:
            result = _store_image_attachment(
                b"jpeg-bytes", att, manager, "building_x",
                user_message="", prev_ai_message="",
                sync_summary=True,
            )
            mock_gen.assert_not_called()

        self.assertIsNone(result.get("summary"))

    def test_audio_sync_summary_uses_ensure_audio_summary(self):
        from api.routes.chat import AttachmentData, _store_audio_attachment

        att = AttachmentData(data="", filename="voice.wav", type="audio", mime_type="audio/wav")
        manager = MagicMock()
        manager.create_audio_item_for_user.return_value = "item-audio-1"

        with patch("saiverse.ffmpeg_runner.is_ffmpeg_available", return_value=True), \
             patch("saiverse.ffmpeg_runner.normalize_audio") as mock_normalize, \
             patch(
                 "saiverse.media_summary.ensure_audio_summary",
                 return_value="鳥の鳴き声です。",
             ) as mock_ensure:

            def _fake_normalize(src, dest, max_duration=300.0):
                dest.write_bytes(b"ogg-bytes")
                return True, None

            mock_normalize.side_effect = _fake_normalize

            result = _store_audio_attachment(
                b"wav-bytes", att, manager, "building_x", sync_summary=True,
            )

        mock_ensure.assert_called_once()
        self.assertEqual(result.get("summary"), "鳥の鳴き声です。")

    def test_audio_sync_summary_off_does_not_call_ensure(self):
        from api.routes.chat import AttachmentData, _store_audio_attachment

        att = AttachmentData(data="", filename="voice.wav", type="audio", mime_type="audio/wav")
        manager = MagicMock()
        manager.create_audio_item_for_user.return_value = "item-audio-1"

        with patch("saiverse.ffmpeg_runner.is_ffmpeg_available", return_value=True), \
             patch("saiverse.ffmpeg_runner.normalize_audio") as mock_normalize, \
             patch("saiverse.media_summary.ensure_audio_summary") as mock_ensure:

            def _fake_normalize(src, dest, max_duration=300.0):
                dest.write_bytes(b"ogg-bytes")
                return True, None

            mock_normalize.side_effect = _fake_normalize

            result = _store_audio_attachment(
                b"wav-bytes", att, manager, "building_x", sync_summary=False,
            )

        mock_ensure.assert_not_called()
        self.assertIsNone(result.get("summary"))


class IterImageMediaFallbackTests(unittest.TestCase):
    """iter_image_media should fall back to the recorded absolute path when the
    URI resolves to a missing file (e.g. records written before the path fix)."""

    def test_falls_back_to_direct_path_when_resolved_uri_missing(self):
        from saiverse.media_utils import iter_image_media

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "saiverse_home"
            real_file = Path(tmp) / "elsewhere" / "photo.jpg"
            real_file.parent.mkdir(parents=True)
            real_file.write_bytes(b"jpg")

            with patch.dict(os.environ, {"SAIVERSE_HOME": str(home)}):
                items = iter_image_media(
                    {
                        "images": [
                            {
                                "uri": "saiverse://image/photo.jpg",
                                "path": str(real_file),
                                "mime_type": "image/jpeg",
                            }
                        ]
                    }
                )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["path"], real_file)


if __name__ == "__main__":
    unittest.main()
