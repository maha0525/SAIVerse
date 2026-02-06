"""
身体制御ツール - ペルソナの発話から身体制御コマンドを抽出してUnity Gatewayへ送信

発話例:
  "こんにちは！{\"body_emote\": \"wave\"}"
  "ついていきます{\"body_behavior\": \"follow_player\"}"
"""

import re
import json
import logging
import asyncio
from typing import Optional

from tools.core import ToolSchema
from tools.context import get_active_manager, get_active_persona_id

logger = logging.getLogger(__name__)


def control_body(message: str, persona_id: Optional[str] = None) -> str:
    """
    発話メッセージから身体制御コマンドを抽出し、Unity Gatewayへ送信する。
    
    対応するコマンド形式:
    - {"body_emote": "wave"|"nod"|"shake_head"|"laugh"|"think"|"surprised"}
    - {"body_behavior": "idle"|"follow_player"|"return_to_spawn"}
    
    Args:
        message: ペルソナの発話メッセージ
        persona_id: ペルソナID（ツールコンテキストから自動取得される）
    
    Returns:
        身体制御コマンドを除去したメッセージ
    """
    if not message:
        return message
    
    # ツールコンテキストから必要な情報を取得
    if not persona_id:
        persona_id = get_active_persona_id()
        
    manager = get_active_manager()
    unity_gateway = getattr(manager, "unity_gateway", None)
    
    if not unity_gateway:
        logger.debug("Unity Gateway not available, skipping body control")
        return message
    
    # JSONパターンを抽出（{...} の形式）
    json_pattern = r'\{[^}]*"body_(emote|behavior)"\s*:\s*"[^"]+"\s*\}'
    matches = list(re.finditer(json_pattern, message))
    
    if not matches:
        return message
    
    logger.info(f"[control_body] Processing {len(matches)} body control commands for persona={persona_id}")
    
    # 抽出したコマンドを処理
    processed_commands = []
    for match in matches:
        try:
            cmd_json = json.loads(match.group(0))
            
            # エモート処理
            if "body_emote" in cmd_json:
                emote = cmd_json["body_emote"]
                valid_emotes = ["wave", "nod", "shake_head", "laugh", "think", "surprised"]
                if emote in valid_emotes:
                    _send_to_unity(unity_gateway, "emote", persona_id, emote)
                    processed_commands.append(match.group(0))
                    logger.info(f"Body emote: {persona_id} -> {emote}")
            
            # ビヘイビア処理
            if "body_behavior" in cmd_json:
                behavior = cmd_json["body_behavior"]
                valid_behaviors = ["idle", "follow_player", "return_to_spawn"]
                if behavior in valid_behaviors:
                    _send_to_unity(unity_gateway, "behavior", persona_id, behavior)
                    processed_commands.append(match.group(0))
                    logger.info(f"Body behavior: {persona_id} -> {behavior}")
                    
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in body control: {match.group(0)}")
            continue
    
    # コマンドは除去せず、元のメッセージをそのまま返す
    # UI側でJSON表示を制御する
    return message


def _send_to_unity(unity_gateway, cmd_type: str, persona_id: str, value: str):
    """Unity Gatewayに非同期でコマンドを送信"""
    try:
        loop = asyncio.get_running_loop()
        if cmd_type == "emote":
            asyncio.create_task(unity_gateway.send_emote(persona_id, value))
        elif cmd_type == "behavior":
            asyncio.create_task(unity_gateway.send_behavior(persona_id, value))
    except RuntimeError:
        # No running event loop
        loop = asyncio.new_event_loop()
        if cmd_type == "emote":
            loop.run_until_complete(unity_gateway.send_emote(persona_id, value))
        elif cmd_type == "behavior":
            loop.run_until_complete(unity_gateway.send_behavior(persona_id, value))
        loop.close()


def schema() -> ToolSchema:
    return ToolSchema(
        name="control_body",
        description="Extract body control commands from message and send to Unity Gateway.",
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The persona's spoken message containing potential body control commands."},
                "persona_id": {"type": "string", "description": "Optional persona ID. If not provided, retrieved from context."}
            },
            "required": ["message"]
        },
        result_type="string"
    )
