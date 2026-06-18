from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi import status

router = APIRouter()

@router.get("/dinosaure/game")
async def get_game():
    return JSONResponse(
        content={"message": "Le jeu est en cours..."},
        status_code=status.HTTP_200_OK,
    )

@router.get("/dinosaure/reset")
async def reset_game():
    return JSONResponse(
        content={"message": "Le jeu a été réinitialisé..."},
        status_code=status.HTTP_200_OK,
    )
