"""Le code d'un module s'importe depuis ``resolve_modules_dir()`` — correctif du 2026-09-25.

L'incident : ``core/module_registry.register_routers`` lit les MANIFESTES dans
``resolve_modules_dir()``, qui honore ``$EPURE_MODULES_DIR``, mais importe le
CODE par ``importlib.import_module("modules.<id>.router")``. ``modules`` est un
paquet d'espace de noms, résolu par ``sys.path`` — donc dans le ``backend/`` que
le lanceur y a placé, quel que soit ``$EPURE_MODULES_DIR``. Variable posée, le
registre voyait les modules d'un arbre et en importait le code d'un autre, sans
rien signaler.

Le paquet distribué n'est pas touché : il ne pose pas la variable, les deux
arbres coïncident. Le risque réel était un FAUX VERT en intégration — un script
qui pointe ``EPURE_MODULES_DIR`` sur une copie pour l'éprouver éprouvait en fait
le dépôt. La suite ne pouvait pas le voir : ``_test_env`` rebranche lui-même
``modules.__path__`` sur l'arbre temporaire, ce que la production ne faisait pas.

D'où un SOUS-PROCESS, qui n'importe pas ``_test_env`` : c'est la seule façon de
rejouer le démarrage de production. Il reproduit la démonstration qui a établi
le bug — dossier courant hors de ``backend/``, ``backend/`` en tête de
``sys.path`` comme le fait ``demarrer.py``, ``EPURE_MODULES_DIR`` sur une copie
de ``hello`` marquée — puis appelle le vrai ``register_routers``.

Usage :
    python test_modules_hors_depot.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401,E402  — isole EPURE_DATA_DIR AVANT tout import de core.*

_BACKEND = Path(__file__).resolve().parent

#: Exécuté dans le sous-process. Aucun ``_test_env`` : c'est le point.
_SONDE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from fastapi import FastAPI
from core.module_registry import register_routers
from core.paths import resolve_modules_dir
app = FastAPI()
register_routers(app)
mod = sys.modules.get("modules.hello.router")
print(json.dumps({
    "manifestes": str(resolve_modules_dir()),
    "router": getattr(mod, "__file__", None),
    "marque": getattr(mod, "MARQUE_COPIE", None),
    "echecs": app.state.modules_en_echec,
}))
"""


class ModulesHorsDepotTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="epure-modules-copie-")
        self.addCleanup(tmp.cleanup)
        self.racine = Path(tmp.name)
        self.copie = self.racine / "modules"
        shutil.copytree(_BACKEND / "modules" / "hello", self.copie / "hello",
                        ignore=shutil.ignore_patterns("__pycache__"))
        routeur = self.copie / "hello" / "router.py"
        routeur.write_text(routeur.read_text(encoding="utf-8")
                           + '\nMARQUE_COPIE = "copie"\n', encoding="utf-8")
        (self.racine / "donnees").mkdir()
        (self.racine / "ailleurs").mkdir()

    def test_router_importe_de_l_arbre_des_manifestes(self):
        env = dict(os.environ)
        env.update({
            "EPURE_MODULES_DIR": str(self.copie),
            "EPURE_DATA_DIR": str(self.racine / "donnees"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        sortie = subprocess.run(
            [sys.executable, "-c", _SONDE, str(_BACKEND)],
            cwd=self.racine / "ailleurs", env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        self.assertEqual(sortie.returncode, 0, sortie.stderr)
        vu = json.loads(sortie.stdout.strip().splitlines()[-1])
        self.assertEqual(Path(vu["manifestes"]), self.copie.resolve())
        self.assertEqual(vu["echecs"], {})
        self.assertIsNotNone(vu["router"], "modules.hello.router n'a pas été importé")
        self.assertTrue(
            Path(vu["router"]).resolve().is_relative_to(self.copie.resolve()),
            f"router importé de {vu['router']}, manifestes lus dans {vu['manifestes']}",
        )
        self.assertEqual(vu["marque"], "copie")


if __name__ == "__main__":
    unittest.main(verbosity=2)
