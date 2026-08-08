"""Catalogue de modules installables — source, installation, suppression.

`modules-catalogue/<id>/` réunit les trois fichiers d'un module côte à côte
(`manifest.json`, `router.py`, `Component.tsx`). C'est une SOURCE, pas une
instance : rien n'y est monté, rien n'y est écrit. Installer = copier vers les
deux emplacements d'exécution ; supprimer = sauvegarder puis effacer ces deux
emplacements. Le catalogue n'est jamais modifié par ces opérations.

Pourquoi un catalogue local et pas un téléchargement : exécuter du JS tiers dans
l'origine de l'app annulerait la frontière qu'on vient de poser — tout module
chargé peut lire `localStorage['epure.apiToken']`. Cf. docs/catalogue-modules.md §0.

RIEN N'EST RÉÉCRIT ICI de ce que `module_workshop` sait déjà faire : la
sauvegarde horodatée (`_backup_existing`), le confinement d'identifiant
(`_check_module_id`), la résolution du composant frontend
(`_frontend_component_path`) et le (re)montage à chaud (`_remount`) sont
importés. Une seconde implémentation de la copie divergerait de la première —
c'est exactement le mécanisme qui avait produit deux stockages d'état
divergents (cf. CLAUDE.md §3.3).
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from core import module_registry
from core.codeagent import SecurityError
from core.module_workshop import (
    _FILES,
    _backup_existing,
    _check_module_id,
    _drop_module_routes,
    _frontend_component_path,
    _remount,
    modules_dir,
)
from core.paths import REPO_ROOT, resolve_generated_dir

logger = logging.getLogger(__name__)


class CatalogueError(ValueError):
    """Opération de catalogue refusée (id inconnu, déjà installé, protégé…)."""


def catalogue_dir() -> Path:
    """Racine du catalogue. Fonction et non constante : cf. core.paths."""
    return REPO_ROOT / "modules-catalogue"


def _entree(module_id: str) -> Path:
    """Dossier catalogue d'un id, confiné. Lève SecurityError si l'id est douteux."""
    mid = _check_module_id(module_id)
    base = catalogue_dir().resolve()
    cible = (base / mid).resolve()
    # Ceinture et bretelles : _check_module_id refuse déjà tout séparateur, mais
    # la vérification de confinement ne coûte rien et documente l'invariant.
    if not cible.is_relative_to(base):
        raise SecurityError(f"Entrée de catalogue hors de {base} : {module_id!r}")
    return cible


def list_catalogue() -> list[dict]:
    """Manifestes du catalogue, chacun enrichi de ``installé: bool``.

    ``installé`` se lit sur le disque (présence de
    ``<modules>/<id>/manifest.json``), comme l'état « installé » de CLAUDE.md
    §3.3 — jamais depuis un stockage parallèle.
    """
    base = catalogue_dir()
    if not base.is_dir():
        return []
    installes = set(module_registry.installed_ids())
    out: list[dict] = []
    for sub in sorted(base.iterdir()):
        mf = sub / "manifest.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8-sig"))
        except Exception:
            logger.exception("Manifeste de catalogue illisible : %s", mf)
            continue
        data.setdefault("id", sub.name)
        data["installé"] = data["id"] in installes
        out.append(data)
    return out


def install(module_id: str, app=None) -> dict:
    """Copie les trois fichiers vers les emplacements d'exécution, puis active.

    Pas de `tsc` : les modules du catalogue sont VERSIONNÉS et vérifiés en CI par
    l'étape « Catalogue — type-check + build après installation », qui fait
    exactement cette copie avant de compiler. Les revalider ici coûterait
    plusieurs secondes pour contrôler ce qui l'a déjà été, et surtout ferait
    écrire `frontend/src/modules/_workshop_check/<id>` dans le vrai arbre — ce
    qui ferait tomber le garde-fou des données réelles pendant les tests.
    """
    mid = _check_module_id(module_id)
    src = _entree(mid)
    if not (src / "manifest.json").is_file():
        raise CatalogueError(f"Module absent du catalogue : {mid}")
    if mid in set(module_registry.installed_ids()):
        raise CatalogueError(f"Module déjà installé : {mid}")

    dest_backend = modules_dir() / mid
    dest_backend.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "router.py"):
        f = src / name
        if f.is_file():
            shutil.copy2(f, dest_backend / name)

    comp = src / "Component.tsx"
    if comp.is_file():
        # Cible d'installation : generated/<id>/ — le glob de registry.ts l'y
        # découvre comme n'importe quel module ajouté. Les imports relatifs du
        # composant sont écrits pour CETTE profondeur (cf. 352d56e).
        dest_comp = resolve_generated_dir() / mid / "Component.tsx"
        dest_comp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(comp, dest_comp)

    module_registry.set_status(mid, "active")

    monte, erreur = False, None
    if app is not None:
        try:
            _remount(app, mid)
            monte = True
        except Exception as exc:  # montage à chaud best-effort
            logger.exception("Montage à chaud du module %s échoué", mid)
            erreur = str(exc)

    logger.info("Module %s installé depuis le catalogue", mid)
    return {"ok": True, "id": mid, "monté": monte, "erreur_montage": erreur}


def uninstall(module_id: str, app=None) -> dict:
    """Sauvegarde horodatée PUIS suppression des deux dossiers, PUIS désactivation.

    L'ordre n'est pas cosmétique : la sauvegarde doit exister avant que quoi que
    ce soit soit effacé. Si elle échoue, rien n'est supprimé.

    Refuse un module ``core_module`` ou ``removable: false`` — le cœur n'est pas
    désinstallable, et ``settings`` porte les deux protections plus celle de
    ``set_status``.
    """
    mid = _check_module_id(module_id)
    manifeste = module_registry.get_module(mid)
    if manifeste is None:
        raise CatalogueError(f"Module inconnu : {mid}")
    if manifeste.get("core_module"):
        raise CatalogueError(f"Module du cœur, non supprimable : {mid}")
    if not manifeste.get("removable", False):
        raise CatalogueError(f"Module marqué non supprimable : {mid}")

    sauvegarde = _backup_existing(mid)
    if not sauvegarde:
        raise CatalogueError(f"Sauvegarde impossible, suppression annulée : {mid}")

    # Les routes d'abord : une fois les fichiers effacés, plus rien ne permet de
    # retrouver le nom de module d'un endpoint pour filtrer.
    if app is not None:
        _drop_module_routes(app, mid)
        app.openapi_schema = None

    dossier = modules_dir() / mid
    if dossier.is_dir():
        shutil.rmtree(dossier)
    comp = _frontend_component_path(mid, must_exist=True)
    if comp and comp.is_file():
        shutil.rmtree(comp.parent, ignore_errors=True)

    module_registry.set_status(mid, "disabled")

    logger.info("Module %s supprimé (sauvegarde : %s)", mid, sauvegarde)
    return {"ok": True, "id": mid, "sauvegarde": sauvegarde}


__all__ = [
    "CatalogueError",
    "catalogue_dir",
    "install",
    "list_catalogue",
    "uninstall",
    "_FILES",
]
