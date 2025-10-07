import unittest

from memory_core import MemoryCore


class TestMemoryCoreLLMFlow(unittest.TestCase):
    def setUp(self):
        self.mc = MemoryCore.create_default(with_dummy_llm=True)

    def test_remember_and_recall(self):
        # 記憶をいくつか投入
        self.mc.remember("那須塩原の吊り橋の写真、送ったよ。めっちゃ揺れたね…", conv_id="c1", speaker="user")
        self.mc.remember("あの吊り橋は高かったね。スリルあった。", conv_id="c1", speaker="ai")
        self.mc.remember("来月また旅行行こう。今度は温泉に入りたい。", conv_id="c1", speaker="user")

        # 想起を実行
        res = self.mc.recall("あの旅行、また行きたいな。")
        texts = res["texts"]
        topics = res["topics"]

        self.assertTrue(len(texts) > 0, "想起結果テキストが返る")
        # トピックが最低1つは作成・紐づけされていること
        self.assertTrue(len(topics) >= 1, "関連トピックが返る")
        # 代表的な語が含まれるテキストが想起されること（弱い検査）
        self.assertTrue(any("吊り橋" in t or "旅行" in t for t in texts))


if __name__ == "__main__":
    unittest.main()

