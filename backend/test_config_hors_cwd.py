"""`config.yaml` se lit quel que soit le dossier courant — correctif du 2026-09-23.

L'incident : `LLMEngine.__init__` et `RAGEngine.__init__` prenaient
`config_path="config.yaml"` par défaut, un chemin RELATIF au dossier courant du
process. Tant qu'on lance depuis `backend/` (ce que font la CI, le tray avec
`cwd=BACKEND_DIR` et la doc), rien ne se voit. Depuis la racine du dépôt,
`unittest discover -s backend` donnait 50 erreurs `FileNotFoundError:
config.yaml` — toutes à l'import de `core/runtime.py`, qui construit
`LLMEngine()` — et n'importe quel lanceur qui ne pose pas le `cwd` aurait un
backend qui ne démarre pas.

`RAGEngine` est le cas le plus sournois : il est derrière un `_LazyEngine`
(CLAUDE.md §3.2), donc le backend démarrait, et c'est la PREMIÈRE requête RAG
qui aurait levé. La suite lancée depuis la racine ne peut pas le voir — rien
n'y construit le vrai `rag` — d'où ce test, qui construit les deux moteurs
depuis un dossier courant qui n'est PAS `backend/`.

Le défaut se résout désormais sur `core.paths.BACKEND_DIR`, l'ancre statique
des fichiers de code source (`config.yaml` en est un, versionné, pas une donnée
utilisateur — d'où `BACKEND_DIR` et non un `resolve_*()`).

Usage :
    python test_config_hors_cwd.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.*

from core.llm import LLMEngine  # noqa: E402
from core.rag import RAGEngine  # noqa: E402


class _StoreFactice:
    """Juste assez de `VectorStore` pour le constructeur de `RAGEngine`."""

    def collection(self, _nom):
        return object()


class ConfigHorsCwdTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="epure-cwd-")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)  # un dossier sans config.yaml

    def test_llm_engine_lit_la_config(self):
        moteur = LLMEngine()
        self.assertTrue(moteur._model)

    def test_rag_engine_lit_la_config(self):
        moteur = RAGEngine(store=_StoreFactice())
        self.assertGreater(moteur._chunk_size, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
