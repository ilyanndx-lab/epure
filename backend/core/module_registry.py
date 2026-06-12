"""Registre des modules d'Épure, piloté par des manifestes.

Chaque module core déclare un ``backend/modules/<id>/manifest.json`` SANS que le
code du module y soit déplacé (les composants/endpoints restent où ils sont).
Le manifeste décrit le module (id, version, nom, icône lucide-react, description,
frontend.component, backend.prefix, core_module, origin, status, removable).

Le status effectif fusionne le manifeste avec ``memory/modules_state.json``
(overrides persistés via :func:`set_status`). Conçu pour accueillir plus tard des
modules tiers (origin ≠ "builtin", removable: true) sans changer l'interface.
"""

import json
import logging
from pathlib import Path
from threading import RLock
from typing import Optional

logger = logging.getLogger(__name__)

_MODULES_DIR = Path(__file__).parent.parent / "modules"
_STATE_FILE = Path(__file__).parent.parent / "memory" / "modules_state.json"

_VALID_STATUS = {"active", "disabled"}
_lock = RLock()


def discover_manifests() -> list[dict]:
    """Lit tous les ``modules/<id>/manifest.json`` présents sur disque."""
    manifests: list[dict] = []
    if not _MODULES_DIR.is_dir():
        return manifests
    for sub in sorted(_MODULES_DIR.iterdir()):
        mf = sub / "manifest.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            data.setdefault("id", sub.name)
            manifests.append(data)
        except Exception:
            logger.exception("Manifest illisible : %s", mf)
    return manifests


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Erreur lecture modules_state")
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_modules() -> list[dict]:
    """Manifestes enrichis du status effectif (override modules_state.json)."""
    state = _load_state()
    out: list[dict] = []
    for mf in discover_manifests():
        m = dict(mf)
        override = state.get(m["id"], {})
        if "status" in override:
            m["status"] = override["status"]
        out.append(m)
    return out


def get_module(module_id: str) -> Optional[dict]:
    return next((m for m in list_modules() if m.get("id") == module_id), None)


def set_status(module_id: str, status: str) -> Optional[dict]:
    """Change le status (active|disabled). Retourne le module à jour ou None.

    Refuse : status invalide, module inconnu, ou désactivation de ``settings``
    (qui doit rester accessible pour réactiver les autres modules).
    """
    if status not in _VALID_STATUS:
        return None
    with _lock:
        manifest = next((m for m in discover_manifests() if m.get("id") == module_id), None)
        if manifest is None:
            return None
        if module_id == "settings" and status != "active":
            return None
        state = _load_state()
        state[module_id] = {**state.get(module_id, {}), "status": status}
        _save_state(state)
    return get_module(module_id)
