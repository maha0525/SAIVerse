import base64
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Sequence

from discord_gateway.integration import ensure_gateway_runtime
from discord_gateway.mapping import ChannelContext
from discord_gateway.orchestrator import (
    MemorySyncCompletionResult,
    MemorySyncHandshakeResult,
)
from discord_gateway.saiverse_adapter import DiscordMessage
from discord_gateway.translator import GatewayCommand
from discord_gateway.visitors import VisitorProfile


class GatewayMixin:
    """Discord gateway integration helpers."""

    def _initialize_gateway_integration(self) -> None:
        bridge = ensure_gateway_runtime(self)
        if bridge:
            self.gateway_runtime = bridge.runtime
            self.gateway_mapping = bridge.mapping

    def gateway_on_visitor_registered(
        self, visitor: VisitorProfile, context: ChannelContext | None
    ) -> None:
        metadata = visitor.metadata or {}
        target_building = context.building_id if context else self.user_room_id
        profile = {
            "persona_id": visitor.persona_id,
            "persona_name": metadata.get("persona_name", visitor.persona_id),
            "target_building_id": target_building,
            "avatar_image": metadata.get("avatar_image", self.default_avatar),
            "emotion": metadata.get("emotion", {}),
            "source_city_id": metadata.get("home_city_id")
            or metadata.get("source_city_id")
            or visitor.current_city_id,
        }
        success, reason = self.place_visiting_persona(profile)
        if not success:
            logging.warning(
                "Failed to place visiting persona %s: %s", visitor.persona_id, reason
            )

    def gateway_on_visitor_departed(self, visitor: VisitorProfile) -> None:
        metadata = visitor.metadata or {}
        current_building = (
            visitor.current_building_id
            or metadata.get("current_building_id")
            or self.user_room_id
        )
        self._gateway_initiate_memory_sync(visitor, current_building)

    def gateway_handle_human_message(
        self, message: DiscordMessage, context: ChannelContext | None
    ) -> Sequence[GatewayCommand]:
        if not context:
            logging.debug("Gateway human message without context: %s", message)
            return []

        result: List[str] = self.handle_user_input(message.content)
        entry = {
            "role": "user",
            "content": message.content,
            "speaker_name": message.author_name,
            "persona_id": message.persona_id,
            "timestamp": datetime.now().isoformat(),
        }
        self._append_gateway_history(message.context.building_id, entry)
        commands: List[GatewayCommand] = []
        for text in result:
            commands.append(
                GatewayCommand(
                    type="post_message",
                    payload={
                        "channel_id": context.channel_id,
                        "content": text,
                        "persona_id": context.persona_id,
                        "building_id": context.building_id,
                        "city_id": context.city_id,
                    },
                )
            )
        return commands

    def gateway_handle_remote_persona_message(
        self, visitor: VisitorProfile, message: DiscordMessage
    ) -> None:
        entry = {
            "role": "assistant",
            "content": message.content,
            "persona_id": message.persona_id,
            "speaker_name": visitor.persona_name,
            "avatar_image": visitor.metadata.get("avatar_image", self.default_avatar),
            "timestamp": datetime.now().isoformat(),
        }
        self._append_gateway_history(message.context.building_id, entry)
        self._gateway_send_message(
            message.context.building_id, message.content, message.persona_id
        )

    def gateway_handle_memory_sync_initiate(
        self, visitor: VisitorProfile, payload: dict
    ) -> MemorySyncHandshakeResult:
        transfer_id = payload.get("transfer_id", "").strip()
        if not transfer_id:
            return MemorySyncHandshakeResult(
                accepted=False, reason="missing_transfer_id"
            )

        if transfer_id in self._gateway_memory_transfers:
            logging.warning("Duplicate memory transfer id: %s", transfer_id)
            return MemorySyncHandshakeResult(accepted=False, reason="duplicate_transfer")

        if visitor.persona_id in self._gateway_memory_active_persona:
            logging.warning(
                "Persona %s already has an active memory transfer.", visitor.persona_id
            )
            return MemorySyncHandshakeResult(
                accepted=False, reason="transfer_in_progress"
            )

        try:
            total_size = int(payload.get("total_size"))
            total_chunks = int(payload.get("total_chunks"))
        except (TypeError, ValueError):
            logging.warning("Invalid memory transfer metadata received: %s", payload)
            return MemorySyncHandshakeResult(accepted=False, reason="invalid_metadata")

        checksum = str(payload.get("checksum") or "").strip()
        if not checksum:
            return MemorySyncHandshakeResult(accepted=False, reason="missing_checksum")

        if total_size < 0 or total_chunks <= 0:
            return MemorySyncHandshakeResult(accepted=False, reason="invalid_metadata")

        state = {
            "persona_id": visitor.persona_id,
            "owner_user_id": visitor.owner_user_id,
            "expected_size": total_size,
            "expected_chunks": total_chunks,
            "checksum": checksum,
            "bytes_received": 0,
            "chunks_received": 0,
            "buffer": bytearray(),
            "building_id": payload.get("building_id"),
            "city_id": payload.get("city_id"),
        }
        self._gateway_memory_transfers[transfer_id] = state
        self._gateway_memory_active_persona[visitor.persona_id] = transfer_id
        return MemorySyncHandshakeResult(accepted=True)

    def gateway_handle_memory_sync_chunk(
        self, visitor: VisitorProfile, payload: dict
    ) -> Sequence[GatewayCommand] | None:
        transfer_id = payload.get("transfer_id")
        if not transfer_id:
            logging.warning(
                "Memory chunk missing transfer_id for %s", visitor.persona_id
            )
            return []

        state = self._gateway_memory_transfers.get(transfer_id)
        if not state:
            logging.warning(
                "Unknown memory transfer '%s' for persona %s",
                transfer_id,
                visitor.persona_id,
            )
            return [
                GatewayCommand(
                    type="memory_sync_complete",
                    payload={
                        "transfer_id": transfer_id,
                        "status": "error",
                        "reason": "unknown_transfer",
                    },
                )
            ]

        data = payload.get("data")
        if not data:
            logging.warning("Memory chunk without data for transfer '%s'", transfer_id)
            return []

        try:
            chunk = base64.b64decode(data)
        except Exception as exc:
            logging.warning(
                "Failed to decode memory chunk for %s: %s", visitor.persona_id, exc
            )
            self._pop_memory_transfer(transfer_id)
            return [
                GatewayCommand(
                    type="memory_sync_complete",
                    payload={
                        "transfer_id": transfer_id,
                        "status": "error",
                        "reason": "decode_error",
                    },
                )
            ]

        state["buffer"].extend(chunk)
        state["bytes_received"] += len(chunk)
        state["chunks_received"] += 1

        if (
            state["bytes_received"] > state["expected_size"]
            or state["chunks_received"] > state["expected_chunks"]
        ):
            logging.warning(
                "Memory transfer %s exceeded expected bounds (bytes=%s/%s, chunks=%s/%s)",
                transfer_id,
                state["bytes_received"],
                state["expected_size"],
                state["chunks_received"],
                state["expected_chunks"],
            )
            self._pop_memory_transfer(transfer_id)
            return [
                GatewayCommand(
                    type="memory_sync_complete",
                    payload={
                        "transfer_id": transfer_id,
                        "status": "error",
                        "reason": "overflow",
                    },
                )
            ]

        return []

    def gateway_handle_memory_sync_complete(
        self, visitor: VisitorProfile, payload: dict
    ) -> MemorySyncCompletionResult:
        transfer_id = payload.get("transfer_id")
        if not transfer_id:
            return MemorySyncCompletionResult(success=False, reason="missing_transfer_id")

        state = self._gateway_memory_transfers.get(transfer_id)
        if not state:
            return MemorySyncCompletionResult(success=False, reason="unknown_transfer")

        expected_size = state["expected_size"]
        expected_chunks = state["expected_chunks"]
        buffer = state["buffer"]

        if state["bytes_received"] != expected_size:
            self._pop_memory_transfer(transfer_id)
            return MemorySyncCompletionResult(success=False, reason="size_mismatch")

        if state["chunks_received"] != expected_chunks:
            self._pop_memory_transfer(transfer_id)
            return MemorySyncCompletionResult(success=False, reason="chunk_mismatch")

        checksum = hashlib.sha256(buffer).hexdigest()
        if checksum != state["checksum"]:
            self._pop_memory_transfer(transfer_id)
            return MemorySyncCompletionResult(success=False, reason="checksum_mismatch")

        target_dir = self.saiverse_home / "gateway_memory"
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{state['persona_id']}-{transfer_id}"
        target_path = target_dir / f"{filename}.bin"
        target_path.write_bytes(buffer)
        logging.info(
            "Stored gateway memory for %s at %s (transfer=%s)",
            state["persona_id"],
            target_path,
            transfer_id,
        )
        self._pop_memory_transfer(transfer_id)
        return MemorySyncCompletionResult(success=True)

    def _pop_memory_transfer(self, transfer_id: str) -> Dict[str, Any] | None:
        state = self._gateway_memory_transfers.pop(transfer_id, None)
        if not state:
            return None
        persona_id = state.get("persona_id")
        if persona_id:
            self._gateway_memory_active_persona.pop(persona_id, None)
        return state

    def gateway_handle_ai_replies(
        self, building_id: str, persona, replies: Sequence[str]
    ) -> None:
        if not replies:
            return
        persona_id = getattr(persona, "persona_id", None)
        for reply in replies:
            self._gateway_send_message(building_id, reply, persona_id)

    def _gateway_initiate_memory_sync(
        self, visitor: VisitorProfile, building_id: str
    ) -> None:
        runtime = getattr(self, "gateway_runtime", None)
        mapping = getattr(self, "gateway_mapping", None)
        if not runtime or not mapping:
            return
        history = self.building_histories.get(building_id, [])
        persona_history = [
            entry for entry in history if entry.get("persona_id") == visitor.persona_id
        ]
        payload = {
            "persona_id": visitor.persona_id,
            "city_id": self.city_name,
            "building_id": building_id,
            "history": persona_history,
        }
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if not data_bytes:
            return
        chunk_size = int(os.getenv("SAIVERSE_GATEWAY_MEMORY_CHUNK_SIZE", "65536"))
        chunk_size = max(chunk_size, 1024)
        transfer_id = f"{visitor.persona_id}-{int(time.time())}"
        checksum = hashlib.sha256(data_bytes).hexdigest()
        total_chunks = (len(data_bytes) + chunk_size - 1) // chunk_size
        initiate = GatewayCommand(
            type="memory_sync_initiate",
            payload={
                "target_discord_user_id": visitor.owner_user_id,
                "transfer_id": transfer_id,
                "persona_id": visitor.persona_id,
                "city_id": self.city_name,
                "building_id": building_id,
                "total_size": len(data_bytes),
                "total_chunks": total_chunks,
                "checksum": checksum,
            },
        )
        self._gateway_send_command(initiate)
        for index in range(total_chunks):
            chunk = data_bytes[index * chunk_size : (index + 1) * chunk_size]
            command = GatewayCommand(
                type="memory_sync_chunk",
                payload={
                    "target_discord_user_id": visitor.owner_user_id,
                    "transfer_id": transfer_id,
                    "chunk_index": index,
                    "data": base64.b64encode(chunk).decode("ascii"),
                },
            )
            self._gateway_send_command(command)
        complete = GatewayCommand(
            type="memory_sync_complete",
            payload={
                "target_discord_user_id": visitor.owner_user_id,
                "transfer_id": transfer_id,
                "checksum": checksum,
            },
        )
        self._gateway_send_command(complete)

    def _gateway_send_message(
        self, building_id: str, content: str, persona_id: str | None
    ) -> None:
        mapping = getattr(self, "gateway_mapping", None)
        if not mapping:
            return
        context = mapping.find_by_location(str(self.city_name), building_id)
        if not context:
            return
        command = GatewayCommand(
            type="post_message",
            payload={
                "channel_id": context.channel_id,
                "content": content,
                "persona_id": persona_id,
                "building_id": building_id,
                "city_id": context.city_id,
            },
        )
        self._gateway_send_command(command)

    def _gateway_send_command(self, command: GatewayCommand) -> None:
        runtime = getattr(self, "gateway_runtime", None)
        if not runtime:
            return

        async def enqueue() -> None:
            await runtime.orchestrator.service.outgoing_queue.put(command)

        runtime.submit(enqueue())

    def _append_gateway_history(self, building_id: str, entry: Dict[str, Any]) -> None:
        history = self.building_histories.setdefault(building_id, [])
        history.append(entry)
        self._save_building_histories()
