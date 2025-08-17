import unittest
from datetime import datetime, timezone

from memory_core.pipeline import MemoryCore
from memory_core.schemas import Topic, MemoryEntry
from memory_core.organizer import run_topic_merge


class TestTopicMergeProtocol(unittest.TestCase):
    def setUp(self):
        # Use default in-memory storage and simple embedding
        self.mc = MemoryCore.create_default(with_dummy_llm=True)

    def _mk_entry(self, eid: str, text: str) -> MemoryEntry:
        return MemoryEntry(
            id=eid,
            conversation_id="c-test",
            turn_index=0,
            timestamp=datetime.now(timezone.utc),
            speaker="user",
            raw_text=text,
            summary=text[:60],
            embedding=self.mc.embedder.embed([text])[0],
            emotion=None,
            linked_topics=[],
            previous_topics=[],
            linked_entries=[],
            meta={},
            raw_pointer=None,
        )

    def _mk_topic(self, tid: str, title: str, entry_ids=None) -> Topic:
        now = datetime.now(timezone.utc)
        return Topic(
            id=tid,
            title=title,
            summary=f"{title} に関する話題",
            created_at=now,
            updated_at=now,
            strength=0.1,
            centroid_embedding=self.mc.embedder.embed([title])[0],
            centroid_emotion=None,
            entry_ids=list(entry_ids or []),
            parents=[],
            children=[],
            disabled=False,
        )

    def test_force_merge_creates_general_topic_and_moves_entries(self):
        st = self.mc.storage
        # Create many small topics containing the shared keyword "旅行"
        small_topic_ids = []
        entry_ids = []
        for i in range(22):
            eid = f"e{i}"
            tid = f"t_small_{i}"
            entry = self._mk_entry(eid, f"来月の旅行プラン{i}")
            entry.linked_topics.append(tid)
            st.upsert_entry(entry)
            entry_ids.append(eid)
            topic = self._mk_topic(tid, f"旅行の話題{i}", [eid])
            st.upsert_topic(topic)
            small_topic_ids.append(tid)

        # Create some unrelated small topics
        for i in range(5):
            eid = f"e_misc_{i}"
            tid = f"t_misc_{i}"
            entry = self._mk_entry(eid, f"猫の写真{i}")
            entry.linked_topics.append(tid)
            st.upsert_entry(entry)
            topic = self._mk_topic(tid, f"猫の話題{i}", [eid])
            st.upsert_topic(topic)

        # Create a large topic (>=10 entries) that should be blocked as source
        large_tid = "t_large"
        large_entry_ids = []
        for i in range(12):
            eid = f"e_large_{i}"
            e = self._mk_entry(eid, f"大規模旅行まとめ{i}")
            e.linked_topics.append(large_tid)
            st.upsert_entry(e)
            large_entry_ids.append(eid)
        large_topic = self._mk_topic(large_tid, "旅行の大全", large_entry_ids)
        st.upsert_topic(large_topic)

        # Sanity: we have > 30 topics to match the threshold, but we'll force anyway
        topics_before = st.list_topics()
        self.assertTrue(len(topics_before) >= 28)  # 22 + 5 + 1 >= 28

        # Run merge forced to ignore count threshold
        result = run_topic_merge(st, self.mc.embedder, min_topics=30, block_source_threshold=10, force=True)
        self.assertEqual(result.get("status"), "merged")
        new_tid = result.get("new_topic_id")
        self.assertIsNotNone(new_tid)
        self.assertIn("keyword", result)

        # Validate new topic exists and has moved entries from small topics with the keyword
        new_topic = st.get_topic(new_tid)
        self.assertIsNotNone(new_topic)
        self.assertGreaterEqual(len(new_topic.entry_ids), 10)
        # Moved entries must not include those from the large blocked topic
        for eid in large_entry_ids:
            self.assertNotIn(eid, new_topic.entry_ids)

        # Source topics are disabled and emptied
        for tid in result.get("source_topics", []):
            t = st.get_topic(tid)
            self.assertTrue(t.disabled)
            self.assertEqual(len(t.entry_ids), 0)

        # Entries have previous_topics recorded
        moved_eids = new_topic.entry_ids
        if moved_eids:
            e0 = st.get_entry(moved_eids[0])
            self.assertTrue(len(e0.previous_topics) >= 1)


if __name__ == "__main__":
    unittest.main()

