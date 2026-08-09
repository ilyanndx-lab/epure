"""Isolation des données de runtime : EPURE_DATA_DIR et liaison tardive.

Deux garanties, et la seconde n'a de valeur que si la première tient :

1. **`resolve_data_dir()` lit l'environnement À CHAQUE APPEL.** C'est vérifié
   par l'exécution et non par relecture du code : chaque test ici pose
   `EPURE_DATA_DIR` **après** que les modules ont été importés, puis constate où
   le fichier atterrit réellement. Un chemin figé dans une constante de module
   ferait échouer ces tests — c'est exactement l'état d'avant, où neuf modules
   calculaient `Path(__file__).parent.parent / "memory"` à l'import.

2. **Rien n'est écrit sous le vrai `backend/memory/` pendant la suite.** Ce
   contrôle-là ne vit PAS ici mais dans `test_zz_donnees_reelles.py`, dont le
   nom le place en dernier module découvert — il vivait dans ce fichier
   (découvert en 3e position sur 12) et ne voyait donc pas les écritures des
   modules suivants. C'est le garde-fou qui doit exister AVANT que
   `DELETE /settings/modules/{id}` soit écrit : un endpoint destructif qui se
   tromperait de dossier détruirait des données réelles pendant les tests.

Usage :
    python test_data_dir.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

from core import admin as core_admin  # noqa: E402
from core import consolidation as core_consolidation  # noqa: E402
from core import flashcards as core_flashcards  # noqa: E402
from core import instance as core_instance  # noqa: E402
from core import module_registry  # noqa: E402
from core import orchestrator as core_orchestrator  # noqa: E402
from core import quota_tracker as core_quota  # noqa: E402
from core.paths import resolve_data_dir  # noqa: E402


class _DossierTemporaire(unittest.TestCase):
    """Pose EPURE_DATA_DIR sur un neuf, APRÈS les imports ci-dessus."""

    def setUp(self):
        self._prev = os.environ.get("EPURE_DATA_DIR")
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-datadir-"))
        os.environ["EPURE_DATA_DIR"] = str(self.tmp)
        self.addCleanup(self._restaurer)

    def _restaurer(self):
        if self._prev is None:
            os.environ.pop("EPURE_DATA_DIR", None)
        else:
            os.environ["EPURE_DATA_DIR"] = self._prev
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class ResolutionTest(_DossierTemporaire):
    def test_lit_la_variable_a_chaque_appel(self):
        """La preuve de la liaison tardive : la variable change, le résultat suit."""
        self.assertEqual(resolve_data_dir(), self.tmp.resolve())

        autre = Path(tempfile.mkdtemp(prefix="epure-datadir2-"))
        try:
            os.environ["EPURE_DATA_DIR"] = str(autre)
            self.assertEqual(
                resolve_data_dir(), autre.resolve(),
                "resolve_data_dir() doit relire l'environnement, pas servir un cache",
            )
        finally:
            import shutil
            shutil.rmtree(autre, ignore_errors=True)

    def test_defaut_sans_variable(self):
        os.environ.pop("EPURE_DATA_DIR", None)
        attendu = (Path(core_instance.__file__).resolve().parent.parent / "memory").resolve()
        self.assertEqual(resolve_data_dir(), attendu)

    def test_toujours_resolu(self):
        self.assertTrue(resolve_data_dir().is_absolute())


class LiaisonTardiveTest(_DossierTemporaire):
    """Chaque module rebranché suit la variable posée APRÈS son import.

    Si l'un d'eux figeait son chemin à l'import, il pointerait le dossier de
    `_test_env` (ou le vrai `memory/`) et le test échouerait.
    """

    def test_neuf_points_suivent_la_variable(self):
        attendus = {
            "admin_log.json": core_admin._log_path(),
            "admin_cache.json": core_admin._cache_path(),
            "consolidation_log.json": core_consolidation._log_path(),
            "flashcards.json": core_flashcards._flashcards_path(),
            "instance_config.json": core_instance._config_file(),
            "modules_state.json": module_registry._legacy_state_file(),
            "orchestrator_presets.json": core_orchestrator._presets_file(),
            "quota_usage.json": core_quota._usage_file(),
        }
        for nom, chemin in attendus.items():
            with self.subTest(fichier=nom):
                self.assertEqual(chemin.parent, self.tmp.resolve())
                self.assertEqual(chemin.name, nom)

    def test_instance_config_ecrit_dans_le_dossier_courant(self):
        """Le cas qui a mordu : InstanceConfig figeait son chemin en défaut d'argument."""
        cfg = core_instance.InstanceConfig()
        cfg.update({"nom_affiché": "test"})
        cible = self.tmp / "instance_config.json"
        self.assertTrue(cible.is_file(), f"rien écrit dans {self.tmp}")

    def test_quota_tracker_ecrit_dans_le_dossier_courant(self):
        """Même piège : path=_USAGE_FILE en défaut d'argument."""
        tracker = core_quota.QuotaTracker()
        self.assertEqual(Path(tracker._path).parent, self.tmp.resolve())

    def test_memory_engine_ecrit_dans_le_dossier_courant(self):
        from core.memory import MemoryEngine
        moteur = MemoryEngine()
        for attr in ("_profile_path", "_sessions_path", "_context_path"):
            with self.subTest(attribut=attr):
                self.assertEqual(getattr(moteur, attr).parent, self.tmp.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
