"""Redémarrage du backend à la demande — le canal vers le tray.

Pourquoi ce module existe : les modules ne se montent plus à chaud
(``core/module_registry.py``, en-tête). Un changement de module prend effet au
redémarrage, qu'il faut donc pouvoir demander depuis l'interface. Design :
``docs/demontage-option-d.md``.

**Le canal est un fichier sentinelle dont le TRAY choisit le chemin.**
``epure_tray.py`` le pose dans ``$EPURE_SENTINELLE_REDEMARRAGE`` quand il lance
uvicorn, et le sonde toutes les 2 s. Le chemin vient du tray et non de
``resolve_data_dir()`` : le tray n'importe pas ``core``, et ``EPURE_DATA_DIR``
peut être posé dans ``backend/.env`` — le tray aurait alors surveillé un autre
fichier que celui que le backend écrit, sans que rien ne le signale. Une seule
source pour le chemin, celle qui le lit.

**La même variable dit s'il y a un tray.** Absente : backend lancé à la main
(``uvicorn main:app``, ``tools/dev-epure.ps1``), paquet distribué, Docker —
personne ne lit de sentinelle. :func:`demander` le dit (``automatique: False``,
« redémarrage manuel requis ») au lieu de promettre un redémarrage qui
n'arrivera pas, et ne lève jamais : l'UI doit toujours recevoir une réponse
exploitable.

**``BOOT_ID`` identifie CE processus.** Exposé par ``GET /health``, il permet à
l'interface de savoir que le backend est vraiment revenu — l'ancien processus
continue de répondre pendant les ~2 s de sondage du tray, donc « /health
répond » ne prouve rien, « /health répond avec un autre ``boot_id`` » si.
"""

import logging
import os
import time
import uuid
from pathlib import Path

from core import module_registry
from core.jsonstore import write_json

logger = logging.getLogger(__name__)

ENV_SENTINELLE = "EPURE_SENTINELLE_REDEMARRAGE"

#: Identifiant de ce processus backend, tiré une fois à l'import.
BOOT_ID = uuid.uuid4().hex

MESSAGE_MANUEL = (
    "Redémarrage manuel requis : Épure n'a pas été lancé par son icône de "
    "notification. Arrêtez le backend (Ctrl+C dans son terminal) puis "
    "relancez-le ; cette page se rechargera d'elle-même."
)


def sentinelle() -> Path | None:
    """Chemin de la sentinelle posé par le tray, ou None sans tray.

    Lu à chaque appel, jamais figé (cf. core.paths) : un test qui pose la
    variable après l'import doit être pris au mot.
    """
    val = os.environ.get(ENV_SENTINELLE, "").strip()
    return Path(val) if val else None


def etat(app) -> dict:
    """Écart chargé/voulu (``module_registry``), plus ce qu'il faut pour agir."""
    return {
        **module_registry.ecart_redemarrage(app),
        "automatique": sentinelle() is not None,
        "boot_id": BOOT_ID,
    }


def demander(raison: str) -> dict:
    """Écrit la sentinelle si un tray l'attend. Ne lève jamais.

    Le contenu (raison, horodatage, ``boot_id``) ne sert qu'au journal du tray :
    c'est l'EXISTENCE du fichier qui déclenche.
    """
    chemin = sentinelle()
    if chemin is None:
        return {"déclenché": False, "automatique": False, "boot_id": BOOT_ID,
                "message": MESSAGE_MANUEL}
    try:
        write_json(chemin, {
            "raison": str(raison)[:200],
            "boot_id": BOOT_ID,
            "demandé_à": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    except OSError as exc:
        logger.error("Sentinelle de redémarrage non écrite (%s) : %s", chemin, exc)
        return {"déclenché": False, "automatique": True, "boot_id": BOOT_ID,
                "message": "Le redémarrage n'a pas pu être demandé à Épure "
                           "(fichier de signal non écrit). Utilisez « Redémarrer » "
                           "dans le menu de l'icône de notification."}
    logger.info("Redémarrage du backend demandé au tray : %s", raison)
    return {"déclenché": True, "automatique": True, "boot_id": BOOT_ID}
