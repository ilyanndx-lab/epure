"""Résolution centralisée et portable des chemins de données d'Épure.

Évite les chemins Windows absolus en dur : le dossier des fiches est résolu
via la variable d'environnement ``EPURE_FICHES_DIR`` si elle est définie, sinon
via un défaut relatif au dépôt (``<racine_du_repo>/data/fiches``). Ce module est
auto-suffisant (il charge ``.env`` lui-même) afin d'être indépendant de l'ordre
d'import des autres modules du package.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# backend/core/paths.py → backend/ → racine du dépôt
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BACKEND_DIR.parent

# Charge backend/.env si présent (idempotent ; n'écrase pas les vars déjà posées).
load_dotenv(_BACKEND_DIR / ".env")


def resolve_fiches_dir() -> Path:
    """Dossier racine des fiches PDF (portable, configurable).

    Priorité : ``$EPURE_FICHES_DIR`` (``~`` accepté) puis défaut
    ``<racine_du_repo>/data/fiches``.
    """
    env = os.environ.get("EPURE_FICHES_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return _REPO_ROOT / "data" / "fiches"


def resolve_under_fiches(folder: str) -> Path:
    """Résout un dossier de config : absolu tel quel, sinon relatif à FICHES_DIR."""
    p = Path(folder).expanduser()
    return p if p.is_absolute() else (FICHES_DIR / folder)


def resolve_workspace() -> Path:
    """Répertoire de travail du codeagent (portable, configurable).

    Priorité : ``$EPURE_WORKSPACE`` (``~`` accepté) puis défaut
    ``<racine_du_repo>/workspace``. Toujours renvoyé résolu (``.resolve()``)
    pour servir de base sûre au confinement des chemins (cf. codeagent._safe_path).
    """
    env = os.environ.get("EPURE_WORKSPACE", "").strip()
    base = Path(env).expanduser() if env else (_REPO_ROOT / "workspace")
    return base.resolve()


#: Dossier racine des fiches, résolu une fois au chargement du module.
FICHES_DIR = resolve_fiches_dir()
