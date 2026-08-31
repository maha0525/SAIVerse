"""Regression test for realtime-spell media forwarding.

Background: realtime spells (e.g. the auto-`see` that runs while a persona
inhabits the Stack-chan vessel) are executed by
``sea.runtime_llm._execute_realtime_spells``. The ``see`` tool returns its
captured image via ``result_meta['media']``, exactly like the normal spell
loop and pre_spells paths. Previously this path injected only the text stub
("目の前の光景を見た。") and dropped ``result_meta`` entirely, so the persona
never actually received the image. This test pins the fix: media returned by
a realtime spell must be attached onto the realtime context message's
``metadata['media']``.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock


class _FakeBinding:
    def __init__(self, spell_name, args_json=None, label=None, binding_id=1, priority=0):
        self.SPELL_NAME = spell_name
        self.SPELL_ARGS_JSON = args_json
        self.LABEL = label
        self.BINDING_ID = binding_id
        self.PRIORITY = priority
        self.ENABLED = True
        self.OWNER_KIND = "persona"
        self.OWNER_ID = "air_test"


class _FakeQuery:
    """Mimics the SQLAlchemy query chain, ignoring filter/order_by args."""

    def __init__(self, result):
        self._result = result

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._result


class _FakeSession:
    def __init__(self, bindings):
        self._bindings = bindings
        self.closed = False

    def query(self, *a, **k):
        return _FakeQuery(self._bindings)

    def close(self):
        self.closed = True


def _make_realtime_message():
    """The message prepare-context creates before realtime spells run."""
    return {
        "role": "user",
        "content": "<system>\n## リアルタイム情報\n- 現在時刻: 2026年06月29日\n</system>",
        "metadata": {"__realtime_context__": True},
    }


class RealtimeSpellMediaTest(unittest.TestCase):
    def _run(self, bindings, fake_spell):
        from sea import runtime_llm

        media_descriptor = {"path": "/tmp/see.jpg", "mime_type": "image/jpeg"}

        messages = [
            {"role": "user", "content": "前のユーザー発話"},
            _make_realtime_message(),
            {"role": "user", "content": "まはーの最新発話: 見えてる?"},
        ]
        state = {"_messages": messages}
        session = _FakeSession(bindings)
        runtime = SimpleNamespace(manager=SimpleNamespace(SessionLocal=lambda: session))
        persona = SimpleNamespace(persona_id="air_test")

        # This test fully controls SPELL_TOOL_NAMES with a narrowed set, so
        # short-circuit addon-name canonicalization (which consults the real
        # tool registry) — its behavior is pinned in
        # tests/test_native_tool_addon_prefix.py.
        with mock.patch.object(runtime_llm, "SPELL_TOOL_NAMES", {"see"}), \
             mock.patch.object(runtime_llm, "canonicalize_spell_name", lambda n: n), \
             mock.patch.object(runtime_llm, "_run_spell_tool_async", fake_spell):
            asyncio.run(
                runtime_llm._execute_realtime_spells(
                    runtime, persona, "stackchan_room", state, None
                )
            )
        return messages, media_descriptor

    def test_media_attached_to_realtime_message(self):
        media_descriptor = {"path": "/tmp/see.jpg", "mime_type": "image/jpeg"}

        async def fake_spell(spell_name, args, persona, state, marker, cb, messages=None):
            return "目の前の光景を見た。", {"media": [media_descriptor]}, True

        messages, _ = self._run([_FakeBinding("see", label="see")], fake_spell)

        realtime_msg = next(
            m for m in messages if m.get("metadata", {}).get("__realtime_context__")
        )
        # Text stub still injected.
        self.assertIn("目の前の光景を見た。", realtime_msg["content"])
        # The image media is now attached (previously dropped).
        self.assertEqual(realtime_msg["metadata"].get("media"), [media_descriptor])
        # The realtime marker must survive.
        self.assertTrue(realtime_msg["metadata"].get("__realtime_context__"))

    def test_text_only_spell_attaches_no_media(self):
        async def fake_spell(spell_name, args, persona, state, marker, cb, messages=None):
            return "気圧: 1008.8 hPa", {}, True

        messages, _ = self._run([_FakeBinding("see", label="気圧")], fake_spell)

        realtime_msg = next(
            m for m in messages if m.get("metadata", {}).get("__realtime_context__")
        )
        self.assertIn("気圧: 1008.8 hPa", realtime_msg["content"])
        self.assertIsNone(realtime_msg["metadata"].get("media"))


if __name__ == "__main__":
    unittest.main()
