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
