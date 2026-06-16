"""
Routeur du module « minecraft ».
Préfixé par /minecraft (cf. manifest.json).
"""

import random
from fastapi import APIRouter
from pydantic import BaseModel

from core.runtime import llm

router = APIRouter()

# ---------- modèles de données ----------
class WorldRequest(BaseModel):
    seed: int
    width: int = 32
    height: int = 24

class CraftRequest(BaseModel):
    question: str

# ---------- état global simple (monde unique en mémoire) ----------
world_state: list[list[int]] = []   # 0=air, 1=pierre, 2=terre, 3=herbe, 4=bois
current_seed: int = 0

def _generate_world(seed: int, width: int, height: int) -> list[list[int]]:
    """Génération procédurale très basique à partir d'une seed."""
    rng = random.Random(seed)
    world = [[0 for _ in range(width)] for _ in range(height)]
    # couche de pierre sur tout le fond
    for x in range(width):
        world[height-1][x] = 1
        if height > 1:
            world[height-2][x] = 1
    # couche de terre
    for x in range(width):
        world[height-3][x] = 2
    # herbe en surface
    for x in range(width):
        world[height-4][x] = 3
    # arbres (bois) et variations
    for x in range(width):
        if rng.random() < 0.1:
            world[height-5][x] = 4
            if height-6 >= 0:
                world[height-6][x] = 4
    # petites cavernes / trous aléatoires
    for _ in range(width * height // 20):
        cx = rng.randint(0, width-1)
        cy = rng.randint(0, height-1)
        if world[cy][cx] != 0:
            world[cy][cx] = 0
    return world

# ---------- routes ----------
@router.post("/world")
async def create_world(req: WorldRequest):
    global world_state, current_seed
    current_seed = req.seed
    world_state = _generate_world(req.seed, req.width, req.height)
    return {"seed": current_seed, "world": world_state}

@router.get("/world")
async def get_world():
    return {"seed": current_seed, "world": world_state}

@router.post("/craft")
async def craft_advice(req: CraftRequest):
    """Utilise le LLM pour répondre à une question de crafting."""
    messages = [
        {"role": "system", "content": "Tu es un assistant Minecraft. Réponds de façon concise et utile sur le crafting, les blocs, les recettes, etc."},
        {"role": "user", "content": req.question}
    ]
    # model=None → utilise le modèle actif de l’instance Épure
    answer = "".join(t for t in llm.stream(messages, model=None) if isinstance(t, str))
    return {"question": req.question, "answer": answer}
