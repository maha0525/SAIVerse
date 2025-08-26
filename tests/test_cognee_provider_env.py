import os
import unittest
from unittest import mock

from integrations.cognee_memory import CogneeMemory


class TestCogneeProviderEnv(unittest.TestCase):
    def test_huggingface_override(self):
        m = CogneeMemory.__new__(CogneeMemory)
        m.persona_id = "test"
        env_vars = {
            "LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": "dummy",
            "EMBEDDING_PROVIDER": "huggingface",
            "SAIVERSE_COGNEE_HF_EMBED_MODEL": "intfloat/multilingual-e5-base",
            "SAIVERSE_COGNEE_HF_EMBED_DIM": "768",
        }
        with mock.patch.dict(os.environ, env_vars, clear=False):
            env = m._provider_env()
        self.assertEqual(env.get("LLM_PROVIDER"), "gemini")
        self.assertEqual(env.get("EMBEDDING_PROVIDER"), "huggingface")
        self.assertEqual(env.get("EMBEDDING_MODEL"), "intfloat/multilingual-e5-base")
        self.assertEqual(env.get("EMBEDDING_DIMENSIONS"), "768")
        self.assertIsNone(env.get("EMBEDDING_API_KEY"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
