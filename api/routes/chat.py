from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_manager
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter()

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatHistoryResponse(BaseModel):
    history: List[ChatMessage]

@router.get("/history", response_model=ChatHistoryResponse)
def get_chat_history(manager = Depends(get_manager)):
    # TODO: Implement actual logic to fetch history from manager/histories
    # For now, return dummy or access manager.building_histories
    
    # Example: getting current building history
    if not manager.user_current_building_id:
        return {"history": []}
        
    raw_history = manager.building_histories.get(manager.user_current_building_id, [])
    # Convert to schema
    # raw_history is list of dict {'role':..., 'content':...}
    return {"history": raw_history}

class SendMessageRequest(BaseModel):
    message: str
    model: Optional[str] = None

@router.post("/send")
def send_message(req: SendMessageRequest, manager = Depends(get_manager)):
    # TODO: Implement message sending logic invoking manager.process_user_input
    # This might require some refactoring of how UI calls manager
    pass
