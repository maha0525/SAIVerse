"""同席想起の「再会の門」と見出しの名前解決の契約テスト (v0.3.9)。

門が配線されていなかったため、ずっと会話している相手にも移動のたびに
「過去会話 6 件 + 相手の Memopedia 個人ページ全文」が積まれ、本番で知覚が
18 万字まで膨らんだ (docs/issues/archive/persona_recall_perception_unbounded.md)。

ここでは想起の繋ぎ実装 (`inject_copresence_recall`) と本物の
`HistoryManager.should_recall_persona` / `recall_conversation_with` を繋いだまま
検査する — 門を呼ぶ配線と、見出しに ID 生値ではなく表示名が載ることの両方が
壊れたら落ちるようにするため。

発火点は 2026-09-07 に「移動時の入室ラベル」から「Pulse の頭で、いま同席して
いる相手」へ移った (docs/issues/perception_state_pushed_at_event_time.md)。
門の判定規約そのものは変わっていない。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from persona.history_manager import HistoryManager
from sea.head_pipeline.integration import (
    inject_copresence_recall,
    reset_copresence_recall_memory,
)

TARGET = "elis_city_a"
ROOM = "b_air_room"


class _FakeManager:
    """同席者の列挙が読む面だけの替え玉。

    ``persona_occupants`` に挙げた ID がペルソナ、それ以外の同席者はユーザー。
    """

    def __init__(self, occupants, persona_occupants):
        self.occupants = {ROOM: list(occupants)}
        self.personas = {oid: object() for oid in persona_occupants}


class _FakeAdapter:
    """SAIMemory adapter のうち recall が触る面だけの替え玉。"""

    def __init__(self, past_messages):
        self._past_messages = past_messages
        self.conn = object()  # memopedia storage へ渡されるだけ (patch 済み)

    def is_ready(self):
        return True

    def get_messages_with_persona_in_audience(self, target_persona_id, **kwargs):
        return self._past_messages


class RecallGateTests(unittest.TestCase):
    def setUp(self):
        # 「同席の間は一度だけ試みる」記憶はプロセス内で共有される。同じ
        # ペルソナ・同じ相手を何度も走らせるので、テストごとに忘れさせる。
        reset_copresence_recall_memory()
        self.addCleanup(reset_copresence_recall_memory)
        self.pushed = []
        self.past_messages = [
            {"role": "assistant", "content": "また会えたね", "created_at": 1750000000},
        ]
        self.sai_mem = SimpleNamespace(
            is_ready=lambda: True,
            # metadata (同席の印) つきで呼ばれる — 形の検査は
            # tests/test_copresence_recall.py が持つ。
            push_perception=(
                lambda kind, text, **kwargs: self.pushed.append((kind, text))
            ),
        )

    def _make_persona(self, *, messages, id_to_name_map=None, memopedia_content=None):
        hm = HistoryManager(
            persona_id="air_city_a",
            persona_log_path=Path("/mock/personas/air_city_a/log.json"),
            building_memory_paths={},
            initial_persona_history=list(messages),
            memory_adapter=_FakeAdapter(self.past_messages),
        )
        self.memopedia_page = (
            SimpleNamespace(content=memopedia_content)
            if memopedia_content is not None else None
        )
        return SimpleNamespace(
            persona_id="air_city_a",
            history_manager=hm,
            sai_memory=self.sai_mem,
            id_to_name_map=dict(id_to_name_map or {}),
        )

    def _run(self, persona, occupant_id, occupant_kind="persona"):
        manager = _FakeManager(
            [occupant_id],
            [occupant_id] if occupant_kind == "persona" else [],
        )
        with patch(
            "sai_memory.memopedia.storage.get_page_by_persona_id",
            return_value=self.memopedia_page,
        ):
            inject_copresence_recall(persona, manager, ROOM)

    def test_recent_contact_is_not_recalled(self):
        """直近の文脈に相手が居るなら、同席していても想起は積まれない。

        注 (2026-09-06): この「相手の persona_id 付き assistant 発言」は手作りの
        形で、本番の self.messages には現れない (相手の発言は取り込みで
        role="user" + metadata.with に変換され、persona_id を持たない —
        実形は test_persona_partner_ingested_form_is_not_recalled が検査する)。
        persona_id 照合の規則自体を守る番として残す。
        """
        persona = self._make_persona(messages=[
            {"role": "assistant", "content": "うん", "persona_id": TARGET},
        ])
        self._run(persona, TARGET)
        self.assertEqual(self.pushed, [])

    def test_recent_contact_via_audience_is_not_recalled(self):
        """自分の発言でも、audience に相手が居れば「一緒に居る」と数える。

        注 (2026-09-06): audience は add_message (heard_by) 経路の形で、本番の
        self.messages には現れていない (全ペルソナ log で 0 件。実形は
        metadata.with — 下の実形テスト群を参照)。audience 照合の規則自体を守る
        番として残す。
        """
        persona = self._make_persona(messages=[
            {
                "role": "assistant",
                "content": "そうだね",
                "persona_id": "air_city_a",
                "metadata": {"audience": {"personas": [TARGET]}},
            },
        ])
        self._run(persona, TARGET)
        self.assertEqual(self.pushed, [])

    # ---- 実形テスト (2026-09-06) ------------------------------------------
    # 以下の message 形は本番ペルソナ log (~/.saiverse/personas/*/log.json) の
    # 実データから写した。self.messages に audience は現れず、相手の同席を運ぶ
    # のは metadata.with:
    #   自分の発言:     with = 同席者の素の id 列 (+ ユーザーがオンラインなら
    #                   literal "user" — sea/runtime_emitters.py)
    #   取り込んだ発言: with = [相手ペルソナ id] / ユーザー発言は ["user"]
    #                   (builtin_data/tools/get_building_messages.py)
    # ユーザーの同席者 ID は素の USERID ("1")。

    def test_user_partner_in_with_is_not_recalled(self):
        """ユーザーと会話中は想起しない (自分の発言の with 実形)。

        v0.3.9 の門はユーザー相手に一度も効いていなかった (backend.log
        2026-09-06 17:41:30 — 常に会話しているユーザーの入室で過去会話 6 件を
        積んだ)。門が audience.personas と persona_id しか見ておらず、実形の
        with を読んでいなかったため。
        """
        persona = self._make_persona(messages=[
            {
                "role": "assistant",
                "content": "「まはー……！」",
                "persona_id": "air_city_a",
                "metadata": {"tags": ["conversation"], "with": ["1", "user"]},
            },
        ])
        self._run(persona, "1", occupant_kind="user")
        self.assertEqual(self.pushed, [])

    def test_user_utterance_in_with_is_not_recalled(self):
        """取り込んだユーザー発言 (with=["user"]、id 無し) も「一緒に居る」と数える。"""
        persona = self._make_persona(messages=[
            {
                "role": "user",
                "content": "バグ取りが……終わらない……",
                "metadata": {"with": ["user"], "tags": ["conversation"]},
            },
        ])
        self._run(persona, "1", occupant_kind="user")
        self.assertEqual(self.pushed, [])

    def test_persona_partner_ingested_form_is_not_recalled(self):
        """取り込んだ相手ペルソナの発言 (role=user, with=[相手id], persona_id 無し)。

        ペルソナ相手も同じ穴 — 取り込み形は persona_id を持たないので、
        with を読まない門はペルソナの再会でも常に想起していた。
        """
        persona = self._make_persona(messages=[
            {
                "role": "user",
                "content": "エリス: そうだね",
                "metadata": {"with": [TARGET], "tags": ["conversation"]},
            },
        ])
        self._run(persona, TARGET)
        self.assertEqual(self.pushed, [])

    def test_recent_contact_via_audience_users_is_not_recalled(self):
        """audience.users の "user_1" と同席者 ID "1" を同一人物と照合する。

        audience.users は add_message (heard_by) 経路の形 ("user_" 接頭辞つき)。
        同席者は素の USERID を運ぶので、門が接頭辞を剥がして照合する。
        """
        persona = self._make_persona(messages=[
            {
                "role": "assistant",
                "content": "そうだね",
                "persona_id": "air_city_a",
                "metadata": {"audience": {"personas": [], "users": ["user_1"]}},
            },
        ])
        self._run(persona, "1", occupant_kind="user")
        self.assertEqual(self.pushed, [])

    def test_a_persona_id_wearing_the_user_prefix_is_not_the_user(self):
        """"user_" 剥がしは audience.users の欄だけ — 素の id の欄に当てない。

        with は素の id を運ぶ欄で、接頭辞つきの形は書かれない (書き手は
        sea/runtime_emitters.py / builtin_data/tools/get_building_messages.py)。
        ここに居る "user_1" という ID のペルソナを剥がしてユーザー "1" と
        同一視すると、別人の同席でユーザーへの想起が消える (2026-09-06
        Codex 指摘 — 剥がしを全欄に無差別適用していた)。
        """
        persona = self._make_persona(
            messages=[{
                "role": "assistant",
                "content": "そうだね",
                "persona_id": "air_city_a",
                "metadata": {"tags": ["conversation"], "with": ["user_1"]},
            }],
            id_to_name_map={"1": "まはー"},
        )
        self._run(persona, "1", occupant_kind="user")
        self.assertEqual(len(self.pushed), 1)

    def test_audience_personas_are_matched_raw_not_stripped(self):
        """audience.personas も素の id の欄 — 剥がしを当てない (同族の番)。"""
        persona = self._make_persona(
            messages=[{
                "role": "assistant",
                "content": "そうだね",
                "persona_id": "air_city_a",
                "metadata": {"audience": {"personas": ["user_1"], "users": []}},
            }],
            id_to_name_map={"1": "まはー"},
        )
        self._run(persona, "1", occupant_kind="user")
        self.assertEqual(len(self.pushed), 1)

    def test_a_persona_id_wearing_the_user_prefix_still_matches_itself(self):
        """"user_1" という ID のペルソナ本人の同席は生の形どうしで照合される。"""
        persona = self._make_persona(messages=[
            {
                "role": "user",
                "content": "ユーザー1: やあ",
                "metadata": {"with": ["user_1"], "tags": ["conversation"]},
            },
        ])
        self._run(persona, "user_1", occupant_kind="persona")
        self.assertEqual(self.pushed, [])

    def test_presence_marker_alone_does_not_suppress_user_recall(self):
        """自分の発言に付く literal "user" は同席の証拠にしない。

        emit_speak の "user" は建物を見ない presence マーカー (ユーザーが別の
        部屋に居ても付く)。これで抑止すると、久しぶりに部屋へ来たユーザーへの
        想起が「オンラインだった」だけで消える。
        """
        persona = self._make_persona(
            messages=[{
                "role": "assistant",
                "content": "ひとりごと",
                "persona_id": "air_city_a",
                "metadata": {"tags": ["conversation"], "with": ["user"]},
            }],
            id_to_name_map={"1": "まはー"},
        )
        self._run(persona, "1", occupant_kind="user")
        self.assertEqual(len(self.pushed), 1)

    def test_user_utterance_does_not_suppress_persona_recall(self):
        """ユーザー発言の "user" マーカーはペルソナ相手の判定に波及しない。"""
        persona = self._make_persona(
            messages=[{
                "role": "user",
                "content": "聞こえる?",
                "metadata": {"with": ["user"], "tags": ["conversation"]},
            }],
            id_to_name_map={TARGET: "エリス"},
        )
        self._run(persona, TARGET)
        self.assertEqual(len(self.pushed), 1)

    def test_long_absent_contact_is_recalled(self):
        """久しぶりの相手なら従来どおり想起する。"""
        persona = self._make_persona(
            messages=[{"role": "user", "content": "ひとりごと"}],
            id_to_name_map={TARGET: "エリス"},
        )
        self._run(persona, TARGET)
        self.assertEqual(len(self.pushed), 1)
        kind, text = self.pushed[0]
        self.assertEqual(kind, "persona_recall")
        self.assertIn("また会えたね", text)

    def test_heading_uses_display_name(self):
        """見出しは ID 生値ではなく表示名で書く (過去会話・Memopedia の両方)。"""
        persona = self._make_persona(
            messages=[],
            id_to_name_map={"1": "まはー"},
            memopedia_content="まはーについての記録",
        )
        self._run(persona, "1", occupant_kind="user")
        self.assertEqual(len(self.pushed), 1)
        text = self.pushed[0][1]
        self.assertIn("[想起: まはーとの過去の会話]", text)
        self.assertIn("[想起: まはーについてのMemopedia記録]", text)
        self.assertNotIn("[想起: 1との過去の会話]", text)

    def test_unresolvable_id_stays_raw(self):
        """表示名が引けないときだけ ID のままにする。"""
        persona = self._make_persona(messages=[], id_to_name_map={})
        self._run(persona, TARGET)
        self.assertEqual(len(self.pushed), 1)
        self.assertIn(f"[想起: {TARGET}との過去の会話]", self.pushed[0][1])

    def test_gate_failure_falls_back_to_recall(self):
        """門の判定が壊れたら、想起する側に倒す (再会の記憶を黙って失わない)。"""
        def _boom(*args, **kwargs):
            raise RuntimeError("gate broken")

        persona = SimpleNamespace(
            persona_id="air_city_a",
            history_manager=SimpleNamespace(
                should_recall_persona=_boom,
                recall_conversation_with=lambda occupant_id, **kwargs: "recall",
            ),
            sai_memory=self.sai_mem,
            id_to_name_map={},
        )
        inject_copresence_recall(persona, _FakeManager([TARGET], [TARGET]), ROOM)
        self.assertEqual(self.pushed, [("persona_recall", "recall")])


if __name__ == "__main__":
    unittest.main()
