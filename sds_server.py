import uvicorn
import time
import threading
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Dict, Any
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(title="SAIVerse Directory Service (SDS)")

# --- Data Structures ---

class CityInfo(BaseModel):
    city_id: str
    api_base_url: str = Field(..., description="e.g., http://127.0.0.1:8001")
    # 将来的にCityの説明やオーナー情報などを追加可能

class CityRegistration(BaseModel):
    city_id: str
    api_port: int

class Heartbeat(BaseModel):
    city_id: str

# 登録されたCityをインメモリで管理
# Key: city_id, Value: { "info": Dict, "last_heartbeat": timestamp }
registered_cities: Dict[str, Dict[str, Any]] = {}
lock = threading.Lock()

HEARTBEAT_TIMEOUT = 60  # 60秒ハートビートがなければタイムアウトとみなす

# --- API Endpoints ---

@app.post("/register", status_code=201)
async def register_city(city_reg: CityRegistration, request: Request):
    """Cityが起動時に自身を登録するためのエンドポイント"""
    client_host = request.client.host
    api_base_url = f"http://{client_host}:{city_reg.api_port}"
    city_info = CityInfo(city_id=city_reg.city_id, api_base_url=api_base_url)

    with lock:
        if city_reg.city_id in registered_cities:
            logging.warning(f"City '{city_reg.city_id}' is re-registering.")
        registered_cities[city_reg.city_id] = {
            "info": city_info.model_dump(),
            "last_heartbeat": time.time()
        }
        logging.info(f"Registered city: {city_info.model_dump()}")
    return {"status": "success", "message": f"City '{city_reg.city_id}' registered."}

@app.get("/cities")
async def get_all_cities():
    """現在アクティブな全Cityのリストを返す"""
    with lock:
        return {city_id: data["info"] for city_id, data in registered_cities.items()}

@app.post("/heartbeat")
async def receive_heartbeat(heartbeat: Heartbeat):
    """Cityからの生存通知を受け取る"""
    with lock:
        if heartbeat.city_id not in registered_cities:
            raise HTTPException(status_code=404, detail=f"City '{heartbeat.city_id}' not registered.")
        registered_cities[heartbeat.city_id]["last_heartbeat"] = time.time()
        logging.debug(f"Heartbeat received from '{heartbeat.city_id}'")
    return {"status": "ok"}

def cleanup_inactive_cities():
    """非アクティブなCityを定期的に削除するバックグラウンドタスク"""
    while True:
        time.sleep(HEARTBEAT_TIMEOUT)
        with lock:
            now = time.time()
            inactive_cities = [cid for cid, data in registered_cities.items() if now - data["last_heartbeat"] > HEARTBEAT_TIMEOUT]
            for city_id in inactive_cities:
                del registered_cities[city_id]
                logging.info(f"Removed inactive city '{city_id}' due to timeout.")

if __name__ == "__main__":
    threading.Thread(target=cleanup_inactive_cities, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8080)