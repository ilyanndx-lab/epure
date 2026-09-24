#!/usr/bin/env python3
"""Supprimer un module, redémarrer le backend : sa route répond 404 — VRAI processus.

Pendant d'intégration de ``test_redemarrage_modules.py``. Les tests unitaires
simulent le redémarrage dans le processus de test (purge de ``sys.modules``,
app neuve) ; celui-ci le fait pour de bon, exactement comme ``epure_tray.py`` :

  1. uvicorn lancé avec ``EPURE_SENTINELLE_REDEMARRAGE`` posé ;
  2. ``DELETE /settings/modules/hello`` — la route RÉPOND ENCORE (aucun
     démontage à chaud, c'est voulu) et ``GET /instance/redemarrage`` le dit ;
  3. ``POST /instance/redemarrage`` écrit la sentinelle ;
  4. ce script joue le tray : ``lanceur.consommer_sentinelle``, puis
     ``lanceur.tuer_arbre`` (l'ARBRE : le PID lancé n'est pas celui qui écoute,
     cf. l'en-tête de ``epure_tray.py``), puis relance ;
  5. ``/health`` répond avec un AUTRE ``boot_id``, la route répond 404, et plus
     aucun redémarrage n'est requis.

C'est l'échange que ``docs/demontage-option-d.md`` §3 étape D annonçait : une
vérification en processus (« la route disparaît ») troquée contre une
vérification qui coûte un processus. D'où le préfixe ``integration_`` — hors de
la découverte ``test_*.py``, lancé à la main.

ISOLATION. uvicorn importe ``modules.*`` depuis son dossier courant (paquet
d'espace de noms), pas depuis ``EPURE_MODULES_DIR`` : supprimer ``hello`` dans
l'arbre réel détruirait un module de l'utilisateur. Le script travaille donc
sur une COPIE temporaire de ``backend/`` (sans données), et pose
``EPURE_DATA_DIR`` / ``EPURE_GENERATED_DIR`` / ``EPURE_WEB_DIR`` sur des
temporaires. Rien n'est écrit hors de ces temporaires.

Usage :
    python integration_redemarrage.py        (depuis backend/, venv activé)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(_BACKEND.parent))
import lanceur  # noqa: E402

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
_IGNORER = shutil.ignore_patterns(
    "__pycache__", "memory", "history", "vector_db", "chroma_db", "doc_uploads",
    "_backups", "_staging", ".env", "*.log", "encre", "encre_dataset",
)


def _requete(methode: str, chemin: str, token: str | None = None, corps=None):
    donnees = json.dumps(corps).encode() if corps is not None else None
    req = urllib.request.Request(BASE + chemin, data=donnees, method=methode)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if donnees is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


class RedemarrageReelTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-integ-redem-"))
        self.backend = self.tmp / "backend"
        shutil.copytree(_BACKEND, self.backend, ignore=_IGNORER)
        for nom in ("data", "generated", "web"):
            (self.tmp / nom).mkdir()
        self.sentinelle = self.tmp / ".epure-redemarrage"
        self.env = dict(
            os.environ,
            EPURE_DATA_DIR=str(self.tmp / "data"),
            EPURE_GENERATED_DIR=str(self.tmp / "generated"),
            EPURE_WEB_DIR=str(self.tmp / "web"),
            EPURE_EMBEDDING_AUTOINSTALL="0", EPURE_HMER_AUTOINSTALL="0",
            HF_HUB_OFFLINE="1",
            **{lanceur.ENV_SENTINELLE: str(self.sentinelle)},
        )
        self.p = None
        self.addCleanup(self._arreter)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _lancer(self):
        self.assertIsNone(lanceur.port_occupant(PORT), f"port {PORT} déjà pris")
        self.p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
             "--port", str(PORT), "--no-access-log"],
            cwd=self.backend, env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertTrue(lanceur.attendre_backend(PORT, timeout=90), "uvicorn n'a pas démarré")

    def _arreter(self):
        if self.p and self.p.poll() is None:
            lanceur.tuer_arbre(self.p.pid)
            self.p.wait(timeout=15)

    def test_supprimer_puis_redemarrer(self):
        self._lancer()
        _, pair = _requete("GET", "/pair")
        token = pair["token"]
        _, sante = _requete("GET", "/health")
        boot1 = sante["boot_id"]

        self.assertEqual(_requete("GET", "/hello/ping", token)[0], 200, "prérequis")
        statut, corps = _requete("DELETE", "/settings/modules/hello", token)
        self.assertEqual(statut, 200, corps)
        # Pas de démontage à chaud : la route sert encore, et c'est DIT.
        self.assertEqual(_requete("GET", "/hello/ping", token)[0], 200)
        _, etat = _requete("GET", "/instance/redemarrage", token)
        self.assertTrue(etat["requis"])
        self.assertTrue(etat["automatique"])

        _, dem = _requete("POST", "/instance/redemarrage", token, {"raison": "intégration"})
        self.assertTrue(dem["déclenché"])

        # Le tray : effacer PUIS arrêter l'arbre PUIS relancer.
        t0 = time.monotonic()
        self.assertIsNotNone(lanceur.consommer_sentinelle(self.sentinelle))
        self.assertFalse(self.sentinelle.exists())
        self._arreter()
        self._lancer()
        duree = time.monotonic() - t0

        _, sante = _requete("GET", "/health")
        self.assertNotEqual(sante["boot_id"], boot1, "c'est l'ancien processus qui répond")
        self.assertEqual(_requete("GET", "/hello/ping", token)[0], 404,
                         "la route du module supprimé répond après redémarrage")
        _, etat = _requete("GET", "/instance/redemarrage", token)
        self.assertFalse(etat["requis"], etat)
        print(f"\nredémarrage (arrêt de l'arbre + relance + port accepté) : {duree:.1f} s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
