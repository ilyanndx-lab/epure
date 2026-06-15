from fastapi import APIRouter
from typing import Dict, Optional

router = APIRouter()

# In‑memory player state (reset to defaults on module reload)
player_state: Dict = {
    "coins": 100,
    "selectedCar": "sport",
    "selectedTrack": "forest",
    "ownedCars": ["sport"],
    "bestTimes": {}
}

CARS = [
    {"id": "sport", "name": "Sport", "speed": 10, "acceleration": 8, "handling": 7, "base_price": 500},
    {"id": "muscle", "name": "Muscle", "speed": 7, "acceleration": 9, "handling": 6, "base_price": 400},
    {"id": "offroad", "name": "Offroad", "speed": 6, "acceleration": 7, "handling": 9, "base_price": 600},
]

TRACKS = [
    {"id": "forest", "name": "Forêt", "difficulty": 5, "length": 1000},
    {"id": "mountain", "name": "Montagne", "difficulty": 7, "length": 1500},
    {"id": "city", "name": "Ville", "difficulty": 6, "length": 1200},
]

@router.get("/cars")
async def get_cars():
    return {"cars": CARS}

@router.get("/tracks")
async def get_tracks():
    return {"tracks": TRACKS}

@router.get("/player-progress")
async def get_player_progress():
    return player_state.copy()

@router.post("/save-progress")
async def save_progress(
    progress: int,
    coins: int,
    selectedCar: str,
    selectedTrack: str,
    bestTime: float | None = None,
):
    player_state["coins"] = coins
    player_state["selectedCar"] = selectedCar
    player_state["selectedTrack"] = selectedTrack
    if bestTime is not None:
        player_state["bestTimes"][selectedTrack] = bestTime
    print(f"Progression sauvegardée : {player_state}")
    return {"status": "success"}

@router.post("/buy-car")
async def buy_car(car_id: str):
    car = next((c for c in CARS if c["id"] == car_id), None)
    if car is None:
        return {"status": "error", "message": "Voiture inconnue."}

    if car_id in player_state["ownedCars"]:
        return {"status": "error", "message": "Voiture déjà possédée."}

    if player_state["coins"] < car["base_price"]:
        return {"status": "error", "message": "Pas assez de pièces."}

    player_state["coins"] -= car["base_price"]
    player_state["ownedCars"].append(car_id)
    return {
        "status": "success",
        "ownedCars": player_state["ownedCars"],
        "coins": player_state["coins"],
    }
