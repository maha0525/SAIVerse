import unittest
from pathlib import Path
import sys
import types as _types
import os

# --- Stub external dependencies (google-genai, openai) before imports ---
google_mod = _types.ModuleType("google")
genai_mod = _types.ModuleType("google.genai")

class _Dummy:
    def __init__(self, *a, **kw):
        pass

class _TypesNS:
    class SafetySetting(_Dummy):
        def __init__(self, category=None, threshold=None): pass
    class HarmCategory:
        HARM_CATEGORY_HATE_SPEECH = 0
        HARM_CATEGORY_HARASSMENT = 1
        HARM_CATEGORY_SEXUALLY_EXPLICIT = 2
        HARM_CATEGORY_DANGEROUS_CONTENT = 3
    class HarmBlockThreshold:
        BLOCK_ONLY_HIGH = 0
        BLOCK_NONE = 1
    class GoogleSearch(_Dummy):
        pass
    class Tool(_Dummy):
        def __init__(self, google_search=None, function_declarations=None): pass
    class FunctionDeclaration(_Dummy):
        def __init__(self, name=None, description=None, parameters=None, response=None): pass
    class Schema(_Dummy):
        def __init__(self, **kw): pass
    class Type(_Dummy):
        def __init__(self, v): pass
    class Content(_Dummy):
        def __init__(self, parts=None, role=None): pass
    class Part(_Dummy):
        def __init__(self, text=None): pass
    class GenerateContentConfig(_Dummy):
        def __init__(self, system_instruction=None, safety_settings=None, response_mime_type=None, tools=None): pass
    class FunctionResponse(_Dummy):
        pass

genai_mod.types = _TypesNS
class _Models:
    def generate_content(self, *a, **kw):
        # Return minimal JSON strings for both router and pulse callers
        cand = _types.SimpleNamespace(content=_types.SimpleNamespace(parts=[_types.SimpleNamespace(text='{"call":"no"}')]))
        return _types.SimpleNamespace(candidates=[cand], text='{"speak": false}')
class _Client:
    def __init__(self, api_key=None):
        self.models = _Models()
genai_mod.Client = _Client
types_mod = _types.ModuleType("google.genai.types")
for k, v in _TypesNS.__dict__.items():
    if not k.startswith("__"):
        setattr(types_mod, k, v)

google_mod.genai = genai_mod
sys.modules.setdefault("google", google_mod)
sys.modules.setdefault("google.genai", genai_mod)
sys.modules.setdefault("google.genai.types", types_mod)

# OpenAI stub
openai_mod = _types.ModuleType("openai")
class OpenAI(_Dummy):
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                class R: choices=[_types.SimpleNamespace(message=_types.SimpleNamespace(content="ok"))]
                return R()
openai_mod.OpenAI = OpenAI
sys.modules.setdefault("openai", openai_mod)

# Dotenv stub
dotenv_mod = _types.ModuleType("dotenv")
def _load_dotenv(*a, **kw):
    return None
dotenv_mod.load_dotenv = _load_dotenv
sys.modules.setdefault("dotenv", dotenv_mod)

# Minimal env to satisfy llm_router imports
os.environ.setdefault("GEMINI_FREE_API_KEY", "test-key")

# database.models stub to avoid SQLAlchemy dependency
db_mod = _types.ModuleType("database")
db_models_mod = _types.ModuleType("database.models")
class _AI: pass
db_models_mod.AI = _AI
sys.modules.setdefault("database", db_mod)
sys.modules.setdefault("database.models", db_models_mod)

from buildings import Building
from persona_core import PersonaCore


class DummyLLM:
    def __init__(self, text="OK"):
        self.text = text

    def generate(self, messages):
        return self.text

    def generate_stream(self, messages):
        for ch in self.text:
            yield ch


class TestConvIdThreading(unittest.TestCase):
    def setUp(self):
        # Minimal building setup
        self.buildings = [
            Building(building_id="user_room_city_a", name="まはーの部屋"),
            Building(building_id="deep_think_room_city_a", name="思索の部屋"),
        ]
        self.core = PersonaCore(
            city_name="city_a",
            persona_id="air_city_a",
            persona_name="air",
            persona_system_instruction="",
            avatar_image=None,
            buildings=self.buildings,
            common_prompt_path=Path("system_prompts/common.txt"),
            action_priority_path=Path("action_priority.json"),
            building_histories={},
            occupants={},
            id_to_name_map={"air_city_a": "air", "eris_city_a": "eris"},
            move_callback=None,
            dispatch_callback=None,
            explore_callback=None,
            create_persona_callback=None,
            session_factory=lambda: None,
            start_building_id="user_room_city_a",
            model="gemini-2.0-flash",
            context_length=4096,
            user_room_id="user_room_city_a",
            provider="gemini",
            user_id=1,
            is_visitor=True,
        )
        # Replace LLM client with dummy
        self.core.llm_client = DummyLLM("ASSIST")
        # Disable emotion module external calls
        self.core.emotion_module.evaluate = lambda *a, **k: None
        # Replace MemoryCore with a minimal, dependency-free instance
        from memory_core.storage import InMemoryStorage
        from memory_core.embeddings import SimpleHashEmbedding
        from memory_core.config import Config as _Cfg
        from memory_core.pipeline import MemoryCore as _MC
        self.core.memory_core = _MC(storage=InMemoryStorage(), embedder=SimpleHashEmbedding(), config=_Cfg())
        # Redirect file outputs to workspace-local path
        base = Path("ai_sessions/test_core").resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.core.saiverse_home = base
        self.core.persona_log_path = base / "personas" / self.core.persona_id / "log.json"
        self.core.conscious_log_path = base / "personas" / self.core.persona_id / "conscious_log.json"
        self.core.persona_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.core.conscious_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.core.building_memory_paths = {b.building_id: base / "buildings" / b.building_id / "log.json" for b in self.buildings}
        for p in self.core.building_memory_paths.values():
            p.parent.mkdir(parents=True, exist_ok=True)
        # Sync paths on HistoryManager as well
        self.core.history_manager.persona_log_path = self.core.persona_log_path
        self.core.history_manager.building_memory_paths = self.core.building_memory_paths

    def test_user_conv_id(self):
        # User triggers a turn; conv_id should be user:1
        self.core.handle_user_input("hello")
        entries = self.core.memory_core.storage.list_entries_by_conversation("user:1")
        # Should have user + assistant entries
        self.assertGreaterEqual(len(entries), 2)
        self.assertEqual(entries[0].speaker, "user")
        self.assertEqual(entries[1].speaker, "ai")

    def test_persona_trigger_conv_id(self):
        # Another persona speaks in the same building
        self.core.history_manager.add_message(
            {"role": "assistant", "content": "ping", "persona_id": "eris_city_a"},
            "user_room_city_a",
        )
        # Pulse perceives and sets last conv id to persona:eris_city_a
        _ = self.core.run_pulse(occupants=["air_city_a", "eris_city_a"], user_online=True)
        # Now generate a self-initiated utterance (no user message)
        self.core.llm_client = DummyLLM("PONG")
        say, _, _ = self.core._generate(None, system_prompt_extra="auto")
        self.assertEqual(say, "PONG")
        entries = self.core.memory_core.storage.list_entries_by_conversation("persona:eris_city_a")
        # Only assistant entry likely saved for this conv_id in this step
        self.assertGreaterEqual(len(entries), 1)
        self.assertEqual(entries[-1].speaker, "ai")


if __name__ == "__main__":
    unittest.main()
