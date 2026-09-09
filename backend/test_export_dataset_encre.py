#!/usr/bin/env python3
"""Tests de `tools/export_dataset_encre.py` — export manuel du dataset (phase 3).

**IMPÉRATIF, et c'est le piège que ce fichier existe pour éviter** : chaque test
passe `--source-dir` ET `--out` sur des temporaires explicites. Le défaut de
sortie du script (`DEFAUT_SORTIE`, `<repo>/dataset_encre_export`) n'est ni
détourné ni surveillé par `_test_env` — `dataset_encre_export/` n'est pas dans
`REAL_DIRS` — donc un test qui l'appellerait sans `--out` écrirait pour de vrai
à la racine du dépôt, sans qu'aucun garde-fou ne le voie. Même famille que
l'avertissement de `integration_modules_mount.py` sur `EPURE_MODULES_DIR`.

Sur un jeu de fixtures : deux exemples fabriqués via `ExemplesEncreEngine`
(une correction, une dictée inversée) plus un troisième écrit à la main avec
des tracés vides, pour éprouver le chemin « ignoré, pas fatal ».

Usage :
    python test_export_dataset_encre.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.*

_BACKEND = Path(__file__).resolve().parent
_REPO = _BACKEND.parent
sys.path.insert(0, str(_REPO / "tools"))

import export_dataset_encre as export_script  # noqa: E402

from core import hmer  # noqa: E402
from core.encre_exemples import SOURCE_CORRECTION, SOURCE_DICTEE_INVERSEE, ExemplesEncreEngine  # noqa: E402
from core.jsonstore import write_json  # noqa: E402

#: Un triangle simple : assez de points pour produire un bounding box non vide,
#: donc une image que `_bitmap`/`_recadrer` savent rendre.
TRAIT = {
    "couleur": "#111827", "taille": 4,
    "points": [{"x": 10, "y": 10, "pression": 0.5, "t": 0},
               {"x": 100, "y": 10, "pression": 0.5, "t": 5},
               {"x": 55, "y": 90, "pression": 0.5, "t": 10}],
}


class ExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        racine = Path(self._tmp.name)
        self.source_dir = racine / "source"
        self.sortie = racine / "sortie"
        self.moteur = ExemplesEncreEngine(self.source_dir, racine / "progres")

    def tearDown(self):
        self._tmp.cleanup()

    def test_exporte_une_correction_et_une_dictee(self):
        self.moteur.create_exemple(
            SOURCE_CORRECTION, [TRAIT], texte_verite="x^{2}", texte_modele="x2")
        self.moteur.create_exemple(
            SOURCE_DICTEE_INVERSEE, [TRAIT], texte_verite=r"\alpha + \beta")

        exportes, ignores = export_script.exporter(self.source_dir, self.sortie)
        self.assertEqual(2, exportes)
        self.assertEqual(0, ignores)

        lignes = (self.sortie / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len(lignes))
        enregistrements = [json.loads(l) for l in lignes]
        sources = {e["source"] for e in enregistrements}
        self.assertEqual({SOURCE_CORRECTION, SOURCE_DICTEE_INVERSEE}, sources)

        for enreg in enregistrements:
            with self.subTest(id=enreg["id"]):
                self.assertEqual(hmer.VERSION, enreg["rendu_version"])
                image = self.sortie / enreg["image"]
                self.assertTrue(image.is_file(), f"image manquante : {image}")
                self.assertGreater(image.stat().st_size, 0)

        correction = next(e for e in enregistrements if e["source"] == SOURCE_CORRECTION)
        self.assertEqual("x^{2}", correction["texte_verite"])
        self.assertEqual("x2", correction["texte_modele"])

        dictee = next(e for e in enregistrements if e["source"] == SOURCE_DICTEE_INVERSEE)
        self.assertIsNone(dictee["texte_modele"])
        self.assertEqual(r"\alpha + \beta", dictee["texte_verite"])

    def test_un_exemple_sans_trace_est_ignore_pas_fatal(self):
        """Ne devrait pas exister en usage normal (les deux flux de création le
        refusent), mais un fichier corrompu ou édité à la main ne doit pas
        arrêter tout l'export pour autant — écrit directement sur disque pour
        contourner la garde du moteur et fabriquer ce cas."""
        vide = self.moteur.create_exemple(
            SOURCE_CORRECTION, [TRAIT], texte_verite="x")
        # Réécrit avec des tracés vides, après coup.
        chemin = self.source_dir / f"{vide['id']}.json"
        vide["strokes"] = []
        write_json(chemin, vide)

        self.moteur.create_exemple(SOURCE_CORRECTION, [TRAIT], texte_verite="y")

        exportes, ignores = export_script.exporter(self.source_dir, self.sortie)
        self.assertEqual(1, exportes)
        self.assertEqual(1, ignores)
        lignes = (self.sortie / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lignes))
        self.assertNotIn(vide["id"], (self.sortie / "manifest.jsonl").read_text(encoding="utf-8"))

    def test_dataset_absent_rend_un_manifeste_vide(self):
        exportes, ignores = export_script.exporter(self.source_dir, self.sortie)
        self.assertEqual((0, 0), (exportes, ignores))
        self.assertEqual([], (self.sortie / "manifest.jsonl").read_text(encoding="utf-8").splitlines())

    def test_main_accepte_source_dir_et_out_explicites(self):
        """Le chemin CLI, bout en bout — mais toujours sur des temporaires."""
        self.moteur.create_exemple(SOURCE_CORRECTION, [TRAIT], texte_verite="z")
        code = export_script.main([
            "--source-dir", str(self.source_dir), "--out", str(self.sortie)])
        self.assertEqual(0, code)
        self.assertTrue((self.sortie / "manifest.jsonl").is_file())

    def test_main_rend_1_si_le_dataset_est_introuvable(self):
        code = export_script.main([
            "--source-dir", str(self.source_dir / "n_existe_pas"),
            "--out", str(self.sortie)])
        self.assertEqual(1, code)


class DefautDeSortieTest(unittest.TestCase):
    """Le défaut de `--out` n'est JAMAIS invoqué par ces tests (cf. l'en-tête du
    fichier) : on se contente d'inspecter le parseur, sans appeler `exporter`."""

    def test_le_defaut_est_a_la_racine_du_depot_hors_dataset_source(self):
        self.assertEqual(export_script.DEFAUT_SORTIE, _REPO / "dataset_encre_export")
        self.assertNotEqual(export_script.DEFAUT_SORTIE, _test_env.REAL_ENCRE_DATASET_DIR)


if __name__ == "__main__":
    unittest.main(verbosity=2)
