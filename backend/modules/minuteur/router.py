"""Routeur du module minuteur.

Monté automatiquement par core.module_registry.register_routers() sous le
prefix déclaré dans manifest.json (``/minuteur``). Les chemins ci-dessous sont donc
relatifs : ``@router.get("/time")`` → ``GET /minuteur/time``.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/time")
async def get_time():
    """Retourne le temps actuel en millisecondes depuis l'époque."""
    import time
    return {"time": int(time.time() * 1000)}

@router.get("/pomodoro-settings")
async def get_pomodoro_settings():
    """Retourne les paramètres par défaut du mode Pomodoro."""
    return {
        "work_duration": 25 * 60 * 1000,  # 25 minutes en millisecondes
        "short_break": 5 * 60 * 1000,     # 5 minutes en millisecondes
        "long_break": 15 * 60 * 1000,     # 15 minutes en millisecondes
        "cycles_before_long_break": 4     # Nombre de cycles avant une pause longue
    }
