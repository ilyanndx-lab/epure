"""Routeur pour le module de jeu de course Vroom."""

from fastapi import APIRouter

router = APIRouter()

@router.get("/vroom/cars")
async def get_cars():
    """Retourne la liste des véhicules disponibles."""
    return {
        "cars": [
            {"id": "sport", "name": "Sport", "speed": 10, "acceleration": 8, "handling": 7},
            {"id": "muscle", "name": "Muscle", "speed": 7, "acceleration": 9, "handling": 6},
            {"id": "offroad", "name": "Offroad", "speed": 6, "acceleration": 7, "handling": 9}
        ]
    }

@router.get("/vroom/tracks")
async def get_tracks():
    """Retourne la liste des pistes disponibles."""
    return {
        "tracks": [
            {"id": "forest", "name": "Forêt", "difficulty": 5, "length": 1000},
            {"id": "mountain", "name": "Montagne", "difficulty": 7, "length": 1500},
            {"id": "city", "name": "Ville", "difficulty": 6, "length": 1200}
        ]
    }

@router.post("/vroom/save-progress")
async def save_progress(progress: dict):
    """Sauvegarde la progression du joueur."""
    # Ici, vous implémenteriez la logique de sauvegarde
    # avec une base de données ou un système de stockage
    return {"status": "success", "message": "Progression sauvegardée"}
