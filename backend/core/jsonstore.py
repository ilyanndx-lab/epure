"""Lecture/écriture des JSON de runtime (memory/, history/…), partagée par les moteurs.

Un seul point pour la règle qui a mordu (BOM dans memory_sessions.json) :
LECTURE EN utf-8-sig — un fichier écrit/édité par un outil Windows (PowerShell
5.1, éditeur) peut porter un BOM que json.loads en utf-8 strict refuse ; chaque
moteur avalait l'erreur dans son helper local → données silencieusement
invisibles, puis ÉCRASEMENT du fichier à l'écriture suivante (le moteur
repartait de son défaut). ÉCRITURE toujours en utf-8 SANS BOM.

Remplace les helpers _read/_load/_write dupliqués de memory, flashcards,
history, admin, consolidation, orchestrator, instance, module_registry,
quota_tracker — qui avaient tous le même défaut latent.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def read_json(path: Path | str, default: Any) -> Any:
    """JSON du fichier, ou ``default`` si absent (silencieux) ou illisible (logué).

    ``default`` est renvoyé TEL QUEL (pas copié) : passer un littéral au site
    d'appel (``read_json(p, {"decks": []})``), jamais une constante partagée
    mutable.
    """
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        logger.exception("Erreur lecture %s", p)
        return default


def write_json(path: Path | str, data: Any) -> None:
    """Écrit ``data`` en JSON (utf-8 sans BOM, indenté), dossiers créés au besoin.

    Ne masque pas les erreurs : les appelants qui veulent une écriture
    best-effort gardent leur try/except (et leur message contextualisé).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
