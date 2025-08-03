from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Boolean,
    UniqueConstraint,
    func,
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
    __table_args__ = (UniqueConstraint('CITYID', 'BUILDINGNAME', name='uq_city_building_name'),)

class City(Base):
    __tablename__ = "city"
    USERID = Column(Integer, ForeignKey("user.USERID"), nullable=False)
    CITYID = Column(Integer, primary_key=True, autoincrement=True)
    CITYNAME = Column(String(32), nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    UI_PORT = Column(Integer, nullable=False)
    API_PORT = Column(Integer, nullable=False)
    START_IN_ONLINE_MODE = Column(Boolean, default=False, nullable=False)
    __table_args__ = (UniqueConstraint('USERID', 'CITYNAME', name='uq_user_city_name'), UniqueConstraint('UI_PORT', name='uq_ui_port'), UniqueConstraint('API_PORT', name='uq_api_port'))

class Tool(Base):
    __tablename__ = "tool"
    TOOLID = Column(Integer, primary_key=True)
    TOOLNAME = Column(String(32), nullable=False, unique=True)
    MODULE_PATH = Column(String(255), nullable=False, unique=True)
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