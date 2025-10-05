import unittest

from sai_memory.memory.recall import semantic_recall, semantic_recall_groups
from sai_memory.memory.storage import (
    add_message,
    get_or_create_thread,
    init_db,
    replace_message_embeddings,
)


class DummyEmbedder:
    def __init__(self, vector):
        self._vector = vector

    def embed(self, texts):
        return [self._vector for _ in texts]


class TestSAIMemoryStorage(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        get_or_create_thread(self.conn, "thread-1", resource_id="resource-1")

    def test_replace_message_embeddings_multiple_chunks(self):
        mid = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="chunked message",
            resource_id="resource-1",
        )
        replace_message_embeddings(self.conn, mid, ([1.0, 0.0], [0.0, 1.0]))

        results = semantic_recall(
            self.conn,
            DummyEmbedder([1.0, 0.0]),
            "query",
            thread_id="thread-1",
            resource_id=None,
            topk=1,
            range_before=0,
            range_after=0,
            scope="thread",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, mid)

    def test_semantic_recall_groups_deduplicates_chunks(self):
        mid1 = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="first",
            resource_id="resource-1",
        )
        mid2 = add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="second",
            resource_id="resource-1",
        )
        replace_message_embeddings(self.conn, mid1, ([1.0, 0.0], [0.9, 0.1]))
        replace_message_embeddings(self.conn, mid2, ([0.0, 1.0],))

        groups = semantic_recall_groups(
            self.conn,
            DummyEmbedder([1.0, 0.0]),
            "query",
            thread_id="thread-1",
            resource_id=None,
            topk=2,
            range_before=0,
            range_after=0,
            scope="thread",
        )
        self.assertEqual(len(groups), 2)
        seed_ids = [seed.id for seed, _, _ in groups]
        self.assertEqual(len(seed_ids), len(set(seed_ids)))

    def test_semantic_recall_groups_respects_exclude_ids(self):
        mid1 = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="first",
            resource_id="resource-1",
        )
        mid2 = add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="second",
            resource_id="resource-1",
        )
        replace_message_embeddings(self.conn, mid1, ([1.0, 0.0],))
        replace_message_embeddings(self.conn, mid2, ([0.0, 1.0],))

        groups = semantic_recall_groups(
            self.conn,
            DummyEmbedder([1.0, 0.0]),
            "query",
            thread_id="thread-1",
            resource_id=None,
            topk=2,
            range_before=0,
            range_after=0,
            scope="thread",
            exclude_message_ids={mid1},
        )
        self.assertTrue(all(seed.id != mid1 for seed, _, _ in groups))

    def test_semantic_recall_groups_includes_context_messages(self):
        mid_user = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="ユーザーの発言",
            resource_id="resource-1",
        )
        mid_target = add_message(
            self.conn,
            thread_id="thread-1",
            role="assistant",
            content="ターゲット",
            resource_id="resource-1",
        )
        mid_after = add_message(
            self.conn,
            thread_id="thread-1",
            role="user",
            content="次の発言",
            resource_id="resource-1",
        )
        replace_message_embeddings(self.conn, mid_user, ([0.5, 0.5],))
        replace_message_embeddings(self.conn, mid_target, ([1.0, 0.0],))
        replace_message_embeddings(self.conn, mid_after, ([0.4, 0.6],))

        groups = semantic_recall_groups(
            self.conn,
            DummyEmbedder([1.0, 0.0]),
            "query",
            thread_id="thread-1",
            resource_id=None,
            topk=1,
            range_before=1,
            range_after=1,
            scope="thread",
        )
        self.assertEqual(len(groups), 1)
        _, bundle, _ = groups[0]
        bundle_ids = [msg.id for msg in bundle]
        self.assertIn(mid_user, bundle_ids)
        self.assertIn(mid_target, bundle_ids)
        self.assertIn(mid_after, bundle_ids)


if __name__ == "__main__":
    unittest.main()
