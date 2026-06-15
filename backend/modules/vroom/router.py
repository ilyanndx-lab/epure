from fastapi import APIRouter

router = APIRouter()

@router.get("/cars")
async def get_cars():
    """Retourne la liste des véhicules disponibles."""
    return {"cars": [
        {"id": "sport", "name": "Sport", "speed": 10, "acceleration": 8, "handling": 7, "base_price": 500},
        {"id": "muscle", "name": "Muscle", "speed": 7, "acceleration": 9, "handling": 6, "base_price": 400},
        {"id": "offroad", "name": "Offroad", "speed": 6, "acceleration": 7, "handling": 9, "base_price": 600}
    ]}

@router.get("/tracks")
async def get_tracks():
    """Retourne la liste des pistes disponibles."""
    return {"tracks": [
        {"id": "forest", "name": "Forêt", "difficulty": 5, "length": 1000},
        {"id": "mountain", "name": "Montagne", "difficulty": 7, "length": 1500},
        {"id": "city", "name": "Ville", "difficulty": 6, "length": 1200}
    ]}

@router.get("/player-progress")
async def get_player_progress():
    """Retourne la progression du joueur."""
    return {
        "coins": 100,
        "selectedCar": "sport",
        "selectedTrack": "forest",
        "bestTimes": {}
    }

@router.post("/save-progress")
async def save_progress(progress: int, coins: int, selectedCar: str, selectedTrack: str, bestTime: float | None = None):
    """Sauvegarde la progression du joueur."""
    # In a real application, this would save to a database
    print(f"Progression sauvegardée: {progress}, {coins}, {selectedCar}, {selectedTrack}, {bestTime}")
    return {"status": "success"}

@router.post("/buy-car")
async def buy_car(car_id: str):
    """Achète une voiture si le joueur a assez de pièces."""
    # In a real application, this would check the player's coins and update their inventory
    print(f"Voiture achetée: {car_id}")
    return {"status": "success"}
