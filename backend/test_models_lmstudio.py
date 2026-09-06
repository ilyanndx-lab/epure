#!/usr/bin/env python3
"""LM Studio comme troisième fournisseur local — même rang qu'Ollama et FLM.

Fichier DÉDIÉ plutôt qu'une extension de `test_vision_images.py` : ce dernier
couvre la dégradation de la description d'image (choix du modèle vision,
timeouts, logs de diagnostic), un sujet sans rapport avec la détection d'un
fournisseur de modèles. La convention réellement proche est celle de
`test_models_cloud_sans_cle.py` — même endpoint (`GET /models`), même façon de
remplacer une sonde par un attribut du module `main` plutôt que de mocker
`urllib` bout en bout à ce niveau. Elle est reprise ici pour les tests
d'endpoint ; les tests unitaires des deux sondes (`check_lmstudio`,
`get_lmstudio_installed`) mockent `urllib.request.urlopen`, comme
`test_web_search.py` le fait pour `core.websearch`.

Ce que ces tests gardent :

1. `core.models.check_lmstudio` / `get_lmstudio_installed` suivent le contrat
   de leurs équivalents Ollama/FLM : `None` (jamais une exception) signale un
   serveur injoignable, distinct d'une liste vide (serveur joignable, aucun
   modèle chargé) ;
2. `GET /models` ajoute un bloc `local_lmstudio`, sans jamais faire tomber les
   autres blocs (`local`, `local_npu`, `cloud`) si LM Studio ne répond pas ;
3. Ollama et LM Studio peuvent tous deux exposer un modèle du même nom
   (`qwen2.5:7b`) : `local` ne préfixe pas ses ids (juste le nom du modèle),
   donc `local_lmstudio` préfixe les siens avec `lmstudio:` — sans ça, deux
   entrées de fournisseurs différents partageraient la même clé côté
   sélecteur du frontend ;
4. `GET /health` interroge LM Studio dans le MÊME `asyncio.gather` que
   Ollama/FLM (cf. CLAUDE.md §pièges, healthcheck borné à 2 s par sonde) —
   une sonde LM Studio qui ne répond jamais ne doit pas faire dépasser ce
   plafond, ni faire disparaître `ollama`/`flm` de la réponse.

Usage :
    python test_models_lmstudio.py
"""

import os
import sys
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.* / main

# Lues à l'import de main : les figer rend le test indépendant du poste.
os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ["EPURE_CORS_ORIGINS"] = "http://localhost:5173"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import core.models as core_models  # noqa: E402
import main  # noqa: E402
from core.auth import get_api_token  # noqa: E402

#: Les six clés que /models consulte pour le cloud — reprises telles quelles
#: de test_models_cloud_sans_cle.py pour neutraliser le poste de dev.
_CLES_CLOUD = (
    "GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
    "MISTRAL_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY",
)


class _FakeResponse:
    """Réponse HTTP minimale compatible avec `with urlopen(...) as resp`."""

    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── Sondes unitaires : core.models.check_lmstudio / get_lmstudio_installed ──

class SondesLmStudioTest(unittest.TestCase):
    """`urllib.request.urlopen` mocké directement — pas de réseau, pas de
    dépendance à un vrai serveur LM Studio sur le poste qui exécute la CI.
    """

    def test_check_lmstudio_true_si_le_serveur_repond(self):
        with mock.patch.object(
            core_models.urllib.request, "urlopen",
            return_value=_FakeResponse('{"data": []}'),
        ):
            self.assertTrue(core_models.check_lmstudio())

    def test_check_lmstudio_false_si_injoignable(self):
        with mock.patch.object(
            core_models.urllib.request, "urlopen",
            side_effect=OSError("connexion refusée"),
        ):
            self.assertFalse(core_models.check_lmstudio())

    def test_get_lmstudio_installed_parse_le_format_openai(self):
        """Format `{"data": [{"id": ...}]}` — PAS le format Ollama
        (`{"models": [{"name"/"model": ...}]}`) : LM Studio suit `/v1/models`
        comme FLM, pas comme Ollama.
        """
        corps = '{"data": [{"id": "llama-3.1-8b-instruct"}, {"id": "qwen2.5-7b"}]}'
        with mock.patch.object(
            core_models.urllib.request, "urlopen", return_value=_FakeResponse(corps),
        ):
            self.assertEqual(
                core_models.get_lmstudio_installed(),
                ["llama-3.1-8b-instruct", "qwen2.5-7b"],
            )

    def test_get_lmstudio_installed_ignore_les_entrees_sans_id(self):
        corps = '{"data": [{"id": "modele-a"}, {"object": "model"}]}'
        with mock.patch.object(
            core_models.urllib.request, "urlopen", return_value=_FakeResponse(corps),
        ):
            self.assertEqual(core_models.get_lmstudio_installed(), ["modele-a"])

    def test_get_lmstudio_installed_none_si_injoignable(self):
        """None, pas une liste vide — distingue « serveur éteint » de
        « serveur allumé, rien chargé », même contrat que get_ollama_installed.
        """
        with mock.patch.object(
            core_models.urllib.request, "urlopen",
            side_effect=OSError("connexion refusée"),
        ):
            self.assertIsNone(core_models.get_lmstudio_installed())


# ── GET /models : le bloc local_lmstudio ─────────────────────────────────────

class ModelesLmStudioEndpointTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(
            main.app, base_url="http://localhost", client=("127.0.0.1", 54321)
        )
        cls.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        async def _catalogue_vide():
            return {}

        self._originaux = {
            "get_ollama_installed": main.get_ollama_installed,
            "check_flm": main.check_flm,
            "get_lmstudio_installed": main.get_lmstudio_installed,
            "get_catalog": main.models_registry.get_catalog,
        }
        # Ollama/FLM neutralisés : ces tests portent sur LM Studio uniquement.
        main.get_ollama_installed = lambda: ["qwen2.5:7b"]
        main.check_flm = lambda: False
        main.models_registry.get_catalog = _catalogue_vide

        self._sauvegarde_cles = {nom: os.environ.get(nom) for nom in _CLES_CLOUD}
        for nom in _CLES_CLOUD:
            os.environ.pop(nom, None)

    def tearDown(self):
        for nom, valeur in self._originaux.items():
            setattr(main, nom, valeur)
        for nom, valeur in self._sauvegarde_cles.items():
            if valeur is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = valeur

    def _models(self) -> dict:
        r = self.client.get("/models", headers=self.entetes)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_lmstudio_apparait_avec_ses_modeles(self):
        main.get_lmstudio_installed = lambda: ["llama-3.1-8b-instruct"]
        d = self._models()
        self.assertEqual(len(d["local_lmstudio"]), 1)
        (modele,) = d["local_lmstudio"]
        self.assertEqual(modele["provider"], "lmstudio")
        self.assertTrue(modele["disponible"])
        self.assertEqual(modele["nom"], "llama-3.1-8b-instruct")

    def test_lmstudio_injoignable_ne_casse_pas_le_reste(self):
        main.get_lmstudio_installed = lambda: None
        d = self._models()
        self.assertEqual(d["local_lmstudio"], [])
        # Ollama, mocké joignable ci-dessus, doit rester intact.
        self.assertTrue(d["local"])
        self.assertIn("cloud", d)

    def test_pas_de_collision_id_entre_ollama_et_lmstudio_sur_le_meme_nom(self):
        """Ollama ET LM Studio exposent tous deux `qwen2.5:7b` (setUp pose déjà
        Ollama sur ce nom) — les deux entrées doivent avoir des ids distincts.
        """
        main.get_lmstudio_installed = lambda: ["qwen2.5:7b"]
        d = self._models()
        id_ollama = {m["id"] for m in d["local"]}
        id_lmstudio = {m["id"] for m in d["local_lmstudio"]}
        self.assertEqual(id_ollama, {"qwen2.5:7b"})
        self.assertEqual(id_lmstudio, {"lmstudio:qwen2.5:7b"})
        self.assertEqual(id_ollama & id_lmstudio, set(), "collision d'id entre fournisseurs")


# ── GET /health : lmstudio dans le même gather borné ────────────────────────

class HealthcheckLmStudioTest(unittest.TestCase):
    """Le `TestClient` doit garder un portail anyio PERSISTANT (`with client:`),
    pas un par requête (comportement par défaut de `TestClient(...)` sans
    context manager). Mesuré : sur ce poste, un portail rouvert à chaque appel
    fait attendre la fermeture de son event loop jusqu'à ce que l'exécuteur
    par défaut ait fini de drainer TOUS ses threads — y compris celui, encore
    en train de dormir, que `wait_for` vient d'abandonner. Le healthcheck
    répond bien en ~2 s (vérifié en isolant l'appel dans le même portail), mais
    `client.get(...)` ne RENDAIT la main qu'après les 10 s pleines du blocage,
    pour une raison propre au TestClient et absente d'un vrai process uvicorn
    (une seule boucle, jamais fermée entre deux requêtes). Un portail persistant
    reproduit ce cas réel.
    """

    @classmethod
    def setUpClass(cls):
        cls._cm = TestClient(main.app, base_url="http://localhost")
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    def setUp(self):
        self._originaux = {
            "get_ollama_installed": main.get_ollama_installed,
            "check_flm": main.check_flm,
            "check_lmstudio": main.check_lmstudio,
        }
        # Rapides et déterministes : seule la sonde LM Studio est mise en
        # cause dans ces tests.
        main.get_ollama_installed = lambda: None
        main.check_flm = lambda: False

    def tearDown(self):
        for nom, valeur in self._originaux.items():
            setattr(main, nom, valeur)

    def test_lmstudio_figure_dans_la_reponse(self):
        main.check_lmstudio = lambda: True
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["lmstudio"])

    def test_une_sonde_lmstudio_qui_bloque_est_bornee_a_2s(self):
        """Même plafond que get_ollama_installed/check_flm : une sonde qui ne
        répond jamais ne doit pas faire dépasser ~2 s, ni faire échouer le
        reste de /health.
        """
        def _bloque_indefiniment():
            time.sleep(5)
            return True

        main.check_lmstudio = _bloque_indefiniment
        debut = time.perf_counter()
        r = self.client.get("/health")
        ecoule = time.perf_counter() - debut
        self.assertEqual(r.status_code, 200, r.text)
        self.assertLess(ecoule, 4.0, "la sonde LM Studio n'est pas bornée à 2 s")
        self.assertFalse(r.json()["lmstudio"])
        # Le reste du healthcheck doit rester lisible malgré le blocage.
        self.assertIn("ollama", r.json())
        self.assertIn("flm", r.json())


# ── État de chargement : la sonde et la route qui l'expose ──────────────────

class EtatChargementLmStudioTest(unittest.TestCase):
    """`core.models.etat_modele_lmstudio` / `lmstudio_chargement_en_cours`.

    **Ces deux fonctions existent parce qu'un POURCENTAGE, lui, n'existe pas.**
    Mesuré sur ce poste le 2026-09-06 (LM Studio 0.4.23) : le corps de
    `POST /api/v1/models/load` refuse `stream` en 400 « Unrecognized key(s) »
    — donc son schéma est FERMÉ, l'absence est prouvée et pas seulement
    constatée —, la réponse arrive en un seul bloc à la fin, et aucune route
    d'état n'existe ailleurs sur `/api/v1`. Le seul signal réel est le champ
    `state` de `/api/v0/models/{id}`, qui n'est pas un nombre. Ce que ces tests
    gardent, c'est donc autant le comportement que la DÉCISION : rien ici ne
    doit se mettre à produire un chiffre.

    `urllib.request.urlopen` est mocké — la CI n'a pas de LM Studio, et le
    poste de dev ne doit pas faire dépendre le résultat de ce qui y tourne.
    """

    def _reponse(self, corps):
        return mock.patch.object(
            core_models.urllib.request, "urlopen", return_value=_FakeResponse(corps),
        )

    def test_etat_rend_le_champ_state_tel_quel(self):
        for etat in core_models.ETATS_LMSTUDIO_MESURES:
            with self.subTest(etat=etat):
                with self._reponse('{"id": "m", "state": "%s"}' % etat):
                    self.assertEqual(core_models.etat_modele_lmstudio("m"), etat)

    def test_etat_none_si_injoignable(self):
        """Injoignable et « pas chargé » ne se confondent pas : le premier ne
        permet aucune affirmation, le second en est une."""
        with mock.patch.object(
            core_models.urllib.request, "urlopen", side_effect=OSError("refusee"),
        ):
            self.assertIsNone(core_models.etat_modele_lmstudio("m"))

    def test_etat_none_sur_500_transitoire(self):
        """LM Studio rend un 500 (corps HTML) à l'instant précis où un
        chargement démarre — observé une fois sur 79 sondes le 2026-09-06. Il
        ne doit ni lever, ni se lire comme un état."""
        erreur = urllib.error.HTTPError(
            "http://x/api/v0/models/m", 500, "Internal Server Error", {}, None,
        )
        with mock.patch.object(
            core_models.urllib.request, "urlopen", side_effect=erreur,
        ):
            self.assertIsNone(core_models.etat_modele_lmstudio("m"))

    def test_etat_none_si_le_champ_manque_ou_est_vide(self):
        for corps in ('{"id": "m"}', '{"id": "m", "state": ""}', '{"id": "m", "state": 3}'):
            with self.subTest(corps=corps):
                with self._reponse(corps):
                    self.assertIsNone(core_models.etat_modele_lmstudio("m"))

    def test_etat_none_sur_identifiant_vide_sans_appel_reseau(self):
        with mock.patch.object(core_models.urllib.request, "urlopen") as urlopen:
            self.assertIsNone(core_models.etat_modele_lmstudio(""))
        urlopen.assert_not_called()

    def test_le_slash_de_la_cle_est_encode(self):
        """Une clé LM Studio porte un « / » (`éditeur/modèle`). Laissé tel
        quel il serait lu comme un segment de chemin par le serveur d'en face.
        """
        with mock.patch.object(
            core_models.urllib.request, "urlopen",
            return_value=_FakeResponse('{"state": "loaded"}'),
        ) as urlopen:
            core_models.etat_modele_lmstudio("mistralai/ministral-3-3b")
        url = urlopen.call_args[0][0].full_url
        self.assertIn("mistralai%2Fministral-3-3b", url)
        self.assertNotIn("mistralai/ministral-3-3b", url)

    def test_en_cours_est_vrai_tant_que_l_etat_n_est_pas_loaded(self):
        """**`!= "loaded"`, jamais `== "loading"`.** Seuls trois états ont été
        observés, mais un quatrième (une file d'attente, par exemple) ne doit
        pas faire disparaître l'indicateur en silence pendant l'attente.
        """
        for etat, attendu in (
            ("loaded", False), ("loading", True), ("not-loaded", True),
            ("queued", True),  # jamais observé — c'est tout l'intérêt du test
        ):
            with self.subTest(etat=etat):
                with self._reponse('{"state": "%s"}' % etat):
                    self.assertIs(core_models.lmstudio_chargement_en_cours("m"), attendu)

    def test_en_cours_none_si_on_ne_sait_pas(self):
        with mock.patch.object(
            core_models.urllib.request, "urlopen", side_effect=OSError("refusee"),
        ):
            self.assertIsNone(core_models.lmstudio_chargement_en_cours("m"))


class RouteChargementLmStudioTest(unittest.TestCase):
    """`GET /models/lmstudio/chargement` — ce que le chat sonde pendant qu'il
    attend son premier token."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(
            main.app, base_url="http://localhost", client=("127.0.0.1", 54321)
        )
        cls.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        self._sonde = main.lmstudio_chargement_en_cours
        self._contexte = main.memory.get_context

    def tearDown(self):
        main.lmstudio_chargement_en_cours = self._sonde
        main.memory.get_context = self._contexte

    def _avec_modele(self, actif):
        main.memory.get_context = lambda: {"modèle_actif": actif}

    def test_modele_non_lmstudio_repond_false_sans_sonder(self):
        """`false` et non `null` : « ce n'est pas LM Studio » est une
        certitude. Et surtout AUCUNE sonde — interroger LM Studio pour un
        modèle Ollama rendrait `null` (inconnu de son catalogue), que le
        frontend garderait affiché.
        """
        appels = []
        main.lmstudio_chargement_en_cours = lambda nom: appels.append(nom)
        for actif in ("qwen2.5:7b", "flm:lfm2:1.2b", "gemini-2.5-flash", ""):
            with self.subTest(actif=actif):
                self._avec_modele(actif)
                r = self.client.get("/models/lmstudio/chargement", headers=self.entetes)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json(), {"modele": None, "chargement": False})
        self.assertEqual(appels, [], "LM Studio sonde pour un modele qui n'est pas le sien")

    def test_le_prefixe_lmstudio_est_retire_avant_la_sonde(self):
        """`lmstudio:` est une convention d'ids de `GET /models` ; le catalogue
        de LM Studio ne connaît que le nom nu."""
        vus = []

        def _sonde(nom):
            vus.append(nom)
            return True

        main.lmstudio_chargement_en_cours = _sonde
        self._avec_modele("lmstudio:mistralai/ministral-3-3b")
        r = self.client.get("/models/lmstudio/chargement", headers=self.entetes)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(vus, ["mistralai/ministral-3-3b"])
        self.assertEqual(
            r.json(), {"modele": "mistralai/ministral-3-3b", "chargement": True},
        )

    def test_les_trois_reponses_passent_telles_quelles(self):
        """`null` doit ARRIVER au frontend comme `null` : c'est lui qui le
        traduit par « aucune information nouvelle » et garde son affichage. Le
        replier ici en `false` ferait clignoter l'indicateur."""
        for renvoye in (True, False, None):
            with self.subTest(renvoye=renvoye):
                main.lmstudio_chargement_en_cours = lambda _n, _r=renvoye: _r
                self._avec_modele("lmstudio:m")
                r = self.client.get("/models/lmstudio/chargement", headers=self.entetes)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIs(r.json()["chargement"], renvoye)

    def test_la_reponse_ne_porte_aucun_nombre(self):
        """Le garde-fou du chantier : LM Studio n'expose aucun pourcentage sur
        son API HTTP (mesuré — schéma de chargement fermé, réponse en un bloc,
        pas de route d'état). Si un champ numérique apparaît un jour dans cette
        réponse, il aura été FABRIQUÉ, et ce test doit tomber.
        """
        main.lmstudio_chargement_en_cours = lambda _n: True
        self._avec_modele("lmstudio:m")
        corps = self.client.get(
            "/models/lmstudio/chargement", headers=self.entetes,
        ).json()
        self.assertEqual(set(corps), {"modele", "chargement"})
        for cle, valeur in corps.items():
            # `bool` est une sous-classe d'`int` en Python : un
            # `assertNotIsInstance(valeur, int)` seul ferait tomber ce test sur
            # le `chargement: true` qu'on veut justement autoriser. C'est le
            # TYPE EXACT qui distingue un drapeau d'un pourcentage.
            self.assertNotIn(
                type(valeur), (int, float),
                f"champ numerique inattendu : {cle}={valeur!r} — aucun signal "
                f"chiffre n'existe cote LM Studio, celui-ci serait invente",
            )

    def test_le_prefixe_survit_au_vrai_reglage_sans_contexte_mocke(self):
        """La chaîne COMPLÈTE, `memory` non mocké : choisir un modèle dans
        ModuleBar (`PATCH /context/settings`) doit rendre la route capable de
        le reconnaître.

        **C'est le seul test du fichier qui ne mocke pas `get_context`, et
        c'est tout son intérêt.** Tous les autres posent eux-mêmes un
        `modèle_actif` déjà préfixé — ils ne peuvent donc pas voir la panne la
        plus vraisemblable de cette fonctionnalité : que le `lmstudio:` de
        `m.id` soit normalisé quelque part entre le sélecteur et le contexte
        mémoire. Il serait alors absent ici, `startswith` répondrait `False`,
        et l'indicateur serait mort dans l'application réelle avec tous les
        autres tests au vert.

        La sonde LM Studio reste remplacée : la CI n'a pas de LM Studio, et ce
        qu'on vérifie est le passage de l'identifiant, pas le réseau.
        """
        vus = []
        main.lmstudio_chargement_en_cours = lambda nom: vus.append(nom) or True
        # Restauré par le tearDown de la classe (get_context y est réaffecté à
        # l'original, qu'on n'a de toute façon pas touché ici).
        main.memory.get_context = self._contexte

        r = self.client.patch(
            "/context/settings",
            headers={**self.entetes, "Content-Type": "application/json"},
            json={"modèle_actif": "lmstudio:mistralai/ministral-3-3b"},
        )
        self.assertEqual(r.status_code, 200, r.text)

        # Le réglage est bien arrivé tel quel dans le contexte…
        self.assertEqual(
            self.client.get("/context", headers=self.entetes).json().get("modèle_actif"),
            "lmstudio:mistralai/ministral-3-3b",
            "le préfixe `lmstudio:` n'a pas survécu à PATCH /context/settings",
        )
        # …et la route en tire le nom NU que connaît le catalogue LM Studio.
        corps = self.client.get(
            "/models/lmstudio/chargement", headers=self.entetes,
        ).json()
        self.assertEqual(vus, ["mistralai/ministral-3-3b"])
        self.assertEqual(corps["modele"], "mistralai/ministral-3-3b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
