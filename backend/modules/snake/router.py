"""Routeur du module Snake — backend léger, sans état persistant.

Monté par ``core.module_registry.register_routers()``. Le manifeste déclare
``backend.prefix == ""`` : les chemins ci-dessous sont donc absolus
(``@router.get("/snake/ping")`` → ``GET /snake/ping``).

Le jeu tourne entièrement côté client (``Component.tsx``). Le backend ne sert
qu'à conserver le meilleur score de la session, en mémoire. Aucun accès disque,
réseau, subprocess, ni clé API.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()

# Meilleur score de la session, gardé en mémoire vive (remis à zéro au redémarrage).
_best_score = 0


class ScorePayload(BaseModel):
    score: int = Field(ge=0, le=100_000)


@router.get("/snake/ping")
async def snake_ping():
    return {"module": "snake", "message": "pong", "ok": True}


@router.get("/snake/highscore")
async def snake_highscore():
    """Renvoie le meilleur score connu côté serveur."""
    return {"best": _best_score}


@router.post("/snake/score")
async def snake_score(payload: ScorePayload):
    """Soumet un score ; ne conserve que le maximum de la session."""
    global _best_score
    if payload.score > _best_score:
        _best_score = payload.score
    return {"best": _best_score}
