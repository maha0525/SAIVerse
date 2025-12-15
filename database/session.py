import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pathlib import Path
from .paths import default_db_path
from .models import Base

# Determine Database URL
# Try to reuse the logic or just point to default path
db_path = default_db_path()
DATABASE_URL = f"sqlite:///{db_path}"

engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False} # Needed for SQLite with multiple threads
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
