import sqlite3
from pathlib import Path

persona = "air_city_a"   # 確認したいペルソナIDに入れ替えてね
db_path = Path.home() / ".saiverse" / "personas" / persona / "memory.db"

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print("tables:", [row[0] for row in cur.fetchall()])

cur.execute("""
    SELECT thread_id, role, substr(content, 1, 80),
            datetime(created_at, 'unixepoch')
    FROM messages
    ORDER BY created_at DESC
    LIMIT 5
""")
for row in cur.fetchall():
    print(row)

conn.close()