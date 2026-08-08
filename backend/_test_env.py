"""Isolation des données de runtime pendant les tests. **À IMPORTER EN PREMIER.**

Trois arborescences sont détournées vers des temporaires :

    EPURE_DATA_DIR       backend/memory/                 (JSON de runtime)
    EPURE_MODULES_DIR    backend/modules/                (copie)
    EPURE_GENERATED_DIR  frontend/src/modules/generated/ (copie)

Pourquoi ce fichier existe : la suite écrivait dans les données réelles de
l'utilisateur. Neuf modules construisaient leur chemin en
``Path(__file__).parent.parent / "memory" / …``, donc importer ``main`` suffisait
à toucher ``backend/memory/``. Le cas s'est produit pour de bon — la migration de
``modules_activés`` s'est exécutée sur la configuration de l'utilisateur au
premier passage de la suite, parce que ``main.py`` la lance à l'import et que
plusieurs tests montent l'app via ``TestClient``.

Les deux arborescences de modules ont été ajoutées **avant** d'écrire
``DELETE /settings/modules/{id}`` : cet endpoint fait un ``rmtree`` sur
``<modules>/<id>`` et ``<generated>/<id>``. Un test de suppression visant les
dossiers de production ne se rate qu'une fois.

Elles sont COPIÉES et non vides : les tests existants s'appuient sur un arbre
réaliste (``module_exists("chat")``, les manifestes du cœur, le ``hello`` de
référence). ``_backups`` est exclu de la copie — 1,2 Mo des 1,6 Mo, et ce n'est
qu'un historique de runtime.

Usage, dans chaque test qui importe ``core.*`` ou ``main`` :

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _test_env  # noqa: F401  — AVANT tout import de core.* ou main

L'ordre est la seule chose qui compte : ``core.paths.resolve_data_dir`` lit
``$EPURE_DATA_DIR`` À CHAQUE APPEL (jamais figée dans une constante de module),
mais les moteurs sont construits à l'import de ``core.runtime`` — si la variable
est posée après, le mal est déjà fait.

UN SEUL dossier pour toute la suite, et pas un par fichier : ``unittest
discover`` importe tous les modules de test dans le même process. Un dossier par
fichier ferait gagner le dernier importé, et les tests se marcheraient dessus de
façon dépendante de l'ordre de découverte.

Ce fichier ne s'appelle pas ``test_*.py`` : il ne doit pas être ramassé par la
découverte automatique.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
_REPO = _BACKEND.parent

#: Les trois arborescences réelles qu'aucun test ne doit toucher.
REAL_DATA_DIR = _BACKEND / "memory"
REAL_MODULES_DIR = _BACKEND / "modules"
REAL_FRONTEND_MODULES = _REPO / "frontend" / "src" / "modules"
REAL_DIRS = (REAL_DATA_DIR, REAL_MODULES_DIR, REAL_FRONTEND_MODULES)

#: Non copiés dans l'arbre temporaire : `_backups` pèse 1,2 Mo des 1,6 Mo de
#: modules/ et n'est qu'un historique de runtime ; `_staging` et les caches sont
#: recréés à la demande par le code testé.
_IGNORES = shutil.ignore_patterns("_backups", "_staging", "__pycache__", "*.pyc")


def _derive(chemin: Path) -> bool:
    """Artefact dérivé, pas une donnée : exclu de l'empreinte.

    Les ``__pycache__`` sont écrits par l'interpréteur lui-même dès qu'un module
    est importé — ``register_routers`` fait ``import modules.admin.router``, et
    le bytecode atterrit à côté de la source, dans le VRAI ``backend/modules/``.
    Rien ne l'empêche : ``EPURE_MODULES_DIR`` détourne la lecture des manifestes,
    pas la résolution du package ``modules`` par ``sys.path``.

    Les compter comme une écriture rendrait le garde-fou ingérable — il
    échouerait sur tout clone frais au premier import, ce qui est exactement ce
    qui s'est produit : l'arbre de travail avait déjà ses ``.pyc``, donc le
    problème ne s'y voyait pas. Un ``.pyc`` est régénérable, gitignoré, et ne
    porte aucune donnée utilisateur ; ce que le garde-fou surveille, ce sont les
    sources et les données.
    """
    return "__pycache__" in chemin.parts or chemin.suffix in (".pyc", ".pyo")


def _instantaner(dossier: Path) -> dict[str, tuple[int, float]]:
    """Empreinte (taille, mtime) par fichier — sert de témoin d'écriture."""
    if not dossier.is_dir():
        return {}
    out: dict[str, tuple[int, float]] = {}
    for p in sorted(dossier.rglob("*")):
        if p.is_file() and not _derive(p.relative_to(dossier)):
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(dossier))] = (st.st_size, st.st_mtime)
    return out


def _installer() -> Path:
    """Pose ``EPURE_DATA_DIR`` sur un temporaire, une seule fois par process.

    Respecte une valeur déjà posée : la CI ou un lanceur externe peut vouloir
    imposer son propre dossier, et deux imports de ce module ne doivent pas
    produire deux dossiers différents (le cache d'import de Python garantit
    déjà l'unicité, la vérification couvre le cas d'un ``importlib.reload``).
    """
    existant = os.environ.get("EPURE_DATA_DIR", "").strip()
    if existant:
        return Path(existant).expanduser().resolve()
    d = Path(tempfile.mkdtemp(prefix="epure-test-data-"))
    os.environ["EPURE_DATA_DIR"] = str(d)
    atexit.register(shutil.rmtree, d, True)
    return d


def _installer_arbre(var: str, source: Path, nom: str) -> Path:
    """Pose ``var`` sur une COPIE de ``source`` dans un temporaire.

    Une copie et non un dossier vide : les tests existants s'appuient sur un
    arbre réaliste (``module_exists("chat")``, ``discover_manifests`` qui doit
    trouver les manifestes du cœur, le ``hello`` de référence lu par l'Atelier).
    Un dossier vide les ferait tous tomber pour de mauvaises raisons.

    C'est aussi ce qui rend testable ``DELETE /settings/modules/{id}`` : son
    ``rmtree`` frappe la copie. Sans ça, le premier test de suppression
    détruirait un vrai module.
    """
    existant = os.environ.get(var, "").strip()
    if existant:
        return Path(existant).expanduser().resolve()
    racine = Path(tempfile.mkdtemp(prefix=f"epure-test-{nom}-"))
    atexit.register(shutil.rmtree, racine, True)
    cible = racine / source.name
    if source.is_dir():
        shutil.copytree(source, cible, ignore=_IGNORES)
    else:
        cible.mkdir(parents=True, exist_ok=True)
    os.environ[var] = str(cible)
    return cible


#: Empreintes des VRAIS dossiers, prises avant tout import de core.* — témoins
#: comparés par test_zz_donnees_reelles.
REAL_SNAPSHOT = _instantaner(REAL_DATA_DIR)
REAL_SNAPSHOTS = {str(d): _instantaner(d) for d in REAL_DIRS}

#: Dossier temporaire où toute la suite écrit ses JSON de runtime.
DATA_DIR = _installer()

#: Copie de backend/modules/ — EPURE_MODULES_DIR pointe dessus.
MODULES_DIR = _installer_arbre("EPURE_MODULES_DIR", REAL_MODULES_DIR, "modules")

#: Copie de frontend/src/modules/ — EPURE_GENERATED_DIR pointe sur son
#: sous-dossier `generated`, dont core.paths déduit le parent.
_FRONTEND_COPIE = _installer_arbre("EPURE_GENERATED_DIR", REAL_FRONTEND_MODULES, "frontend")
if _FRONTEND_COPIE.name != "generated":
    # _installer_arbre a copié `modules/` ; la variable doit désigner
    # `modules/generated`, le parent en étant déduit par core.paths.
    GENERATED_DIR = _FRONTEND_COPIE / "generated"
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["EPURE_GENERATED_DIR"] = str(GENERATED_DIR)
else:
    GENERATED_DIR = _FRONTEND_COPIE
