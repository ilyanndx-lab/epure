from fastapi import APIRouter

router = APIRouter()

@router.get("/ping")
async def pong_ping():
    return {"module": "pong", "message": "pong", "ok": True}

@router.get("/score")
async def pong_score():
    return {"score": 0}

@router.post("/score")
async def pong_update_score(score: int):
    return {"score": score}
