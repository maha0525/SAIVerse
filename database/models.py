from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Boolean,
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

class AI(Base):
    __tablename__ = "ai"
    AIID = Column(String(255), primary_key=True)  # persona_id
    AINAME = Column(String(32), nullable=False)
    SYSTEMPROMPT = Column(String(4096), default="", nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)
    AVATAR_IMAGE = Column(String(255))
    EMOTION = Column(String(1024))  # JSON形式で保存
    AUTO_COUNT = Column(Integer, default=0, nullable=False)
    LAST_AUTO_PROMPT_TIMES = Column(String(2048)) # JSON形式で保存
    INTERACTION_MODE = Column(String(32), default='auto', nullable=False) # auto / user

class Building(Base):
    __tablename__ = "building"
    BUILDINGID = Column(String(255), primary_key=True)  # building_id
    BUILDINGNAME = Column(String(32), nullable=False)
    CAPACITY = Column(Integer, default=1, nullable=False)
    SYSTEM_INSTRUCTION = Column(String(4096), default="", nullable=False)
    ENTRY_PROMPT = Column(String(4096), default="", nullable=False)
    AUTO_PROMPT = Column(String(4096), default="", nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)

class City(Base):
    __tablename__ = "city"
    CITYID = Column(Integer, primary_key=True)
    CITYNAME = Column(String(32), nullable=False)
    DESCRIPTION = Column(String(1024), default="", nullable=False)

class Tool(Base):
    __tablename__ = "tool"
    TOOLID = Column(Integer, primary_key=True)
    TOOLNAME = Column(String(32), nullable=False)
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

class CityBuildingLink(Base):
    __tablename__ = "city_building_link"
    CITYID = Column(Integer, ForeignKey("city.CITYID"), primary_key=True)
    BUILDINGID = Column(String(255), ForeignKey("building.BUILDINGID"), primary_key=True)

class BuildingOccupancyLog(Base):
    __tablename__ = "building_occupancy_log"
    ID = Column(Integer, primary_key=True, autoincrement=True)
    BUILDINGID = Column(String(255), ForeignKey("building.BUILDINGID"), nullable=False)
    AIID = Column(String(255), ForeignKey("ai.AIID"), nullable=False)
    ENTRY_TIMESTAMP = Column(DateTime, nullable=False)
    EXIT_TIMESTAMP = Column(DateTime)