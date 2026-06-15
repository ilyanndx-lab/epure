from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi import status

router = APIRouter()

@app.get("/game")
async def get_game():
    return JSONResponse(
        content={"message": "Le jeu est en cours..."},
        status=status.HTTP_200_OK
    )

@app.get("/reset")
async def reset_game():
    return JSONResponse(
        content={"message": "Le jeu a été réinitialisé..."},
        status=status.HTTP_200_OK
    )
