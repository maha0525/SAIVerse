import unittest

from sai_memory.memory.chunking import chunk_text


class TestChunkText(unittest.TestCase):
    def test_prefers_sentence_and_newline_boundaries(self):
        text = "これはテスト。とても長い文章だけど。\n改行も入っているよ。最後の行です。"
        chunks = chunk_text(text, min_chars=10, max_chars=25)

        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual("".join(chunks), text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 25)
        if len(chunks) > 1:
            for chunk in chunks[:-1]:
                self.assertGreaterEqual(len(chunk), 10)

    def test_force_split_long_segment(self):
        text = "a" * 53
        chunks = chunk_text(text, min_chars=10, max_chars=20)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_short_text_remains_single_chunk(self):
        text = "短い"
        chunks = chunk_text(text, min_chars=10, max_chars=20)
        self.assertEqual(chunks, [text])


if __name__ == "__main__":
    unittest.main()
