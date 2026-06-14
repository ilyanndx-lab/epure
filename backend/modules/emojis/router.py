from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def get_emojis():
    emojis = [
        {"emoji": "😊", "name": "Smile"},
        {"emoji": "😄", "name": "Grin"},
        {"emoji": "😆", "name": "Laugh"},
        {"emoji": "😍", "name": "Heart Eyes"},
        {"emoji": "😘", "name": "Face Blowing a Kiss"}
    ]
    return emojis
