import base64
import hashlib
import importlib
import os
from pathlib import Path

from discord_gateway.orchestrator import (
    MemorySyncCompletionResult,
    MemorySyncHandshakeResult,
)
from discord_gateway.visitors import VisitorProfile

os.environ.setdefault("GEMINI_FREE_API_KEY", "test-key")

SAIVerseManager = importlib.import_module("saiverse_manager").SAIVerseManager


def _create_manager(tmp_path: Path) -> SAIVerseManager:
    manager = object.__new__(SAIVerseManager)
    manager.saiverse_home = tmp_path
    manager._gateway_memory_transfers = {}
    manager._gateway_memory_active_persona = {}
    return manager  # type: ignore[return-value]


def _create_visitor() -> VisitorProfile:
    return VisitorProfile(
        discord_user_id="user-1",
        persona_id="persona-1",
        owner_user_id="owner-1",
        current_city_id="CityA",
        current_building_id="Hall",
    )


def test_memory_sync_success(tmp_path: Path):
    manager = _create_manager(tmp_path)
    visitor = _create_visitor()

    data = b"memory-bytes"
    checksum = hashlib.sha256(data).hexdigest()
    payload_initiate = {
        "transfer_id": "transfer-1",
        "total_size": len(data),
        "total_chunks": 1,
        "checksum": checksum,
        "building_id": "Hall",
        "city_id": "CityA",
    }

    result = manager.gateway_handle_memory_sync_initiate(visitor, payload_initiate)
    assert isinstance(result, MemorySyncHandshakeResult)
    assert result.accepted

    chunk_payload = {
        "transfer_id": "transfer-1",
        "data": base64.b64encode(data).decode("ascii"),
    }
    commands = manager.gateway_handle_memory_sync_chunk(visitor, chunk_payload)
    assert commands == []

    complete_result = manager.gateway_handle_memory_sync_complete(
        visitor, {"transfer_id": "transfer-1"}
    )
    assert isinstance(complete_result, MemorySyncCompletionResult)
    assert complete_result.success

    stored_path = tmp_path / "gateway_memory" / f"{visitor.persona_id}-transfer-1.bin"
    assert stored_path.read_bytes() == data
    assert manager._gateway_memory_transfers == {}
    assert manager._gateway_memory_active_persona == {}


def test_memory_sync_checksum_failure(tmp_path: Path):
    manager = _create_manager(tmp_path)
    visitor = _create_visitor()

    data = b"other-bytes"
    wrong_checksum = hashlib.sha256(b"different").hexdigest()
    payload_initiate = {
        "transfer_id": "transfer-err",
        "total_size": len(data),
        "total_chunks": 1,
        "checksum": wrong_checksum,
    }

    result = manager.gateway_handle_memory_sync_initiate(visitor, payload_initiate)
    assert result.accepted

    chunk_payload = {
        "transfer_id": "transfer-err",
        "data": base64.b64encode(data).decode("ascii"),
    }
    manager.gateway_handle_memory_sync_chunk(visitor, chunk_payload)

    complete_result = manager.gateway_handle_memory_sync_complete(
        visitor, {"transfer_id": "transfer-err"}
    )
    assert not complete_result.success
    assert complete_result.reason == "checksum_mismatch"
    assert manager._gateway_memory_transfers == {}
    assert manager._gateway_memory_active_persona == {}

    memory_dir = tmp_path / "gateway_memory"
    assert not memory_dir.exists()


def test_memory_sync_initiate_rejects_invalid_metadata(tmp_path: Path):
    manager = _create_manager(tmp_path)
    visitor = _create_visitor()

    result = manager.gateway_handle_memory_sync_initiate(
        visitor,
        {
            "transfer_id": "bad-meta",
            "total_size": "not-an-int",
            "total_chunks": 0,
            "checksum": "",
        },
    )
    assert not result.accepted
    assert result.reason in {"invalid_metadata", "missing_checksum"}
