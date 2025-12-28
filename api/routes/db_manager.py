from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from sqlalchemy import inspect, text
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import logging
from datetime import datetime

from api.deps import get_db
from database import models

LOGGER = logging.getLogger(__name__)
router = APIRouter()

import inspect as py_inspect

# Dynamically map table names to model classes
TABLE_MAP = {}
for name, obj in py_inspect.getmembers(models):
    if py_inspect.isclass(obj) and hasattr(obj, "__tablename__"):
        TABLE_MAP[obj.__tablename__] = obj

class TableInfo(BaseModel):
    name: str
    columns: List[str]
    pk_columns: List[str]

class RowData(BaseModel):
    data: Dict[str, Any]

class DeleteRequest(BaseModel):
    pks: Dict[str, Any]

@router.get("/tables", response_model=List[TableInfo])
def list_tables():
    """List all available database tables and their schemas."""
    tables = []
    for name, model in TABLE_MAP.items():
        mapper = inspect(model)
        columns = [c.key for c in mapper.columns]
        pks = [c.key for c in mapper.primary_key]
        tables.append(TableInfo(name=name, columns=columns, pk_columns=pks))
    return sorted(tables, key=lambda x: x.name)

@router.get("/tables/{table_name}")
def get_table_data(table_name: str, limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    """Get data from a specific table."""
    if table_name not in TABLE_MAP:
        raise HTTPException(status_code=404, detail="Table not found")
    
    model = TABLE_MAP[table_name]
    try:
        # Use simple query for now. filters can be added later if needed.
        query = db.query(model).offset(offset).limit(limit)
        items = query.all()
        
        # Serialize
        result = []
        for item in items:
            row = {}
            for col in inspect(model).columns:
                val = getattr(item, col.key)
                if isinstance(val, datetime):
                    val = val.isoformat()
                row[col.key] = val
            result.append(row)
            
        return result
    except Exception as e:
        LOGGER.error(f"DB Read Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/tables/{table_name}")
def upsert_row(table_name: str, row: RowData, db: Session = Depends(get_db)):
    """Insert or Update a row."""
    if table_name not in TABLE_MAP:
        raise HTTPException(status_code=404, detail="Table not found")
    
    model = TABLE_MAP[table_name]
    mapper = inspect(model)
    data = row.data
    
    try:
        # Check if PKs exist to determine update vs insert (or use merge)
        # SQLAlchemy merge acts as upsert based on PKs
        
        # Convert types if necessary (e.g. empty string to None, bools)
        # Simple boolean/datetime conversion logic might be needed here akin to legacy db_manager.py
        clean_data = {}
        for col in mapper.columns:
            if col.key in data:
                val = data[col.key]
                # Type sanitization
                if val == "":
                    val = None
                clean_data[col.key] = val
                
        instance = model(**clean_data)
        db.merge(instance)
        db.commit()
        return {"success": True, "message": "Row saved"}
    except Exception as e:
        db.rollback()
        LOGGER.error(f"DB Write Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/tables/{table_name}")
def delete_row(table_name: str, req: DeleteRequest, db: Session = Depends(get_db)):
    """Delete a row by Primary Key(s)."""
    if table_name not in TABLE_MAP:
        raise HTTPException(status_code=404, detail="Table not found")
    
    model = TABLE_MAP[table_name]
    try:
        # Build filter from PKs
        query = db.query(model)
        for pk, val in req.pks.items():
            if not hasattr(model, pk):
                continue
            query = query.filter(getattr(model, pk) == val)
            
        instance = query.first()
        if not instance:
            raise HTTPException(status_code=404, detail="Row not found")
            
        db.delete(instance)
        db.commit()
        return {"success": True, "message": "Row deleted"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        LOGGER.error(f"DB Delete Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
