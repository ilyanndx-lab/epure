#!/usr/bin/env python3
"""Tests de la surface d'authentification HTTP (durcissement v1, lot 1).

Ce fichier verrouille ce qui est atteignable *sans* token, et ce qui ne doit
jamais sortir *avec*. Il couvre les cas de non-régression du lot 1 :

  - ``GET /health``          sans token                → 200 (sonde, exempte)
  - ``GET /pair``            depuis 127.0.0.1          → 200 + le token
  - ``GET /pair``            avec un Host étranger     → 400 (TrustedHost)
  - ``GET /models``          sans token → 401, avec    → 200
  - ``GET /instance/config`` n'expose ni ``auth`` ni ``atelier.gateway.api_key``

Plus l'invariant qui les rend vrais : **l'ordre d'empilement des middlewares**.
Starlette empile ``user_middleware`` dans l'ordre inverse de l'ajout, ce qui se
raisonne mal et se casse en déplaçant trois lignes. On l'affirme donc deux fois,
par la liste déclarée *et* par son effet observable (cf. MiddlewareOrderTest) —
la lecture du code ne suffit pas.

Attention : importer ``main`` démarre une vraie instance (config, moteurs,
préchauffage RAG dans un thread) et réinitialise ``memory/context_session.json``,
exactement comme un lancement du backend. Aucun réseau n'est touché : les sondes
Ollama/FLM sont neutralisées dans ``setUpModule``.

Usage :
    python test_auth_surface.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Permettre d'importer le package `core` depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

# Ces trois variables sont lues À L'IMPORT de main : les figer ici rend le test
# indépendant de l'environnement du poste (et de backend/.env, que load_dotenv
# n'applique jamais par-dessus une variable déjà posée).
os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ["EPURE_CORS_ORIGINS"] = "http://localhost:5173"
# Pas de validation du cache HF au démarrage (cf. core.runtime._hf_offline_if_cached).
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402  (après les variables ci-dessus)

import main  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.instance import InstanceConfig  # noqa: E402

#: Origine autorisée, alignée sur EPURE_CORS_ORIGINS ci-dessus.
_ORIGIN = "http://localhost:5173"

_ORIGINAUX: dict = {}


def setUpModule():
    """Neutralise les sondes réseau de /health et /models.

    Le sujet ici est la surface d'auth, pas la disponibilité d'Ollama. Sans ces
    stubs chaque appel paie les timeouts socket (3 s Ollama + 2,5 s FLM), et le
    catalogue cloud partirait sur le réseau dès qu'une clé traîne dans .env.
    """

    async def _catalogue_vide():
        return {}

    _ORIGINAUX["get_ollama_installed"] = main.get_ollama_installed
    _ORIGINAUX["check_flm"] = main.check_flm
    _ORIGINAUX["get_catalog"] = main.models_registry.get_catalog
    main.get_ollama_installed = lambda: None
    main.check_flm = lambda: False
    main.models_registry.get_catalog = _catalogue_vide


def tearDownModule():
    main.get_ollama_installed = _ORIGINAUX["get_ollama_installed"]
    main.check_flm = _ORIGINAUX["check_flm"]
    main.models_registry.get_catalog = _ORIGINAUX["get_catalog"]


def _client(**kw) -> TestClient:
    """TestClient configuré comme le frontend local.

    Deux défauts de TestClient sont incompatibles avec la surface testée et
    donneraient des échecs trompeurs :

    - ``base_url="http://testserver"`` → en-tête ``Host: testserver``, rejeté
      par TrustedHostMiddleware (400 sur *toutes* les routes) ;
    - ``client=("testclient", 50000)`` → ``request.client.host`` vaut
      « testclient », donc ``is_local_client`` est faux et /pair répond 403.
    """
    kw.setdefault("base_url", "http://localhost")
    kw.setdefault("client", ("127.0.0.1", 54321))
    return TestClient(main.app, **kw)


class AuthSurfaceTest(unittest.TestCase):
    """Les 5 cas de non-régression du lot 1, vus depuis le réseau."""

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.token = get_api_token()

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    # ── 1. /health : sonde, exempte de token ─────────────────────────────────

    def test_health_sans_token(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200, r.text)
        # C'est l'URL du healthcheck Docker : la réponse doit être exploitable.
        self.assertIn("ollama", r.json())

    # ── 2. /pair depuis la machine hôte ──────────────────────────────────────

    def test_pair_depuis_localhost(self):
        r = self.client.get("/pair")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("token"), self.token)

    def test_pair_depuis_une_autre_machine(self):
        """La garde IP historique doit rester en place derrière TrustedHost."""
        distant = _client(client=("192.168.1.42", 51000))
        r = distant.get("/pair")
        self.assertEqual(r.status_code, 403, r.text)

    # ── 3. /pair avec un Host étranger : le scénario DNS rebinding ───────────

    def test_pair_host_attaquant(self):
        """Domaine de l'attaquant résolvant vers 127.0.0.1 → rejeté sur le Host.

        La requête est par ailleurs parfaitement légitime (elle vient bien de
        127.0.0.1) : seul l'en-tête Host trahit l'origine.
        """
        r = self.client.get("/pair", headers={"Host": "attaquant.example"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertNotIn("token", r.text)

    # ── 4. /models : protégée par le token ───────────────────────────────────

    def test_models_sans_token(self):
        r = self.client.get("/models")
        self.assertEqual(r.status_code, 401, r.text)

    def test_models_avec_token_invalide(self):
        r = self.client.get("/models", headers={"Authorization": "Bearer faux"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_models_avec_token(self):
        r = self.client.get("/models", headers=self._auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("local", r.json())

    # ── 5. /instance/config ne laisse fuiter aucun secret ────────────────────

    def test_instance_config_n_expose_aucun_secret(self):
        r = self.client.get("/instance/config", headers=self._auth())
        self.assertEqual(r.status_code, 200, r.text)
        cfg = r.json()
        self.assertNotIn("auth", cfg)
        gateway = (cfg.get("atelier") or {}).get("gateway") or {}
        self.assertNotIn("api_key", gateway)
        # Le remplaçant dérivé, lui, doit être là (l'UI en a besoin).
        self.assertIsInstance(gateway.get("api_key_présente"), bool)
        # Filet large : aucune trace du token nulle part dans la réponse.
        self.assertNotIn(self.token, r.text)


class MiddlewareOrderTest(unittest.TestCase):
    """L'ordre d'empilement Starlette, vérifié — pas raisonné.

    ``add_middleware`` fait ``insert(0, ...)`` : ``user_middleware`` est donc
    dans l'ordre externe → interne, soit l'INVERSE de l'ordre d'écriture dans
    main.py.

    Chaque test ci-dessous dit ce qu'il discrimine réellement — vérifié en
    inversant l'ordre sur une app jouet, pas déduit :

    - la pile déclarée attrape n'importe quelle permutation ;
    - « Host étranger sur /health » attrape TrustedHost passé SOUS la garde de
      token, mais **pas** une permutation TrustedHost/CORS (sans en-tête Origin,
      CORS laisse simplement passer) ;
    - le préflight à Host étranger, lui, attrape cette permutation : c'est le
      seul cas où CORS répond avant que le Host ait été contrôlé.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = _client()

    def test_pile_declaree(self):
        pile = [m.cls.__name__ for m in main.app.user_middleware]
        self.assertEqual(
            pile,
            ["TrustedHostMiddleware", "CORSMiddleware", "BaseHTTPMiddleware"],
            "ordre des middlewares modifié — relire le commentaire ORDRE de main.py",
        )
        # BaseHTTPMiddleware est générique : confirmer que c'est bien la garde
        # de token et pas un autre middleware ajouté entre-temps.
        self.assertIs(
            main.app.user_middleware[-1].kwargs["dispatch"],
            main._require_api_token,
        )

    def test_trusted_host_est_hors_de_la_garde_de_token(self):
        """Effet observable : un Host étranger est rejeté même sur /health.

        /health est exempt de token. S'il répond 400 malgré tout, c'est que le
        filtrage du Host s'applique avant — donc plus à l'extérieur.
        """
        r = self.client.get("/health", headers={"Host": "attaquant.example"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_cors_est_hors_de_la_garde_de_token(self):
        """Effet observable : un 401 conserve ses en-têtes CORS.

        Sinon le navigateur transforme le refus d'auth en « network error »
        opaque, et le frontend ne peut plus déclencher un réappairage.
        """
        r = self.client.get("/models", headers={"Origin": _ORIGIN})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(r.headers.get("access-control-allow-origin"), _ORIGIN)

    def test_preflight_options_sans_token(self):
        """Un préflight doit passer sans token, sinon aucun appel authentifié
        ne peut démarrer depuis le navigateur."""
        r = self.client.options(
            "/models",
            headers={
                "Origin": _ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("access-control-allow-origin"), _ORIGIN)

    def test_preflight_a_host_etranger_est_rejete(self):
        """Le seul effet observable qui distingue TrustedHost de CORS.

        Sur un préflight, CORSMiddleware répond lui-même (200) sans appeler la
        suite de la pile. S'il était la couche externe, il court-circuiterait le
        contrôle du Host : ce test répondrait 200 au lieu de 400. C'est ce qui
        casse — silencieusement — si l'on remonte le bloc TrustedHost au-dessus
        du bloc CORS dans main.py.
        """
        r = self.client.options(
            "/models",
            headers={
                "Host": "attaquant.example",
                "Origin": _ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(r.status_code, 400, r.text)


class GatewayKeyRedactionTest(unittest.TestCase):
    """Expurgation de ``atelier.gateway.api_key``, sur une config jetable.

    Le test HTTP ci-dessus passerait à vide sur une instance sans clé : on
    reproduit donc ici une config qui en contient une, dans un fichier
    temporaire — jamais l'``instance_config.json`` du poste.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "instance_config.json"
        self.path.write_text(
            json.dumps(
                {
                    "instance_id": "test",
                    "auth": {"token": "TOKEN-SECRET"},
                    "atelier": {"gateway": {"api_key": "sk-secret", "model": "m"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.cfg = InstanceConfig(path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_masque_les_secrets(self):
        vue = self.cfg.get()
        self.assertNotIn("auth", vue)
        self.assertNotIn("api_key", vue["atelier"]["gateway"])
        self.assertTrue(vue["atelier"]["gateway"]["api_key_présente"])
        self.assertNotIn("sk-secret", json.dumps(vue, ensure_ascii=False))

    def test_raw_conserve_les_secrets(self):
        """Le moteur claude_gateway a besoin de la vraie valeur (cf. _gateway_cfg)."""
        brut = self.cfg.raw()
        self.assertEqual(brut["atelier"]["gateway"]["api_key"], "sk-secret")
        self.assertEqual(brut["auth"]["token"], "TOKEN-SECRET")

    def test_api_key_vide_ne_efface_pas_la_cle(self):
        """Le formulaire des Réglages renvoie le champ vide : ne pas l'appliquer.

        Sans ça, la clé serait effacée au premier passage dans les Réglages,
        puisque GET ne la renvoie plus.
        """
        self.cfg.update({"atelier": {"gateway": {"api_key": "", "model": "m2"}}})
        self.assertEqual(self.cfg.raw()["atelier"]["gateway"]["api_key"], "sk-secret")
        self.assertEqual(self.cfg.raw()["atelier"]["gateway"]["model"], "m2")

    def test_api_key_non_vide_remplace_la_cle(self):
        self.cfg.update({"atelier": {"gateway": {"api_key": "sk-neuve"}}})
        self.assertEqual(self.cfg.raw()["atelier"]["gateway"]["api_key"], "sk-neuve")

    def test_champ_derive_jamais_persiste(self):
        """api_key_présente est calculé : un client qui le renvoie est ignoré."""
        self.cfg.update({"atelier": {"gateway": {"api_key_présente": False}}})
        self.assertNotIn("api_key_présente", self.cfg.raw()["atelier"]["gateway"])

    def test_token_non_modifiable_par_update(self):
        self.cfg.update({"auth": {"token": "choisi-par-l-attaquant"}})
        self.assertEqual(self.cfg.raw()["auth"]["token"], "TOKEN-SECRET")


if __name__ == "__main__":
    unittest.main()
