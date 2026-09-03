#!/usr/bin/env python3
"""Rendu inline des figures matplotlib du module Code, au lieu d'une fenêtre
externe (`_launch_gui`) — ajout du 2026-09-03.

`matplotlib` retiré de `GUI_LIBS` (cf. `core/codeagent.py`) : un script `.py`
qui l'utilise s'exécute désormais comme n'importe quel autre (subprocess
capturé), avec `MPLBACKEND=Agg` et le hook `core/_plot_support/
sitecustomize.py` posé en tête de PYTHONPATH pour sauvegarder les figures
encore ouvertes à la sortie du script.

matplotlib est une dépendance OPTIONNELLE de l'UTILISATEUR (installée via le
panneau d'installation de packages du module Code), pas une dépendance du
backend — absente de `requirements.txt`, volontairement. Les tests qui
exécutent réellement un script matplotlib sont donc SAUTÉS si l'interpréteur
utilisé par `codeagent` (`codeagent._exec_python()`) ne l'a pas, plutôt que de
l'ajouter comme dépendance obligatoire de la suite.

Usage :
    python test_codeagent_plots.py
"""

import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Permettre d'importer le package `core` depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

from core import codeagent


def _matplotlib_disponible() -> bool:
    """Vérifie sur l'interpréteur RÉELLEMENT utilisé par codeagent (primaire
    ou repli) — pas sur `sys.executable` du process de test, qui peut différer."""
    try:
        out = subprocess.run(
            [codeagent._exec_python(), "-c", "import matplotlib"],
            capture_output=True, text=True, timeout=15,
        )
        return out.returncode == 0
    except Exception:
        return False


_MPL_OK = _matplotlib_disponible()
_MPL_SKIP_RAISON = "matplotlib n'est pas installé sur l'interpréteur utilisé par le module Code"


class _WorkspaceTest(unittest.TestCase):
    """Racine de travail confinée à un temporaire — même patron que
    `test_safe_path.SafePathTest`, pour ne jamais toucher le vrai workspace."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_workspace = codeagent.WORKSPACE
        codeagent.WORKSPACE = Path(self._tmp.name).resolve()

    def tearDown(self):
        codeagent.WORKSPACE = self._orig_workspace
        self._tmp.cleanup()

    def _ecrire(self, nom: str, contenu: str) -> str:
        (codeagent.WORKSPACE / nom).write_text(contenu, encoding="utf-8")
        return nom


class GuiLibsTest(unittest.TestCase):
    """Retrait ciblé : SEULS "matplotlib"/"pyplot" sortent de GUI_LIBS."""

    def test_matplotlib_et_pyplot_retires(self):
        self.assertNotIn("matplotlib", codeagent.GUI_LIBS)
        self.assertNotIn("pyplot", codeagent.GUI_LIBS)

    def test_les_autres_libs_gui_inchangees(self):
        for lib in ("pygame", "tkinter", "turtle", "wx", "PyQt", "pyglet", "kivy"):
            self.assertIn(lib, codeagent.GUI_LIBS)
        self.assertEqual(len(codeagent.GUI_LIBS), 7, "aucune autre entrée ne doit avoir bougé")


class SitecustomizeLocationTest(unittest.TestCase):
    """Le hook n'est ni dans WORKSPACE, ni éditable depuis l'arbre utilisateur."""

    def test_fichier_existe_hors_workspace(self):
        f = codeagent._PLOT_SITECUSTOMIZE_DIR / "sitecustomize.py"
        self.assertTrue(f.is_file())
        self.assertFalse(f.is_relative_to(codeagent.WORKSPACE))


class NonRegressionGuiReelleTest(_WorkspaceTest):
    """Une vraie lib GUI interactive route TOUJOURS vers `_launch_gui` — pas
    le nouveau chemin matplotlib. Non-régression explicite (tâche §Tests)."""

    def test_tkinter_route_toujours_vers_launch_gui(self):
        nom = self._ecrire("app.py", "import tkinter\nprint('salut')\n")
        with mock.patch.object(codeagent, "_launch_gui", return_value={"external": True}) as lg:
            out = codeagent.execute_code(nom)
        lg.assert_called_once()
        self.assertEqual(out, {"external": True})

    def test_pygame_route_toujours_vers_launch_gui(self):
        nom = self._ecrire("jeu.py", "import pygame\n")
        with mock.patch.object(codeagent, "_launch_gui", return_value={"external": True}) as lg:
            codeagent.execute_code(nom)
        lg.assert_called_once()


class SansMatplotlibTest(_WorkspaceTest):
    """Script qui n'importe jamais matplotlib : `images` vide, le hook ne
    casse rien, aucun dossier temporaire résiduel après exécution."""

    def test_images_vide_et_aucun_dossier_residuel(self):
        nom = self._ecrire("script.py", "print('bonjour')\n")

        dossiers_crees: list[str] = []
        original_mkdtemp = tempfile.mkdtemp

        def _mkdtemp_trace(*a, **kw):
            d = original_mkdtemp(*a, **kw)
            dossiers_crees.append(d)
            return d

        with mock.patch.object(codeagent.tempfile, "mkdtemp", side_effect=_mkdtemp_trace):
            out = codeagent.execute_code(nom)

        self.assertEqual(out["returncode"], 0, out)
        self.assertEqual(out["stdout"].strip(), "bonjour")
        self.assertEqual(out.get("images"), [])
        self.assertEqual(len(dossiers_crees), 1, "un dossier de figures dédié par exécution")
        self.assertFalse(Path(dossiers_crees[0]).exists(), "le dossier de figures doit être nettoyé")

    def test_pas_dexception_ni_de_ralentissement_perceptible(self):
        """sitecustomize est importé à CHAQUE script .py — même ceux sans
        aucun rapport avec matplotlib : ne doit ajouter ni exception ni
        latence perceptible."""
        nom = self._ecrire("rapide.py", "print(1 + 1)\n")
        out = codeagent.execute_code(nom)
        self.assertEqual(out["returncode"], 0, out)
        self.assertEqual(out["stdout"].strip(), "2")
        self.assertLess(out["duration_ms"], 10_000)


@unittest.skipUnless(_MPL_OK, _MPL_SKIP_RAISON)
class AvecMatplotlibTest(_WorkspaceTest):
    """Tests bout en bout, avec un VRAI interpréteur + matplotlib installé."""

    def test_figure_sans_savefig_explicite_est_capturee_inline(self):
        """Le cas central de la tâche : l'utilisateur ne pense pas à
        sauvegarder lui-même sa figure."""
        nom = self._ecrire("plot.py", (
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 2, 3])\n"
        ))
        out = codeagent.execute_code(nom)
        self.assertEqual(out["returncode"], 0, out)
        images = out.get("images")
        self.assertTrue(images, "au moins une figure attendue")
        self.assertEqual(images[0]["nom"], "figure_1.png")
        data = base64.b64decode(images[0]["data_base64"])
        self.assertTrue(data.startswith(b"\x89PNG"), "signature PNG attendue en tête des octets décodés")

    def test_matplotlib_ne_route_plus_vers_launch_gui(self):
        nom = self._ecrire("plot2.py", "import matplotlib.pyplot as plt\nplt.plot([1])\n")
        with mock.patch.object(codeagent, "_launch_gui") as lg:
            codeagent.execute_code(nom)
        lg.assert_not_called()

    def test_plt_close_avant_la_fin_rend_images_vide(self):
        nom = self._ecrire("plot_ferme.py", (
            "import matplotlib.pyplot as plt\n"
            "plt.plot([1, 2])\n"
            "plt.close('all')\n"
        ))
        out = codeagent.execute_code(nom)
        self.assertEqual(out["returncode"], 0, out)
        self.assertEqual(out.get("images"), [])

    def test_aucun_dossier_residuel_apres_execution_reussie(self):
        nom = self._ecrire("plot3.py", "import matplotlib.pyplot as plt\nplt.plot([1, 2])\n")
        dossiers_crees: list[str] = []
        original_mkdtemp = tempfile.mkdtemp

        def _mkdtemp_trace(*a, **kw):
            d = original_mkdtemp(*a, **kw)
            dossiers_crees.append(d)
            return d

        with mock.patch.object(codeagent.tempfile, "mkdtemp", side_effect=_mkdtemp_trace):
            codeagent.execute_code(nom)
        self.assertEqual(len(dossiers_crees), 1)
        self.assertFalse(Path(dossiers_crees[0]).exists())

    def test_plafond_du_nombre_de_figures(self):
        nom = self._ecrire("beaucoup.py", (
            "import matplotlib.pyplot as plt\n"
            "for i in range(15):\n"
            "    plt.figure()\n"
            "    plt.plot([i, i + 1])\n"
        ))
        out = codeagent.execute_code(nom)
        self.assertEqual(out["returncode"], 0, out)
        images = out.get("images")
        self.assertEqual(len(images), codeagent._PLOT_MAX_IMAGES)
        self.assertIn("figures produites", out["stderr"])

    def test_ordre_des_figures_est_numerique_pas_lexicographique(self):
        nom = self._ecrire("dix.py", (
            "import matplotlib.pyplot as plt\n"
            "for i in range(10):\n"
            "    plt.figure()\n"
            "    plt.plot([i])\n"
        ))
        out = codeagent.execute_code(nom)
        noms = [img["nom"] for img in out["images"]]
        self.assertEqual(noms, [f"figure_{i}.png" for i in range(1, len(noms) + 1)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
