#!/usr/bin/env python3
"""Démarrage de FastFlowLM depuis l'UI, quand `check_flm()` le signale éteint.

Contexte : `epure_tray.py::_demarrer` lance déjà `["flm", "serve", "--port",
str(PORT_FLM)]` au démarrage normal de l'application (icône, Ollama, Vite) —
**sans argument de modèle** : `flm serve` sert le NPU à la demande, un modèle
par requête, comme `ollama serve`. Il n'existe donc PAS de « modèle par défaut
à lancer » à choisir ici, contrairement à ce qu'on pourrait supposer par
analogie avec `POST /models/load` (qui, lui, charge un modèle Ollama nommé).
Ce fichier verrouille la commande EXACTE reprise du tray, pas un modèle en dur.

Ce que `core.models.start_flm()` ajoute par rapport au tray : un point d'entrée
HTTP pour le cas où l'utilisateur a fermé FLM après le lancement normal (ou
l'a lancé sur une machine où `epure_tray.py` n'a pas pu le trouver au premier
essai), avec le même patron que `core.module_workshop.start_gateway()` —
binaire vérifié, process détaché, jamais de kill d'un occupant du port.

Convention reprise de `test_models_lmstudio.py` : les sondes HTTP
(`check_flm`) sont mockées au niveau du module `core.models`, jamais par un
vrai serveur FLM sur le poste qui exécute la CI.

Usage :
    python test_flm_start.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ["EPURE_CORS_ORIGINS"] = "http://localhost:5173"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import core.models as core_models  # noqa: E402
import main  # noqa: E402
from core.auth import get_api_token  # noqa: E402


class _FakePopen:
    """Remplace `subprocess.Popen` : pas de vrai process, juste un `.poll()`
    piloté par le test. `None` = toujours vivant, un entier = sorti.
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self._code = None

    def poll(self):
        return self._code


class StartFlmTest(unittest.TestCase):
    """`core.models.start_flm()` — binaire, verrou, forme de la commande."""

    def setUp(self):
        # État module-level remis à neuf : un test précédent qui a « lancé »
        # FLM ne doit pas polluer celui qui suit.
        core_models._flm_process = None
        self._popen_calls: list = []
        self._popen = mock.patch.object(
            core_models.subprocess, "Popen",
            side_effect=lambda *a, **k: (
                self._popen_calls.append((a, k)) or _FakePopen(*a, **k)
            ),
        )
        self._popen.start()
        self.addCleanup(self._popen.stop)

        self._which = mock.patch.object(
            core_models.shutil, "which", return_value=r"C:\Program Files\flm\flm.exe",
        )
        self._which.start()
        self.addCleanup(self._which.stop)

        self._check_flm = mock.patch.object(core_models, "check_flm", return_value=False)
        self._check_flm.start()
        self.addCleanup(self._check_flm.stop)

    def tearDown(self):
        core_models._flm_process = None

    # ── Binaire absent ───────────────────────────────────────────────────────

    def test_binaire_absent_refuse_sans_spawn(self):
        with mock.patch.object(core_models.shutil, "which", return_value=None):
            res = core_models.start_flm()
        self.assertFalse(res["ok"])
        self.assertIn("introuvable", res["raison"])
        self.assertEqual(self._popen_calls, [])

    # ── Déjà joignable : rien à lancer ───────────────────────────────────────

    def test_deja_joignable_ne_relance_rien(self):
        with mock.patch.object(core_models, "check_flm", return_value=True):
            res = core_models.start_flm()
        self.assertTrue(res["ok"])
        self.assertEqual(self._popen_calls, [], "FLM répond déjà, rien à spawn")

    # ── Forme de la commande — PAS de modèle en dur ─────────────────────────

    def test_commande_reprend_exactement_celle_du_tray(self):
        """Même argv que `epure_tray.py::_demarrer` : `serve`, `--port`,
        `11435`. Aucun nom de modèle : ce n'est pas un paramètre de `flm
        serve`, cf. le docstring de ce fichier.
        """
        res = core_models.start_flm()
        self.assertTrue(res["ok"], res)
        (args, kwargs), = self._popen_calls
        cmd = args[0]
        self.assertEqual(cmd[0], r"C:\Program Files\flm\flm.exe")
        self.assertEqual(cmd[1:], ["serve", "--port", "11435"])
        self.assertFalse(kwargs.get("shell", False))

    # ── Garde anti double-spawn ──────────────────────────────────────────────

    def test_lancement_deja_en_cours_refuse_sans_second_spawn(self):
        """Simule un premier lancement encore vivant (`poll()` rend `None`) :
        un second appel doit être refusé SANS toucher `subprocess.Popen`, pas
        seulement en pratique mais par assertion sur le compteur d'appels —
        c'est le double-clic que le seul verrou ne couvrirait pas (cf.
        docstring de `core.models._flm_launch_lock`).
        """
        core_models._flm_process = _FakePopen()  # poll() -> None : vivant
        res = core_models.start_flm()
        self.assertFalse(res["ok"])
        self.assertIn("cours", res["raison"])
        self.assertEqual(self._popen_calls, [], "aucun second Popen ne doit partir")

    def test_process_precedent_mort_autorise_un_nouveau_lancement(self):
        """Un `flm serve` qui a crashé (poll() rend un code) ne doit pas
        bloquer indéfiniment les lancements suivants — sans ça, un échec
        transitoire condamnerait le bouton pour le reste du process backend.
        """
        mort = _FakePopen()
        mort._code = 1
        core_models._flm_process = mort
        res = core_models.start_flm()
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(self._popen_calls), 1)

    def test_echec_du_spawn_est_rapporte_sans_exception(self):
        with mock.patch.object(
            core_models.subprocess, "Popen", side_effect=OSError("boom"),
        ):
            res = core_models.start_flm()
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["raison"])


class StartFlmEndpointTest(unittest.TestCase):
    """`POST /models/flm/start` — le fil jusqu'à `core.models.start_flm`."""

    @classmethod
    def setUpClass(cls):
        cls._cm = TestClient(main.app, base_url="http://localhost")
        cls.client = cls._cm.__enter__()
        cls.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    def setUp(self):
        self._original = main.start_flm

    def tearDown(self):
        main.start_flm = self._original

    def test_route_relaie_le_resultat_de_start_flm(self):
        main.start_flm = lambda: {"ok": False, "raison": "FLM introuvable dans le PATH — vérifie l'installation."}
        r = self.client.post("/models/flm/start", headers=self.entetes)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {
            "ok": False,
            "raison": "FLM introuvable dans le PATH — vérifie l'installation.",
        })

    def test_route_exige_le_token(self):
        r = self.client.post("/models/flm/start")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
