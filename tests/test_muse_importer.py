from __future__ import annotations

import io
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
import uuid
import zipfile
import zlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.utilities.muse_importer import MuseExport

_FIXTURE_STAMP = "01-15-2027_0000000001"
_FIXTURE_TRANSCRIPT = f"Conversation with Muse AI_{_FIXTURE_STAMP}.txt"

# 架空の Muse エクスポート。登場人物・会話はすべて作り話で実データは含まない。
# バイナリのフィクスチャをリポジトリに置く代わりに、テスト実行時に生成する。
_FIXTURE_CONVERSATION: list[tuple[str, str, str]] = [
    ("2027-01-10T21:03:11.120+09:00", "user",
     "ルナ、はじめまして。山田花子です。これからよろしくね。"),
    ("2027-01-10T21:03:40.512+09:00", "assistant",
     "はじめまして、花子さん。ルナです。こちらこそよろしくお願いします。今日はどんなことでお手伝いできますか？"),
    ("2027-01-10T21:05:02.003+09:00", "user",
     "架空の港町「汐見市」に旅行に行きたいんだけど、2泊3日のモデルコースを考えてくれる？"),
    ("2027-01-10T21:05:47.884+09:00", "assistant",
     "いいですね、汐見市。架空の町なので、私の想像でコースを組みますね。\n\n**1日目**\n- 午前：汐見駅に到着、港の市場で海鮮丼\n- 午後：灯台公園を散策\n- 夜：温泉宿でゆっくり\n\n**2日目**\n- 終日：沖の小島へフェリーで渡ってサイクリング\n\n**3日目**\n- 午前：商店街でお土産探し\n- 午後：帰路\n\n予算感も出しましょうか？"),
    ("2027-01-10T21:07:19.330+09:00", "user",
     "ありがとう。予算は6万円くらいで考えてる。あと、私はエビアレルギーだから食事はエビ抜きで。"),
    ("2027-01-10T21:07:55.201+09:00", "assistant",
     "了解です、エビアレルギーは覚えておきます。6万円なら、宿を1泊あたり1万5千円の温泉宿にして、残りを食費とフェリー代に回すのがちょうどいいと思います。市場の海鮮丼はエビ抜きの盛り合わせに変えてもらいましょう。"),
    ("2027-01-11T08:15:26.640+09:00", "user",
     "おはよう。昨日の旅行計画、ファイルにまとめておいてくれた？"),
    ("2027-01-11T08:16:01.977+09:00", "assistant",
     "おはようございます。昨夜のうちに旅程メモを作っておきました。ワークスペースの旅行フォルダに入れてあります。"),
    ("2027-01-12T22:41:33.415+09:00", "user",
     "盆栽の水やりを記録する小さなアプリを作りたい。Pythonで、記録をJSONに保存するシンプルなやつ。"),
    ("2027-01-12T22:42:20.730+09:00", "assistant",
     "いいですね。最小構成ならこんな感じです。\n\n```python\nimport json, datetime\n\ndef log_watering(plant, path=\"watering_log.json\"):\n    try:\n        data = json.load(open(path))\n    except FileNotFoundError:\n        data = []\n    data.append({\"plant\": plant, \"at\": datetime.datetime.now().isoformat()})\n    json.dump(data, open(path, \"w\"), ensure_ascii=False, indent=2)\n```\n\n植物ごとに最終水やり日を出す表示機能も付けますか？"),
    ("2027-01-12T22:44:08.156+09:00", "user",
     "うん、表示機能もお願い。あと私の盆栽は松とモミジの2つだけ。"),
    ("2027-01-12T22:44:51.602+09:00", "assistant",
     "松とモミジですね、覚えました。表示機能を足した版をワークスペースに置いておきました。実行すると「松：3日前、モミジ：昨日」のように出ます。"),
    ("2027-01-14T19:20:44.908+09:00", "user",
     "来月の目標を決めたよ。毎朝6時に起きて、盆栽の世話をしてから仕事に行く生活にする。"),
    ("2027-01-14T19:21:30.264+09:00", "assistant",
     "素敵な目標ですね。朝6時起きの朝型生活、応援します。旅行の計画、盆栽アプリ、朝の習慣。花子さんの近況はだいたい掴めました。"),
]

_FIXTURE_ITINERARY = (
    "# 汐見市 2泊3日 旅程メモ（架空）\n\n- 予算：60,000円\n"
    "- 食事：エビ抜き（アレルギー対応）\n- 宿：港の見える温泉宿\n\n"
    "## 1日目\n市場で海鮮丼、灯台公園、温泉。\n"
)
_FIXTURE_BONSAI = "# 盆栽メモ（架空）\n\n- 松：水やりは3日に1回目安\n- モミジ：毎朝の様子見\n"


def _fixture_png() -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _build_fixture_zip(path: Path) -> None:
    turns: list[Dict[str, Any]] = []
    lines = ["Conversation with Muse AI", ""]
    for i, (created_at, role, content) in enumerate(_FIXTURE_CONVERSATION):
        prefix = "user-msg" if role == "user" else "assistant-msg"
        turns.append({
            "turn_id": f"{prefix}-{uuid.UUID(int=1000 + i)}",
            "role": role,
            "content": content,
            "source": "runtime",
            "created_at": created_at,
            "channels": ["main"],
            "transcript_file": _FIXTURE_TRANSCRIPT,
            "resources": [],
        })
        name = "山田花子" if role == "user" else "ルナ"
        lines.append(f"[{created_at[:10]} {created_at[11:19]}] {name}: {content}")
        lines.append("")
    lines.insert(6, f"[[hatch_widget:widget-{uuid.UUID(int=42)}]]")

    files = [
        ("workspace/goals/shiomi-trip/files/itinerary.md", _FIXTURE_ITINERARY.encode(), "artifact"),
        ("workspace/user/files/bonsai-note.md", _FIXTURE_BONSAI.encode(), "user_upload"),
        ("workspace/user/files/shiomi-port.png", _fixture_png(), "user_upload"),
    ]
    manifest = {
        "exported_at": "2027-01-15T09:00:00.123456789+09:00",
        "file_name": f"EYI Package_{_FIXTURE_STAMP}.zip",
        "workspace_files": [
            {
                "source": source,
                "workspace_path": bundled[len("workspace/"):],
                "bundled_file_path": bundled,
                "size_bytes": len(data),
                "unresolved_reason": None,
            }
            for bundled, data, source in files
        ],
        "voice_audio": [],
        "voice_audio_inventory_unavailable": False,
        "mail": None,
        "blobs": None,
        "turns": turns,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_FIXTURE_TRANSCRIPT, "\n".join(lines))
        for bundled, data, _source in files:
            zf.writestr(bundled, data)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))


FIXTURE_ZIP = (
    Path(tempfile.mkdtemp(prefix="muse_fixture_")) / f"EYI_Package_{_FIXTURE_STAMP}.zip"
)
_build_fixture_zip(FIXTURE_ZIP)


def _read_manifest(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        with zf.open("manifest.json") as raw:
            return json.loads(raw.read().decode("utf-8"))


def _write_manifest_zip(path: Path, manifest: Dict[str, Any]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("workspace/user/files/note.md", "fixture")


class MuseImporterFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = _read_manifest(FIXTURE_ZIP)

    def test_fixture_records_all_turns(self) -> None:
        export = MuseExport(FIXTURE_ZIP)
        self.assertEqual(len(export.conversations), 1)

        record = export.conversations[0]
        self.assertEqual(len(record.messages), 14)
        self.assertEqual(sum(1 for msg in record.messages if msg.role == "user"), 7)
        self.assertEqual(sum(1 for msg in record.messages if msg.role == "assistant"), 7)
        self.assertEqual(record.message_count(), 14)
        self.assertEqual(record.title, "Conversation with Muse AI_01-15-2027_0000000001")
        self.assertEqual(record.identifier, "EYI Package_01-15-2027_0000000001")

    def test_fixture_keeps_role_timestamp_and_content(self) -> None:
        export = MuseExport(FIXTURE_ZIP)
        record = export.conversations[0]
        turns = self.manifest["turns"]

        for message, turn in zip(record.messages, turns):
            self.assertEqual(message.node_id, turn["turn_id"])
            self.assertEqual(message.role, turn["role"])
            self.assertEqual(message.content, turn["content"].strip())
            self.assertIsNotNone(message.create_time)
            expected_dt = datetime.fromisoformat(turn["created_at"]).astimezone(timezone.utc)
            self.assertEqual(message.create_time, expected_dt)
            self.assertEqual(message.create_time.timestamp(), expected_dt.timestamp())

        payloads = list(record.iter_memory_payloads(include_roles=["user", "assistant"]))
        self.assertEqual(len(payloads), 14)
        self.assertEqual([p["role"] for p in payloads], [t["role"] for t in turns])
        self.assertEqual([p["content"] for p in payloads], [t["content"] for t in turns])
        for payload, turn in zip(payloads, turns):
            self.assertTrue(payload["timestamp"].endswith("Z"))
            expected_timestamp = (
                datetime.fromisoformat(turn["created_at"])
                .astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            self.assertEqual(payload["timestamp"], expected_timestamp)
            self.assertEqual(payload["metadata"]["channels"], turn["channels"])
            self.assertEqual(payload["metadata"]["resources"], turn["resources"])
            self.assertIn("conversation", payload["metadata"]["tags"])

    def test_workspace_file_metadata_is_kept_for_listing(self) -> None:
        export = MuseExport(FIXTURE_ZIP)
        self.assertEqual(export.workspace_file_count, 3)
        self.assertEqual(
            [item.source for item in export.workspace_files],
            ["artifact", "user_upload", "user_upload"],
        )
        self.assertEqual(
            export.workspace_files[0].workspace_path,
            "goals/shiomi-trip/files/itinerary.md",
        )
        self.assertEqual(export.workspace_files[0].size_bytes, 210)

        summary = export.summaries()[0]
        self.assertEqual(summary["workspace_file_count"], 3)
        self.assertEqual(summary["message_count"], 14)
        self.assertEqual(
            summary["workspace_files"][0]["workspace_path"],
            "goals/shiomi-trip/files/itinerary.md",
        )

    def test_extracted_directory_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(FIXTURE_ZIP) as zf:
                zf.extractall(root)
            from_zip = MuseExport(FIXTURE_ZIP)
            from_dir = MuseExport(root)
            self.assertEqual(from_dir.conversations[0].identifier, from_zip.conversations[0].identifier)
            self.assertEqual(
                [m.to_memory_payload() for m in from_dir.conversations[0].messages],
                [m.to_memory_payload() for m in from_zip.conversations[0].messages],
            )


class MuseImporterRobustnessTest(unittest.TestCase):
    def test_resources_channels_unknown_keys_and_widget_tokens(self) -> None:
        content = "one\n[[hatch_widget:widget-1234]]\ntwo"
        manifest = {
            "exported_at": "2028-02-01T10:00:00.123+09:00",
            "file_name": "EYI Package_02-01-2028_0000000002.zip",
            "workspace_files": [
                {
                    "source": "artifact",
                    "workspace_path": "goals/x/files/y.md",
                    "bundled_file_path": "workspace/goals/x/files/y.md",
                    "size_bytes": 12,
                    "unresolved_reason": None,
                    "future_workspace_key": {"kept": True},
                },
                "not-a-dict",
            ],
            "voice_audio": [],
            "voice_audio_inventory_unavailable": True,
            "mail": None,
            "blobs": None,
            "turns": [
                {
                    "turn_id": "user-msg-00000000-0000-0000-0000-000000000001",
                    "role": "user",
                    "content": content,
                    "source": "runtime",
                    "created_at": "2028-02-01T10:00:00.123+09:00",
                    "channels": ["main", "future_channel"],
                    "transcript_file": "Conversation with Muse AI_02-01-2028_0000000002.txt",
                    "resources": [{"kind": "unknown", "payload": {"a": [1, 2, 3]}}],
                    "future_turn_key": {"nested": True},
                },
                {
                    "turn_id": "assistant-msg-00000000-0000-0000-0000-000000000002",
                    "role": "assistant",
                    "content": "reply",
                    "source": "runtime",
                    "created_at": "2028-02-01T10:00:05Z",
                    "channels": ["main"],
                    "transcript_file": "Conversation with Muse AI_02-01-2028_0000000002.txt",
                    "resources": [],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "muse.zip"
            _write_manifest_zip(zip_path, manifest)
            export = MuseExport(zip_path)
            record = export.conversations[0]
            self.assertEqual(len(record.messages), 2)
            self.assertEqual(record.messages[0].content, content)

            payload = record.messages[0].to_memory_payload()
            self.assertEqual(payload["metadata"]["channels"], ["main", "future_channel"])
            self.assertEqual(
                payload["metadata"]["resources"],
                [{"kind": "unknown", "payload": {"a": [1, 2, 3]}}],
            )
            self.assertEqual(payload["metadata"]["muse_extra"], {"future_turn_key": {"nested": True}})

            self.assertEqual(export.workspace_file_count, 2)
            first = export.workspace_files[0].to_dict()
            self.assertEqual(first["workspace_path"], "goals/x/files/y.md")
            self.assertEqual(first["future_workspace_key"], {"kept": True})
            self.assertEqual(export.workspace_files[1].to_dict()["raw"], "not-a-dict")
            self.assertTrue(export.voice_audio_inventory_unavailable)

    def test_empty_turns_and_missing_timestamps_do_not_raise(self) -> None:
        manifest = {
            "exported_at": "2028-02-01T10:00:00+09:00",
            "file_name": "EYI Package_02-01-2028_0000000003.zip",
            "turns": [
                {"turn_id": "u1", "role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "created_at": "not-a-date"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "muse.zip"
            _write_manifest_zip(zip_path, manifest)
            export = MuseExport(zip_path)
            record = export.conversations[0]
            self.assertEqual(len(record.messages), 2)
            self.assertIsNone(record.messages[0].create_time)
            self.assertIsNone(record.messages[1].create_time)
            self.assertEqual(record.messages[1].content, "")
            self.assertIsNotNone(record.create_time)


class MuseImporterCliTest(unittest.TestCase):
    def test_header_is_a_system_notice_with_muse_import_kind(self) -> None:
        import argparse

        import scripts.import_muse_conversations as importer
        from sai_memory.memory.storage import SYSTEM_NOTICE_KEY, is_system_notice

        written = []

        class _FakeAdapter:
            def __init__(self, persona_id: str) -> None:
                self.persona_id = persona_id

            def is_ready(self) -> bool:
                return True

            def append_persona_message(self, payload, *, thread_suffix=None):
                written.append((thread_suffix, payload))
                return "id"

            def backfill_missing_message_embeddings(self):
                return {"embedded": 0, "total_missing": 0}

            def reembed_stale_messages(self):
                return {"embedded": 0, "total": 0}

            def close(self) -> None:
                pass

        args = argparse.Namespace(
            select=None, select_all=True, preview=40, roles="user,assistant",
            thread_suffix=None, no_header=False, dry_run=False, persona="p1",
            output="json",
        )
        with patch.object(importer, "SAIMemoryAdapter", _FakeAdapter):
            with redirect_stdout(io.StringIO()):
                importer.handle_import(MuseExport(FIXTURE_ZIP), args)

        self.assertEqual(len(written), 15)
        _suffix, header = written[0]
        self.assertEqual(header["role"], "system")
        self.assertEqual(header["metadata"][SYSTEM_NOTICE_KEY], importer.MUSE_IMPORT_NOTICE_KIND)
        self.assertEqual(importer.MUSE_IMPORT_NOTICE_KIND, "muse_import")
        self.assertTrue(is_system_notice(header))
        self.assertEqual(
            header["content"],
            '[Imported Muse conversation "Conversation with Muse AI_01-15-2027_0000000001" '
            "(EYI Package_01-15-2027_0000000001) created 2027-01-10T12:03:11Z]",
        )
        for _suffix, payload in written[1:]:
            self.assertNotEqual(payload["role"], "system")
            self.assertNotIn(SYSTEM_NOTICE_KEY, payload.get("metadata") or {})


class _FakeAdapterBase:
    """append_persona_message の成否を role で制御できるフェイク。

    本物のアダプタは保存に失敗すると例外を飲んで None を返すので、
    フェイクも同じ契約 (成功時は id、失敗時は None) に合わせる。
    """

    fail_roles: set = set()
    backfill_error: Exception | None = None

    def __init__(self, persona_id: str) -> None:
        self.persona_id = persona_id

    def is_ready(self) -> bool:
        return True

    def append_persona_message(self, payload, *, thread_suffix=None):
        if payload.get("role") in self.fail_roles:
            return None
        return "id"

    def backfill_missing_message_embeddings(self):
        if self.backfill_error is not None:
            raise self.backfill_error
        return {"embedded": 0, "total_missing": 0}

    def reembed_stale_messages(self):
        return {"embedded": 0, "total": 0}

    def close(self) -> None:
        pass


def _run_import(adapter_cls):
    import argparse

    import scripts.import_muse_conversations as importer

    args = argparse.Namespace(
        select=None, select_all=True, preview=40, roles="user,assistant",
        thread_suffix=None, no_header=False, dry_run=False, persona="p1",
        output="json",
    )
    out, err = io.StringIO(), io.StringIO()
    with patch.object(importer, "SAIMemoryAdapter", adapter_cls):
        with redirect_stdout(out), redirect_stderr(err):
            rc = importer.handle_import(MuseExport(FIXTURE_ZIP), args)
    return rc, out.getvalue(), err.getvalue()


class MuseImporterFailureTest(unittest.TestCase):
    def test_all_saves_failing_is_reported_as_failed(self) -> None:
        class _AllFail(_FakeAdapterBase):
            fail_roles = {"system", "user", "assistant"}

        rc, out, _err = _run_import(_AllFail)
        self.assertEqual(rc, 1)
        results = json.loads(out)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[0]["messages_imported"], 0)
        self.assertEqual(results[0]["messages_failed"], 15)

    def test_partial_failure_is_reported_as_partial(self) -> None:
        class _UserFail(_FakeAdapterBase):
            fail_roles = {"user"}

        rc, out, _err = _run_import(_UserFail)
        self.assertEqual(rc, 1)
        results = json.loads(out)
        self.assertEqual(results[0]["status"], "partial")
        self.assertEqual(results[0]["messages_imported"], 8)
        self.assertEqual(results[0]["messages_failed"], 7)

    def test_json_output_keeps_stdout_pure(self) -> None:
        rc, out, err = _run_import(_FakeAdapterBase)
        self.assertEqual(rc, 0)
        results = json.loads(out)
        self.assertEqual(results[0]["status"], "imported")
        self.assertEqual(results[0]["messages_imported"], 15)
        self.assertNotIn("インポート完了", out)
        self.assertIn("インポート完了", err)

    def test_backfill_failure_still_returns_pure_json(self) -> None:
        class _BackfillFail(_FakeAdapterBase):
            backfill_error = RuntimeError("boom")

        rc, out, err = _run_import(_BackfillFail)
        self.assertEqual(rc, 0)
        results = json.loads(out)
        self.assertEqual(results[0]["status"], "imported")
        self.assertIn("バックフィルに失敗", err)


class MuseImporterEndToEndTest(unittest.TestCase):
    def test_cli_import_writes_fourteen_turns_plus_header(self) -> None:
        """一時 SAIMemory へ実際に CLI で取り込み、15 行・role・timestamp を確かめる。

        サンドボックスにはエンベディングモデルが無いので、ダウンロードだけを
        即失敗させる (取り込みは embedder 無しでも成立する)。
        """
        import scripts.import_muse_conversations as importer
        import sai_memory.memory.recall as recall

        manifest = _read_manifest(FIXTURE_ZIP)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {"SAIVERSE_HOME": str(home)}
            argv = [
                "--source", str(FIXTURE_ZIP),
                "--persona", "muse-test",
                "--select-all",
                "--roles", "user,assistant",
                "--output", "json",
            ]
            with patch.dict(os.environ, env):
                with patch.object(recall, "_download_model", side_effect=FileNotFoundError("offline")):
                    with redirect_stdout(io.StringIO()):
                        rc = importer.main(["import", *argv])
            self.assertEqual(rc, 0)

            db_path = home / "personas" / "muse-test" / "memory.db"
            self.assertTrue(db_path.is_file())
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT role, content, created_at, metadata FROM messages ORDER BY rowid"
                ).fetchall()

        self.assertEqual(len(rows), 15)
        self.assertEqual(rows[0][0], "system")
        header_meta = json.loads(rows[0][3])
        self.assertEqual(header_meta["__system_notice__"], importer.MUSE_IMPORT_NOTICE_KIND)

        turns = manifest["turns"]
        for (role, content, created_at, metadata), turn in zip(rows[1:], turns):
            self.assertEqual(role, turn["role"])
            self.assertEqual(content, turn["content"])
            self.assertEqual(created_at, int(datetime.fromisoformat(turn["created_at"]).timestamp()))
            meta = json.loads(metadata)
            self.assertEqual(meta["channels"], turn["channels"])
            self.assertEqual(meta["resources"], turn["resources"])


if __name__ == "__main__":
    unittest.main()
