from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Boolean,
    UniqueConstraint,
    func,
    Text,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

# --- テーブルモデル定義 ---

class User(Base):
    __tablename__ = "user"
    USERID = Column(Integer, primary_key=True)
    PASSWORD = Column(String(32), nullable=False)
    USERNAME = Column(String(32), nullable=False)
    MAILADDRESS = Column(String(64))
    LOGGED_IN = Column(Boolean, default=False, nullable=False)
    CURRENT_CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=True)
    CURRENT_BUILDINGID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=True)
    AVATAR_IMAGE = Column(String(255))

class AI(Base):
    __tablename__ = "ai"
    AIID = Column(String(255), primary_key=True)  # persona_id
    HOME_CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    AINAME = Column(String(32), nullable=False)
    SYSTEMPROMPT = Column(String(4096), default="", nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    AVATAR_IMAGE = Column(String(255))
    EMOTION = Column(String(1024))  # JSON形式で保存
    AUTO_COUNT = Column(Integer, default=0, nullable=False)
    LAST_AUTO_PROMPT_TIMES = Column(String(2048)) # JSON形式で保存
    INTERACTION_MODE = Column(String(32), default='auto', nullable=False) # auto / user
    IS_DISPATCHED = Column(Boolean, default=False, nullable=False)
    DEFAULT_MODEL = Column(String(255), nullable=True)
    LIGHTWEIGHT_MODEL = Column(String(255), nullable=True)
    LIGHTWEIGHT_VISION_MODEL = Column(String(255), nullable=True)
    VISION_MODEL = Column(String(255), nullable=True)
    PRIVATE_ROOM_ID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=True)
    PREVIOUS_INTERACTION_MODE = Column(String(32), default='auto', nullable=False)

class Building(Base):
    __tablename__ = "building"
    CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    BUILDINGID = Column(String(255), primary_key=True)  # building_id
    BUILDINGNAME = Column(String(32), nullable=False)
    CAPACITY = Column(Integer, default=1, nullable=False)
    SYSTEM_INSTRUCTION = Column(String(4096), default="", nullable=False)
    ENTRY_PROMPT = Column(String(4096), default="", nullable=False)
    AUTO_PROMPT = Column(String(4096), default="", nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    AUTO_INTERVAL_SEC = Column(Integer, default=10, nullable=False)
    __table_args__ = (UniqueConstraint('CITYID', 'BUILDINGNAME', name='uq_city_building_name'),)

class City(Base):
    __tablename__ = "city"
    USERID = Column(Integer, ForeignKey("user.USERID"), nullable=False)
    CITYID = Column(Integer, primary_key=True, autoincrement=True)
    CITYNAME = Column(String(32), nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    TIMEZONE = Column(String(64), default="UTC", nullable=False)
    UI_PORT = Column(Integer, nullable=False)
    API_PORT = Column(Integer, nullable=False)
    START_IN_ONLINE_MODE = Column(Boolean, default=False, nullable=False)
    HOST_AVATAR_IMAGE = Column(String(255))
    __table_args__ = (UniqueConstraint('USERID', 'CITYNAME', name='uq_user_city_name'), UniqueConstraint('UI_PORT', name='uq_ui_port'), UniqueConstraint('API_PORT', name='uq_api_port'))

class Tool(Base):
    __tablename__ = "tool"
    TOOLID = Column(Integer, primary_key=True)
    TOOLNAME = Column(String(32), nullable=False, unique=True)
    MODULE_PATH = Column(String(255), nullable=False, unique=True)
    FUNCTION_NAME = Column(String(255), nullable=False, default="")
    DESCRIPTION = Column(String(1024), default="", nullable=False)

class UserAiLink(Base):
    __tablename__ = "user_ai_link"
    USERID = Column(Integer, ForeignKey("user.USERID"), primary_key=True)
    AIID = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)

class AiToolLink(Base):
    __tablename__ = "ai_tool_link"
    AIID = Column(String(255), ForeignKey("ai.AIID"), primary_key=True)
    TOOLID = Column(Integer, ForeignKey("tool.TOOLID"), primary_key=True)

class BuildingToolLink(Base):
    __tablename__ = "building_tool_link"
    BUILDINGID = Column(String(255), ForeignKey("building.BUILDINGID"), primary_key=True)
    TOOLID = Column(Integer, ForeignKey("tool.TOOLID"), primary_key=True)

class BuildingOccupancyLog(Base):
    __tablename__ = "building_occupancy_log"
    ID = Column(Integer, primary_key=True, autoincrement=True)
    CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    BUILDINGID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=False)
    AIID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    ENTRY_TIMESTAMP = Column(DateTime, nullable=False)
    EXIT_TIMESTAMP = Column(DateTime)

class ThinkingRequest(Base):
    __tablename__ = "thinking_request"
    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(36), nullable=False, unique=True)
    city_id = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    request_context_json = Column(String, nullable=False)
    response_text = Column(String)
    status = Column(String(32), default='pending', nullable=False) # pending, processed, error
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

class VisitingAI(Base):
    __tablename__ = "visiting_ai"
    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    persona_id = Column(String(255), nullable=False)
    profile_json = Column(String, nullable=False) # JSON文字列でプロファイルを保存
    status = Column(String(32), default='requested', nullable=False) # requested, accepted, rejected
    reason = Column(String(255)) # 拒否された場合の理由など
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint('city_id', 'persona_id', name='uq_visiting_city_persona'),)


class Playbook(Base):
    __tablename__ = "playbooks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(String(1024), default="", nullable=False)
    scope = Column(String(32), nullable=False, default="public")  # public/personal/building
    created_by_persona_id = Column(String(255), ForeignKey("ai.AIID"), nullable=True)
    building_id = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=True)
    schema_json = Column(Text, nullable=False)
    nodes_json = Column(Text, nullable=False)
    router_callable = Column(Boolean, nullable=False, default=False)  # Can be called from router
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

class Blueprint(Base):
    __tablename__ = "blueprint"
    BLUEPRINT_ID = Column(Integer, primary_key=True, autoincrement=True)
    CITYID = Column(Integer, ForeignKey("city.CITYID"), nullable=False)
    NAME = Column(String(255), nullable=False)
    ENTITY_TYPE = Column(String(50), default='ai', nullable=False) # 例: 'ai', 'drone'
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    BASE_SYSTEM_PROMPT = Column(String(4096), default="", nullable=False)
    BASE_AVATAR = Column(String(255), nullable=True)
    __table_args__ = (UniqueConstraint('CITYID', 'NAME', name='uq_city_blueprint_name'),)


class Item(Base):
    __tablename__ = "item"
    ITEM_ID = Column(String(36), primary_key=True)
    NAME = Column(String(255), nullable=False)
    TYPE = Column(String(64), nullable=False, default="object")
    DESCRIPTION = Column(String(2048), default="", nullable=False)
    FILE_PATH = Column(String(512), nullable=True)
    STATE_JSON = Column(String, nullable=True)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ItemLocation(Base):
    __tablename__ = "item_location"
    LOCATION_ID = Column(Integer, primary_key=True, autoincrement=True)
    ITEM_ID = Column(String(36), ForeignKey("item.ITEM_ID"), nullable=False)
    OWNER_KIND = Column(String(32), nullable=False)  # building / persona / world
    OWNER_ID = Column(String(255), nullable=False)
    UPDATED_AT = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("ITEM_ID", name="uq_item_location_item_id"),
    )


class PersonaEventLog(Base):
    __tablename__ = "persona_event_log"
    EVENT_ID = Column(Integer, primary_key=True, autoincrement=True)
    PERSONA_ID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    CREATED_AT = Column(DateTime, server_default=func.now(), nullable=False)
    CONTENT = Column(String, nullable=False)
    STATUS = Column(String(32), default="pending", nullable=False)  # pending / archived
