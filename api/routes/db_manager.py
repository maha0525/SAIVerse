import inspect as py_inspect
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from api.deps import get_db
from database import models

LOGGER = logging.getLogger(__name__)
router = APIRouter()


# Dynamically map table names to model classes
TABLE_MAP = {}
for name, obj in py_inspect.getmembers(models):
    if py_inspect.isclass(obj) and hasattr(obj, "__tablename__"):
        TABLE_MAP[obj.__tablename__] = obj

# 1 リクエストで返せる行数の上限。これを超える値はエラーにする (黙って
# 切り詰めると、呼び出し側は「全部取れた」と信じたまま欠けた一覧を表示する
# ことになる)。全件が要る呼び出し側は offset をずらして読み進める。
MAX_TABLE_ROWS_PER_REQUEST = 1000


class TableInfo(BaseModel):
    name: str
    columns: list[str]
    pk_columns: list[str]

@router.get("/tables", response_model=list[TableInfo])
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
def get_table_data(
    table_name: str,
    response: Response,
    limit: int = Query(100, ge=1, le=MAX_TABLE_ROWS_PER_REQUEST),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),  # noqa: B008
):
    """Get data from a specific table.

    本体は今までどおり行の配列を返す (呼び出し側の互換のため形は変えない)。
    テーブルの総行数は応答ヘッダ `X-Total-Count` に載せる。これが無いと
    呼び出し側は「1 ページ分しか返っていないこと」に気づけず、欠けた一覧を
    全件だと信じて表示してしまう。
    """
    if table_name not in TABLE_MAP:
        raise HTTPException(status_code=404, detail="Table not found")

    model = TABLE_MAP[table_name]
    mapper = inspect(model)
    try:
        total = db.query(model).count()
        # ページ送りの順序が呼び出しごとに変わらないよう主キー順に固定する。
        # ORDER BY を付けないと LIMIT/OFFSET が指す行が不定になり、
        # ページをまたぐと同じ行が二度出たり抜けたりしうる。
        query = db.query(model)
        pk_columns = list(mapper.primary_key)
        if pk_columns:
            query = query.order_by(*pk_columns)
        items = query.offset(offset).limit(limit).all()

        # Serialize
        result = []
        for item in items:
            row = {}
            for col in mapper.columns:
                val = getattr(item, col.key)
                if isinstance(val, datetime):
                    val = val.isoformat()
                row[col.key] = val
            result.append(row)

        response.headers["X-Total-Count"] = str(total)
        return result
    except Exception as e:
        LOGGER.error(f"DB Read Error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
