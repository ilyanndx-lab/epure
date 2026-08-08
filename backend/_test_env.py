"""Isolation des données de runtime pendant les tests. **À IMPORTER EN PREMIER.**

Pourquoi ce fichier existe : la suite écrivait dans les données réelles de
l'utilisateur. Neuf modules construisaient leur chemin en
``Path(__file__).parent.parent / "memory" / …``, donc importer ``main`` suffisait
à toucher ``backend/memory/``. Le cas s'est produit pour de bon — la migration de
``modules_activés`` s'est exécutée sur la configuration de l'utilisateur au
premier passage de la suite, parce que ``main.py`` la lance à l'import et que
plusieurs tests montent l'app via ``TestClient``.

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

#: Le vrai dossier de données, celui qu'aucun test ne doit toucher.
REAL_DATA_DIR = Path(__file__).resolve().parent / "memory"


def _instantaner(dossier: Path) -> dict[str, tuple[int, float]]:
    """Empreinte (taille, mtime) par fichier — sert de témoin d'écriture."""
    if not dossier.is_dir():
        return {}
    out: dict[str, tuple[int, float]] = {}
    for p in sorted(dossier.rglob("*")):
        if p.is_file():
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


#: Empreinte du VRAI dossier, prise avant tout import de core.* — c'est le
#: témoin comparé par test_data_dir.RealDataUntouchedTest.
REAL_SNAPSHOT = _instantaner(REAL_DATA_DIR)

#: Dossier temporaire où toute la suite écrit.
DATA_DIR = _installer()
