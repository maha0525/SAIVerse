import tempfile
import unittest
from pathlib import Path

from persona.mixins.pulse import PersonaPulseMixin
from persona.tasks.storage import TaskStorage


class DummyPulse(PersonaPulseMixin):
    def __init__(self, persona_id: str, persona_dir: Path) -> None:
        self.persona_id = persona_id
        self.persona_name = persona_id
        self.persona_system_instruction = ""
        self.buildings = {}
        self.task_storage = TaskStorage(persona_id, base_dir=persona_dir.parent.parent)


class PulseTaskSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name) / ".saiverse"
        persona_dir = base / "personas" / "tester"
        persona_dir.mkdir(parents=True)
        self.pulse = DummyPulse("tester", persona_dir)
        self.pulse.task_storage.create_task(
            title="短編制作",
            goal="短編制作を完了する",
            summary="短編制作",
            notes=None,
            steps=[{"title": "構想"}, {"title": "執筆"}],
            actor="tester",
        )

    def tearDown(self) -> None:
        self.pulse.task_storage.close()
        self.tmp.cleanup()

    def test_compose_task_summary_includes_active(self) -> None:
        summary = self.pulse._compose_task_summary()
        self.assertIn("アクティブタスク", summary)
        self.assertIn("短編制作", summary)


if __name__ == "__main__":
    unittest.main()
