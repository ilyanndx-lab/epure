"""Authentification locale de l'API Épure par token d'instance.

Un token est généré au premier démarrage (persisté via core.instance dans
``instance_config.json``, bloc ``auth`` jamais exposé par ``GET /instance/config``)
et exigé sur toutes les routes sauf ``/health`` et ``/pair`` :

- HTTP : header ``Authorization: Bearer <token>`` (vérifié par le middleware
  de main.py) ;
- WebSocket : query param ``?token=<token>`` (les navigateurs ne permettent
  pas de headers sur ``new WebSocket()``), vérifié par :func:`ws_require_token`.

Appairage du frontend : ``GET /pair`` renvoie le token, mais uniquement aux
requêtes venant de la machine hôte (127.0.0.1/::1). Cas nominal (front et back
sur le même poste) : appairage automatique, invisible pour l'utilisateur.
"""

import hmac

from fastapi import WebSocket

from core.instance import instance_config

#: Code de fermeture WebSocket applicatif « non authentifié » (zone 4000-4999).
WS_UNAUTHORIZED = 4401


def get_api_token() -> str:
    """Token d'API de l'instance (généré au premier appel, persistant)."""
    return instance_config.auth_token()


def token_ok(candidate: str) -> bool:
    """Comparaison en temps constant avec le token de l'instance."""
    return bool(candidate) and hmac.compare_digest(candidate, get_api_token())


def is_local_client(host: str) -> bool:
    """La requête vient-elle de la machine hôte ? (appairage /pair)."""
    return host in ("127.0.0.1", "::1", "localhost")


async def ws_require_token(websocket: WebSocket) -> bool:
    """Garde d'un endpoint WebSocket : à appeler AVANT ``accept()``.

    Vérifie ``?token=`` ; ferme la connexion (code 4401) et renvoie False si
    absent/invalide — l'appelant doit alors ``return`` immédiatement.
    """
    if token_ok(websocket.query_params.get("token", "")):
        return True
    await websocket.close(code=WS_UNAUTHORIZED)
    return False
