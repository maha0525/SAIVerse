"""generator.generate_text_with_empty_retry (空応答の再試行) の回帰テスト。

2026-09-03 まはー裁定: 推論モデルが出力を reasoning_content だけに書いて
content を空で閉じることが確率的にあり、数千チャンクの走行が必ずどこかで
落ちる。純生成 (チャンク / 束ね / 吸収) の LLM 呼び出しは副作用を持たないので、
空応答だけは規定回数まで同じ呼び出しをやり直す。他の LLMError は再試行しない。
"""

import unittest

from llm_clients.exceptions import EmptyResponseError, LLMTimeoutError
from sai_memory.arasuji.generator import (
    DEFAULT_EMPTY_RESPONSE_ATTEMPTS,
    empty_response_attempts,
    generate_text_with_empty_retry,
)

_ENV = "SAIVERSE_CHRONICLE_EMPTY_RESPONSE_RETRIES"


class _SequenceClient:
    def __init__(self, responses):
        self.calls = 0
        self.kwargs_seen = []
        self.responses = list(responses)
        self.usage_reads = 0

    def generate(self, messages, tools, **kwargs):
        self.calls += 1
        self.kwargs_seen.append((messages, tools, kwargs))
        item = self.responses[self.calls - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    def consume_usage(self):
        self.usage_reads += 1
        return None


class TestEmptyRetry(unittest.TestCase):
    def setUp(self):
        import os
        self._saved = os.environ.pop(_ENV, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        import os
        if self._saved is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = self._saved

    def _set_env(self, value):
        import os
        os.environ[_ENV] = value

    def test_default_attempts_is_three(self):
        self.assertEqual(DEFAULT_EMPTY_RESPONSE_ATTEMPTS, 3)
        self.assertEqual(empty_response_attempts(), 3)

    def test_env_override_to_one_disables_retry(self):
        """env で総試行回数 1 → 再試行せず 1 回目の空で EmptyResponseError。"""
        self._set_env("1")
        self.assertEqual(empty_response_attempts(), 1)
        client = _SequenceClient(["", "来ないはず"])
        with self.assertRaises(EmptyResponseError) as ctx:
            generate_text_with_empty_retry(
                client, [{"role": "user", "content": "p"}], purpose="test",
            )
        self.assertEqual(client.calls, 1)
        self.assertEqual(ctx.exception.error_code, "empty_response")

    def test_invalid_env_falls_back_to_default(self):
        self._set_env("abc")
        self.assertEqual(empty_response_attempts(), 3)
        self._set_env("0")
        self.assertEqual(empty_response_attempts(), 3)

    def test_empty_string_then_text_returns_stripped_text(self):
        client = _SequenceClient(["\n", "  本文です。  "])
        with self.assertLogs("sai_memory.arasuji.generator", level="WARNING") as logs:
            out = generate_text_with_empty_retry(
                client, [{"role": "user", "content": "p"}], purpose="unit",
            )
        self.assertEqual(out, "本文です。")
        self.assertEqual(client.calls, 2)
        self.assertTrue(any("empty LLM response for unit (attempt 1/3)" in m
                            for m in logs.output))

    def test_last_empty_response_error_is_reraised(self):
        """最後の試行が EmptyResponseError なら、その例外そのものが上がる
        (呼び出し側が user_message / batch_meta を付けられる)。"""
        last = EmptyResponseError("third")
        client = _SequenceClient(["", EmptyResponseError("second"), last])
        with self.assertRaises(EmptyResponseError) as ctx:
            generate_text_with_empty_retry(
                client, [{"role": "user", "content": "p"}], purpose="unit",
            )
        self.assertIs(ctx.exception, last)
        self.assertEqual(client.calls, 3)

    def test_empty_response_error_twice_then_text_succeeds(self):
        """クライアントが EmptyResponseError を 2 回投げても 3 回目の本文で成功する
        (Ollama / NIM も RuntimeError ではなく EmptyResponseError を投げる前提)。"""
        client = _SequenceClient([
            EmptyResponseError("first"), EmptyResponseError("second"), "三度目の本文",
        ])
        out = generate_text_with_empty_retry(
            client, [{"role": "user", "content": "p"}], purpose="unit",
        )
        self.assertEqual(out, "三度目の本文")
        self.assertEqual(client.calls, 3)

    def test_other_llm_errors_propagate_without_retry(self):
        client = _SequenceClient([LLMTimeoutError("slow"), "来ないはず"])
        with self.assertRaises(LLMTimeoutError):
            generate_text_with_empty_retry(
                client, [{"role": "user", "content": "p"}], purpose="unit",
            )
        self.assertEqual(client.calls, 1)

    def test_usage_is_recorded_per_attempt(self):
        """空応答の往復も実際の API 呼び出し — usage は試行ごとに読む。"""
        client = _SequenceClient(["", "", "ok"])
        generate_text_with_empty_retry(
            client, [{"role": "user", "content": "p"}], purpose="unit",
            usage_node_type="chronicle_level1",
        )
        self.assertEqual(client.usage_reads, 3)

    def test_explicit_max_attempts_wins_over_env(self):
        self._set_env("5")
        client = _SequenceClient(["", "", "来ないはず"])
        with self.assertRaises(EmptyResponseError):
            generate_text_with_empty_retry(
                client, [{"role": "user", "content": "p"}], purpose="unit",
                max_attempts=2,
            )
        self.assertEqual(client.calls, 2)

    def test_kwargs_are_forwarded_to_generate(self):
        client = _SequenceClient(["ok"])
        generate_text_with_empty_retry(
            client, [{"role": "user", "content": "p"}], purpose="unit",
            temperature=0.2,
        )
        messages, tools, kwargs = client.kwargs_seen[0]
        self.assertEqual(tools, [])
        self.assertEqual(kwargs, {"temperature": 0.2})


if __name__ == "__main__":
    unittest.main()
