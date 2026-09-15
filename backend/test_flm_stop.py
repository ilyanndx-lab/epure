#!/usr/bin/env python3
"""Fermeture de FastFlowLM depuis l'UI — symétrique de `test_flm_start.py`.

Contexte : `core.models.start_flm()` mémorise le `Popen` du `flm serve` qu'il a
lancé (`core.models._flm_process`, sous `core.models._flm_launch_lock`). Cette
fonction, `core.models.stop_flm()`, est le seul repli possible pour libérer la
RAM/NPU qu'il occupe — **aucun déchargement fin n'existe côté FLM** (mesuré sur
FLM v0.9.43 : `keep_alive: 0` sur `/api/generate` répond 200 sans rien
décharger, et aucun endpoint dédié — `/api/stop`, `/unload`, `/api/delete`,
`/api/eject`, `DELETE /api/generate` — n'existe, tous en 404 ; cf. le docstring
de `stop_flm`). Elle ferme donc le SERVEUR entier, jamais un modèle isolé.

Ce que ce fichier verrouille : elle ne cible QUE le process mémorisé (jamais un
kill par nom d'image, qui fermerait aussi un `flm serve` lancé à la main par
l'utilisateur en dehors d'Épure), elle distingue proprement « rien à fermer »
d'un échec, et `terminate()` est toujours essayé avant `kill()` — qui n'est
qu'un secours si le premier n'a pas suffi dans le délai imparti.

Même convention que `test_flm_start.py` : `subprocess.Popen`/`shutil.which`/
`check_flm` sont mockés au niveau du module `core.models`, jamais un vrai
serveur FLM sur le poste qui exécute la CI.

Usage :
    python test_flm_stop.py
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
    """Remplace `subprocess.Popen` : pas de vrai process.

    `poll()` est piloté par `_code` (`None` = vivant, un entier = sorti), comme
    dans `test_flm_start.py`. `terminate()`/`kill()`/`wait()` enregistrent leurs
    appels et, pour `wait()`, savent simuler un `TimeoutExpired` — c'est ce qui
    rejoue « terminate() ne répond pas » sans process réel : `terminate()` seul
    ne change pas `_code`, `kill()` le fait, et `wait()` lève tant que `_code`
    est encore `None` à la limite du délai qu'on lui a annoncée.
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self._code = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list = []
        #: Si vrai, `terminate()` seul ne suffit jamais : `_code` ne passe à
        #: un entier qu'au `kill()`. Simule un process qui ignore l'arrêt propre.
        self.ignore_terminate = False

    def poll(self):
        return self._code

    def terminate(self):
        self.terminate_calls += 1
        if not self.ignore_terminate:
            self._code = 0

    def kill(self):
        self.kill_calls += 1
        self._code = 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._code is None:
            import subprocess
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)
        return self._code


class StopFlmTest(unittest.TestCase):
    """`core.models.stop_flm()` — cible, garde, secours."""

    def setUp(self):
        core_models._flm_process = None
        self._check_flm = mock.patch.object(core_models, "check_flm", return_value=False)
        self._check_flm.start()
        self.addCleanup(self._check_flm.stop)

    def tearDown(self):
        core_models._flm_process = None

    # ── Rien à fermer ────────────────────────────────────────────────────────

    def test_rien_a_fermer_si_jamais_lance(self):
        """`_flm_process` à `None` — jamais lancé depuis cette application, ou
        backend rechargé depuis (cf. docstring de `core.models._flm_process`).
        Doit répondre proprement, sans tenter le moindre `terminate`/`kill`.
        """
        res = core_models.stop_flm()
        self.assertFalse(res["ok"])
        self.assertIn("Rien à fermer", res["raison"])

    def test_rien_a_fermer_si_process_deja_mort(self):
        """`poll()` rend déjà un code : le process a fini tout seul (crash,
        fermé par l'utilisateur) — même message que ci-dessus, pas un kill
        sur un cadavre.
        """
        mort = _FakePopen()
        mort._code = 1
        core_models._flm_process = mort
        res = core_models.stop_flm()
        self.assertFalse(res["ok"])
        self.assertIn("Rien à fermer", res["raison"])
        self.assertEqual(mort.terminate_calls, 0)
        self.assertEqual(mort.kill_calls, 0)

    # ── Cas nominal : terminate() suffit ────────────────────────────────────

    def test_arret_propre_confirme(self):
        vivant = _FakePopen()
        core_models._flm_process = vivant
        res = core_models.stop_flm()
        self.assertTrue(res["ok"], res)
        self.assertEqual(vivant.terminate_calls, 1)
        self.assertEqual(vivant.kill_calls, 0, "terminate() a suffi, pas de kill")
        self.assertIsNone(core_models._flm_process, "la mémoire du process est effacée")

    # ── terminate() ne répond pas → kill() de secours ───────────────────────

    def test_kill_de_secours_si_terminate_ne_repond_pas(self):
        """`terminate()` est essayé EN PREMIER, mais le process l'ignore
        (`ignore_terminate`) : le premier `wait()` doit lever `TimeoutExpired`,
        ce qui déclenche `kill()`, puis un second `wait()` qui réussit.
        """
        recalcitrant = _FakePopen()
        recalcitrant.ignore_terminate = True
        core_models._flm_process = recalcitrant
        res = core_models.stop_flm()
        self.assertTrue(res["ok"], res)
        self.assertEqual(recalcitrant.terminate_calls, 1, "terminate() essayé d'abord")
        self.assertEqual(recalcitrant.kill_calls, 1, "kill() en secours")
        self.assertEqual(len(recalcitrant.wait_calls), 2, "un wait() par tentative")

    # ── Le verrou cible bien LE process mémorisé, pas un autre ──────────────

    def test_verrou_partage_avec_start_flm(self):
        """`stop_flm` et `start_flm` protègent le même état sous le même
        verrou — pas un second, qui laisserait une fenêtre de course entre un
        clic « Démarrer » et un clic « Fermer » presque simultanés.
        """
        self.assertIs(
            core_models.stop_flm.__globals__["_flm_launch_lock"],
            core_models.start_flm.__globals__["_flm_launch_lock"],
        )

    # ── Post-condition journalisée, jamais fatale ───────────────────────────

    def test_echec_de_terminate_est_rapporte_sans_exception(self):
        vivant = _FakePopen()

        def _terminate_qui_explose():
            raise OSError("boom")

        vivant.terminate = _terminate_qui_explose
        core_models._flm_process = vivant
        res = core_models.stop_flm()
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["raison"])


class StopFlmEndpointTest(unittest.TestCase):
    """`POST /models/flm/stop` — le fil jusqu'à `core.models.stop_flm`."""

    @classmethod
    def setUpClass(cls):
        cls._cm = TestClient(main.app, base_url="http://localhost")
        cls.client = cls._cm.__enter__()
        cls.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    def setUp(self):
        self._original = main.stop_flm

    def tearDown(self):
        main.stop_flm = self._original

    def test_route_relaie_le_resultat_de_stop_flm(self):
        main.stop_flm = lambda: {"ok": True, "raison": "FLM fermé."}
        r = self.client.post("/models/flm/stop", headers=self.entetes)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "raison": "FLM fermé."})

    def test_route_exige_le_token(self):
        r = self.client.post("/models/flm/stop")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
