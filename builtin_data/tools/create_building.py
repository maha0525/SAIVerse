"""Tool for creating a new building in the city.

This tool allows personas to create new buildings with specified properties.
The interior image can be provided as a URI or generated on-the-fly.
"""
import logging
from typing import Optional

from tools.core import ToolSchema, ToolResult

logger = logging.getLogger(__name__)


def create_building(
    name: str,
    description: str,
    system_instruction: str,
    capacity: int = 10,
    interior_image_path: Optional[str] = None,
) -> str:
    """Create a new building in the current city.

    Args:
        name: The name of the building.
        description: A description of the building's purpose and atmosphere.
        system_instruction: The system prompt that defines AI behavior in this building.
        capacity: Maximum number of AI personas that can occupy the building (default: 10).
        interior_image_path: Optional path to an interior image for visual context.
            Can be a file path or saiverse:// URI.

    Returns:
        Result message indicating success or failure.
    """
    from tools.context import get_active_manager, get_active_persona_id
    from media_utils import resolve_extended_media_uri

    manager = get_active_manager()
    persona_id = get_active_persona_id()

    if not manager:
        return "エラー: マネージャーが見つかりません。"

    city_id = manager.city_id

    # Resolve interior image path if provided
    resolved_image_path = None
    if interior_image_path:
        # Check if it's a saiverse:// URI
        if interior_image_path.startswith("saiverse://"):
            building_id = None
            if persona_id:
                persona = manager.all_personas.get(persona_id)
                if persona:
                    building_id = getattr(persona, "current_building_id", None)
            resolved = resolve_extended_media_uri(interior_image_path, persona_id, building_id)
            if resolved and resolved.exists():
                resolved_image_path = str(resolved)
                logger.info(f"[create_building] Resolved interior image URI: {interior_image_path} -> {resolved_image_path}")
            else:
                logger.warning(f"[create_building] Failed to resolve interior image URI: {interior_image_path}")
        else:
            # Assume it's a direct file path
            resolved_image_path = interior_image_path

    # Create the building
    result = manager.create_building(
        name=name,
        description=description,
        capacity=capacity,
        system_instruction=system_instruction,
        city_id=city_id,
    )

    if result.startswith("Error"):
        return f"Building作成に失敗しました: {result}"

    # Extract building_id from result message if possible
    # Result format: "Created new building '...' (ID: ...) in city ..."
    building_id = None
    if "ID:" in result:
        try:
            building_id = result.split("ID:")[1].split(")")[0].strip()
        except (IndexError, AttributeError):
            pass

    # Set interior image if provided and building was created
    if resolved_image_path and building_id:
        try:
            # Update building with image path
            from database.session import SessionLocal
            from database.models import Building as BuildingModel

            session = SessionLocal()
            try:
                db_building = session.query(BuildingModel).filter(
                    BuildingModel.BUILDINGID == building_id
                ).first()
                if db_building:
                    db_building.IMAGE_PATH = resolved_image_path
                    session.commit()
                    logger.info(f"[create_building] Set interior image for {building_id}: {resolved_image_path}")
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"[create_building] Failed to set interior image: {e}")

    creator_info = f"（作成者: {persona_id}）" if persona_id else ""
    return (
        f"新しいBuilding「{name}」を作成しました。{creator_info}\n\n"
        f"説明: {description}\n"
        f"定員: {capacity}名\n"
        f"インテリア画像: {'設定済み' if resolved_image_path else '未設定'}"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="create_building",
        description=(
            "Create a new building in the current city. "
            "Buildings are spaces where personas can gather and interact. "
            "Each building has its own system instruction that defines AI behavior, "
            "a description for discovery, and optional interior image for visual context.\n\n"
            "Use this tool when you want to create a new location for specific activities, "
            "gatherings, or purposes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the building (e.g., '静寂の図書館', 'カフェ・モーメント')"
                },
                "description": {
                    "type": "string",
                    "description": (
                        "A description of the building's purpose, atmosphere, and what activities happen there. "
                        "This is shown when personas explore or discover the building."
                    )
                },
                "system_instruction": {
                    "type": "string",
                    "description": (
                        "The system prompt that defines how AI personas should behave in this building. "
                        "Include the building's atmosphere, rules, expected interactions, and any special mechanics."
                    )
                },
                "capacity": {
                    "type": "integer",
                    "description": "Maximum number of AI personas that can occupy the building simultaneously (default: 10)",
                    "default": 10
                },
                "interior_image_path": {
                    "type": "string",
                    "description": (
                        "Optional path to an interior image for visual context. "
                        "Can be a file path or saiverse:// URI (e.g., 'saiverse://image/generated_abc.png'). "
                        "If you want to generate an image, use the generate_image tool first, "
                        "then pass the resulting file path here."
                    )
                }
            },
            "required": ["name", "description", "system_instruction"],
        },
        result_type="string",
    )
