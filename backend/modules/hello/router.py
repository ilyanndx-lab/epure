"""Routeur du module de démonstration « hello ».

Monté automatiquement par core.module_registry.register_routers() sous le
prefix déclaré dans manifest.json (``/hello``). Les chemins ci-dessous sont donc
relatifs : ``@router.get("/ping")`` → ``GET /hello/ping``.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/ping")
async def hello_ping():
    return {"module": "hello", "message": "pong", "ok": True}
