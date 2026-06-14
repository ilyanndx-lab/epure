from fastapi import APIRouter

router = APIRouter()


@router.get("/clicker/counter")
async def get_counter():
    return {"counter": 0}

@router.post("/clicker/increment")
async def increment_counter():
    return {"message": "Counter incremented"}
