#!/usr/bin/env python3
"""Modèles Ollama résidents en mémoire : lister, charger, éjecter.

`core/models.py` savait déjà dire ce qui est INSTALLÉ (`/api/tags`). Ce module
dit ce qui est CHARGÉ (`/api/ps`) et permet d'agir dessus. **Les deux ensembles
sont différents et l'un n'est pas inclus dans l'autre de façon utile** : sept
modèles installés, zéro chargé, est l'état normal d'une machine au repos.
Confondre les deux ferait afficher « chargé » pour tout ce qui est sur le
disque, c'est-à-dire l'inverse de l'information cherchée.

FORMES MESURÉES SUR UN VRAI OLLAMA (0.13, ce poste, 2026-09-05) et non lues
dans la doc — chaque décision ci-dessous s'appuie sur une de ces mesures :

    GET /api/ps, rien chargé      {"models": []}
    GET /api/ps, moondream chargé {"models": [{"name": "moondream:latest",
                                   "model": "moondream:latest", "size": ...,
                                   "digest": ..., "details": {...},
                                   "expires_at": "...", "size_vram": 0,
                                   "context_length": 2048}]}
    POST /api/generate keep_alive=0   {"done_reason": "unload", ...}
    POST /api/generate prompt=""      {"done_reason": "load", ...}
    modèle inconnu                    HTTP 404 {"error": "model '...' not found"}

**`size_vram: 0` NE VEUT PAS DIRE « pas chargé ».** Mesuré : `moondream`
résident, servi par le CPU, `size_vram` à 0. Ce champ distingue CPU et GPU, pas
chargé et déchargé — le seul indicateur de charge est l'appartenance au tableau
`models`. S'en remettre à `size_vram > 0` ferait lire « déchargé » sur toute
machine sans GPU, c'est-à-dire sur celles où l'information compte le plus.
(Même famille d'erreur que `finish_reason` qui ne discrimine pas un contenu
vide, §3.3 bis de CLAUDE.md.)

**ÉJECTER PENDANT UNE GÉNÉRATION NE FAIT RIEN, et c'est mesuré, pas déduit.**
Séquence jouée sur `qwen2.5:7b` : génération en flux lancée, éjection tirée 6 s
plus tard sur le même modèle. Résultat — l'éjection répond **HTTP 200 en 0,0 s**
avec `done_reason: "unload"`, la génération **se termine normalement** (1194
morceaux, 146 s, `done_reason: "stop"`), et le modèle est **de nouveau résident**
ensuite. Le `keep_alive` de la requête en vol l'emporte à sa complétion.

L'éjection n'est donc ni refusée ni effective : elle ment. La conséquence de
conception, testée plus bas, est que **`decharger()` ne rend jamais un succès
supposé** — il relit `/api/ps` et rend l'état RÉEL. Une UI qui croirait le 200
afficherait « déchargé » puis verrait le modèle réapparaître deux minutes plus
tard sans explication.

**OLLAMA ABSENT EST LE CAS NOMINAL**, pas une erreur : sur une machine sans
Ollama, `lister_charges()` rend `None` — la même convention que
`get_ollama_installed`, que `GET /models` traduit déjà en silence côté interface.

AUCUN TEST NE TOUCHE LE RÉSEAU. `urllib.request.urlopen` est remplacé pour
toute la classe, et un appel qui passerait au travers ÉCHOUE au lieu de partir
sur localhost : le bouchon lève sur une URL qu'il n'attendait pas.

Usage :
    python test_ollama_memoire.py
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — AVANT tout import de core.*

from core import ollama_memoire  # noqa: E402


# Corps réels, recopiés d'un Ollama qui tourne (cf. l'en-tête).
PS_VIDE = {"models": []}
PS_MOONDREAM = {
    "models": [
        {
            "name": "moondream:latest",
            "model": "moondream:latest",
            "size": 1301901474,
            "digest": "55fc3abd386771e5b5d1bbcc732f3c3f4df6e9f9f08f1131f9cc27ba2d1eec5b",
            "details": {"parent_model": "", "format": "gguf", "family": "phi2",
                        "families": ["phi2", "clip"], "parameter_size": "1B",
                        "quantization_level": "Q4_0"},
            "expires_at": "2026-09-05T23:12:26.376831+02:00",
            "size_vram": 0,
            "context_length": 2048,
        }
    ]
}
PS_SUR_GPU = {
    "models": [
        dict(PS_MOONDREAM["models"][0], model="qwen3:8b", name="qwen3:8b",
             size=5000000000, size_vram=5000000000),
    ]
}


class _Reponse(io.BytesIO):
    """Ce que rend `urlopen` : un flux + un `status`, utilisable en `with`."""

    def __init__(self, charge, status=200):
        super().__init__(json.dumps(charge).encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class _OllamaBouchonne(unittest.TestCase):
    """Remplace `urlopen` pour toute la classe et enregistre les appels.

    Le bouchon LÈVE sur une URL non prévue plutôt que de rendre un défaut :
    un test qui partirait vraiment sur le réseau doit échouer bruyamment, pas
    passer au vert parce qu'un Ollama tourne sur le poste de l'auteur.
    """

    def setUp(self):
        self.appels = []          # [(url, corps_envoye_ou_None, timeout)]
        self.reponses = {}        # url -> _Reponse | Exception (callable)
        patch = mock.patch.object(ollama_memoire.urllib.request, "urlopen",
                                  side_effect=self._urlopen)
        patch.start()
        self.addCleanup(patch.stop)

    def _urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        corps = None
        if getattr(req, "data", None):
            corps = json.loads(req.data.decode("utf-8"))
        self.appels.append((url, corps, timeout))
        fabrique = self.reponses.get(url)
        if fabrique is None:
            raise AssertionError(f"appel reseau non prevu par le test : {url}")
        sortie = fabrique() if callable(fabrique) else fabrique
        if isinstance(sortie, Exception):
            raise sortie
        return sortie

    def repondre(self, chemin, charge, status=200):
        url = f"{ollama_memoire.hote_ollama()}{chemin}"
        self.reponses[url] = lambda: _Reponse(charge, status)

    def echouer(self, chemin, exc):
        url = f"{ollama_memoire.hote_ollama()}{chemin}"
        self.reponses[url] = lambda: exc


class ListerChargesTest(_OllamaBouchonne):
    def test_rien_de_charge_rend_une_liste_vide_pas_none(self):
        """La distinction porte tout le reste : liste vide = Ollama répond et
        rien n'est chargé ; `None` = Ollama ne répond pas. Les confondre ferait
        afficher « injoignable » sur une machine au repos parfaitement saine."""
        self.repondre("/api/ps", PS_VIDE)
        self.assertEqual(ollama_memoire.lister_charges(), [])

    def test_un_modele_charge_est_decrit(self):
        self.repondre("/api/ps", PS_MOONDREAM)
        charges = ollama_memoire.lister_charges()
        self.assertEqual(len(charges), 1)
        m = charges[0]
        self.assertEqual(m["id"], "moondream:latest")
        self.assertEqual(m["taille"], 1301901474)
        self.assertEqual(m["expire_a"], "2026-09-05T23:12:26.376831+02:00")

    def test_size_vram_a_zero_reste_charge(self):
        """Le piège mesuré : moondream résident sur CPU annonce `size_vram: 0`.
        L'appartenance au tableau fait foi, jamais ce champ."""
        self.repondre("/api/ps", PS_MOONDREAM)
        m = ollama_memoire.lister_charges()[0]
        self.assertEqual(m["vram"], 0)
        self.assertEqual(m["processeur"], "cpu")

    def test_le_gpu_est_distingue_du_cpu(self):
        self.repondre("/api/ps", PS_SUR_GPU)
        self.assertEqual(ollama_memoire.lister_charges()[0]["processeur"], "gpu")

    def test_ollama_injoignable_rend_none_sans_lever(self):
        """Cas NOMINAL d'une machine sans Ollama — même convention que
        `get_ollama_installed`. Une exception ici remonterait en 500."""
        self.echouer("/api/ps", OSError("connexion refusee"))
        self.assertIsNone(ollama_memoire.lister_charges())

    def test_corps_inattendu_ne_fait_pas_tomber(self):
        """Un corps sans `models` (proxy, page d'erreur JSON) ne doit pas lever :
        même leçon que le §8 sur `.json()` cru sur parole, côté serveur."""
        self.repondre("/api/ps", {"detail": "quelque chose d'autre"})
        self.assertEqual(ollama_memoire.lister_charges(), [])

    def test_l_hote_vient_de_core_llm(self):
        """Ne PAS réimplémenter la normalisation : `OLLAMA_HOST=0.0.0.0` est une
        adresse d'écoute, inutilisable en connexion sous Windows (§8)."""
        from core.llm import ollama_host
        self.assertEqual(ollama_memoire.hote_ollama(), ollama_host)
        self.repondre("/api/ps", PS_VIDE)
        ollama_memoire.lister_charges()
        self.assertTrue(self.appels[0][0].startswith(ollama_host))


class ChargerTest(_OllamaBouchonne):
    def test_charger_poste_un_prompt_vide_sans_keep_alive(self):
        """`keep_alive` absent = défaut d'Ollama (5 min). Le poser ici
        imposerait une durée que l'utilisateur n'a pas choisie."""
        self.repondre("/api/generate", {"done": True, "done_reason": "load"})
        self.repondre("/api/ps", PS_MOONDREAM)
        ollama_memoire.charger("moondream:latest")
        _, corps, _ = self.appels[0]
        self.assertEqual(corps["model"], "moondream:latest")
        self.assertEqual(corps["prompt"], "")
        self.assertNotIn("keep_alive", corps)

    def test_charger_rend_l_etat_reel_relu(self):
        self.repondre("/api/generate", {"done": True, "done_reason": "load"})
        self.repondre("/api/ps", PS_MOONDREAM)
        res = ollama_memoire.charger("moondream:latest")
        self.assertTrue(res["ok"])
        self.assertEqual([m["id"] for m in res["charges"]], ["moondream:latest"])

    def test_modele_inconnu_rend_un_message_clair(self):
        """404 d'Ollama → message, jamais une exception nue qui sortirait en
        500 « Erreur interne »."""
        import urllib.error
        self.echouer("/api/generate", urllib.error.HTTPError(
            "u", 404, "Not Found", {},
            io.BytesIO(json.dumps({"error": "model 'zz' not found"}).encode())))
        self.repondre("/api/ps", PS_VIDE)
        res = ollama_memoire.charger("zz")
        self.assertFalse(res["ok"])
        self.assertIn("zz", res["message"])

    def test_ollama_injoignable_pendant_un_chargement(self):
        self.echouer("/api/generate", OSError("connexion refusee"))
        self.echouer("/api/ps", OSError("connexion refusee"))
        res = ollama_memoire.charger("moondream:latest")
        self.assertFalse(res["ok"])
        self.assertIsNone(res["charges"])
        self.assertIn("Ollama", res["message"])


class DechargerTest(_OllamaBouchonne):
    def test_decharger_poste_keep_alive_zero(self):
        self.repondre("/api/generate", {"done": True, "done_reason": "unload"})
        self.repondre("/api/ps", PS_VIDE)
        ollama_memoire.decharger("moondream:latest")
        _, corps, _ = self.appels[0]
        self.assertEqual(corps["model"], "moondream:latest")
        self.assertEqual(corps["keep_alive"], 0)

    def test_decharger_rend_l_etat_relu_et_non_le_succes_suppose(self):
        """LE test de ce module. Mesuré sur un vrai Ollama : éjecter pendant une
        génération répond 200 `done_reason: "unload"` ET NE DÉCHARGE RIEN — la
        génération en vol se termine et réinstalle le modèle. Le 200 ment.

        On rejoue exactement ça : Ollama accepte, puis `/api/ps` montre le modèle
        toujours résident. Le résultat doit dire « toujours chargé », sinon
        l'interface annoncerait un déchargement qui n'a pas eu lieu."""
        self.repondre("/api/generate", {"done": True, "done_reason": "unload"})
        self.repondre("/api/ps", PS_MOONDREAM)     # toujours là !

        res = ollama_memoire.decharger("moondream:latest")

        self.assertFalse(res["ok"], "un 200 d'Ollama ne prouve pas le déchargement")
        self.assertIn("moondream:latest", [m["id"] for m in res["charges"]])
        self.assertIn("génération", res["message"].lower())

    def test_decharger_effectif_rend_ok(self):
        self.repondre("/api/generate", {"done": True, "done_reason": "unload"})
        self.repondre("/api/ps", PS_VIDE)
        res = ollama_memoire.decharger("moondream:latest")
        self.assertTrue(res["ok"])
        self.assertEqual(res["charges"], [])

    def test_ollama_injoignable_pendant_une_ejection(self):
        self.echouer("/api/generate", OSError("connexion refusee"))
        self.echouer("/api/ps", OSError("connexion refusee"))
        res = ollama_memoire.decharger("moondream:latest")
        self.assertFalse(res["ok"])
        self.assertIsNone(res["charges"])


class InstalleEtChargeSontDisjointsTest(_OllamaBouchonne):
    """`/api/tags` et `/api/ps` ne répondent pas à la même question.

    Sept modèles installés et zéro chargé est l'état normal d'une machine au
    repos. Ce test existe pour qu'une refonte qui « simplifierait » en dérivant
    l'un de l'autre devienne rouge.
    """

    def test_un_modele_installe_n_est_pas_charge_pour_autant(self):
        self.repondre("/api/ps", PS_VIDE)
        with mock.patch("core.models.get_ollama_installed",
                        return_value=["moondream:latest", "qwen3:8b"]):
            from core.models import get_ollama_installed
            installes = get_ollama_installed()
        charges = ollama_memoire.lister_charges()
        self.assertEqual(len(installes), 2)
        self.assertEqual(charges, [])


class SondeOrchestrateurTest(_OllamaBouchonne):
    """`core/orchestrator._ollama_ok` visait `http://localhost:11434` EN DUR.

    Bug latent, et de la pire espèce : un `OLLAMA_HOST` personnalisé laissait
    toute l'application fonctionner — le chat, `/models`, le RAG passent par
    `core.llm` — tout en rendant cette sonde-ci systématiquement fausse.
    L'orchestrateur écartait alors le palier local sur une machine où Ollama
    tournait parfaitement. Une panne partielle se voit moins qu'une franche.

    Le test porte sur l'URL RÉELLEMENT appelée, pas sur la présence d'un import :
    une constante recopiée passerait le second et pas le premier.
    """

    def test_la_sonde_suit_l_hote_normalise(self):
        from core import orchestrator
        self.repondre("/api/tags", {"models": []})
        self.assertTrue(orchestrator._ollama_ok())
        self.assertEqual(self.appels[0][0], f"{ollama_memoire.hote_ollama()}/api/tags")

    def test_ollama_absent_rend_faux_sans_lever(self):
        from core import orchestrator
        self.echouer("/api/tags", OSError("connexion refusee"))
        self.assertFalse(orchestrator._ollama_ok())


#: `/api/tags` d'Ollama 0.33.3, recopié tel quel. Le champ `capabilities` y est
#: — mesuré, pas lu dans une doc — donc annoter la liste ne coûte AUCUNE requête
#: supplémentaire : c'est le même appel que `get_ollama_installed` fait déjà.
TAGS_AVEC_CAPACITES = {
    "models": [
        {"model": "qwen2.5:7b", "name": "qwen2.5:7b",
         "capabilities": ["completion", "tools"]},
        {"model": "moondream:latest", "name": "moondream:latest",
         "capabilities": ["completion", "vision"]},
        {"model": "qwen3:8b", "name": "qwen3:8b",
         "capabilities": ["completion", "tools", "thinking"]},
        # Ordre DIFFÉRENT de celui de /api/show pour le même modèle : mesuré sur
        # qwen3.6, où /api/tags rend ['vision','completion',...] et /api/show
        # ['completion','vision',...]. Même ensemble, ordre libre.
        {"model": "qwen3.6:latest", "name": "qwen3.6:latest",
         "capabilities": ["vision", "completion", "tools", "thinking"]},
    ]
}

#: Ollama plus ancien, ou modèle sans le champ : la capacité est INCONNUE, pas
#: absente. Aucune version installée sur ce poste ne produit ce cas — il est
#: donc supposé, et traité dans le sens sûr.
TAGS_SANS_CAPACITES = {"models": [{"model": "vieux:7b", "name": "vieux:7b"}]}


class CapacitesOllamaTest(_OllamaBouchonne):
    """Capacités déclarées par Ollama lui-même (`/api/tags`).

    MESURÉ sur Ollama 0.33.3, sept modèles de six familles : le champ
    `capabilities` est présent partout, et vaut un sous-ensemble de
    `completion` / `tools` / `vision` / `thinking`. `/api/show` rend la même
    chose — **au même ENSEMBLE près, pas à la même liste** : sur `qwen3.6`, les
    deux ordres diffèrent. D'où la comparaison en ensembles ici et dans le code.

    Une seule version d'Ollama est installée sur ce poste : « présent sur toutes
    les versions » n'est donc PAS mesuré, et le champ absent est traité comme
    « on ne sait pas », jamais comme « aucune capacité ».
    """

    def test_les_capacites_viennent_de_api_tags_sans_requete_de_plus(self):
        self.repondre("/api/tags", TAGS_AVEC_CAPACITES)
        caps = ollama_memoire.capacites_installees()
        self.assertEqual(len(self.appels), 1, "une seule requête doit suffire")
        self.assertTrue(self.appels[0][0].endswith("/api/tags"))
        self.assertEqual(caps["qwen2.5:7b"], {"completion", "tools"})

    def test_les_trois_capacites_sont_lues(self):
        self.repondre("/api/tags", TAGS_AVEC_CAPACITES)
        caps = ollama_memoire.capacites_installees()
        self.assertEqual(caps["moondream:latest"], {"completion", "vision"})
        self.assertEqual(caps["qwen3:8b"], {"completion", "tools", "thinking"})
        self.assertEqual(caps["qwen3.6:latest"],
                         {"completion", "vision", "tools", "thinking"})

    def test_l_ordre_du_tableau_ne_compte_pas(self):
        """`qwen3.6` rend un ordre différent selon l'endpoint interrogé. Une
        comparaison de LISTES passerait aujourd'hui et casserait au prochain
        modèle qu'Ollama réordonne."""
        self.repondre("/api/tags", TAGS_AVEC_CAPACITES)
        caps = ollama_memoire.capacites_installees()
        self.assertIsInstance(caps["qwen3.6:latest"], set)

    def test_champ_absent_rend_none_pas_un_ensemble_vide(self):
        """INCONNU n'est pas ABSENT. Un ensemble vide dirait « ce modèle ne sait
        rien faire » ; `None` dit « Ollama ne l'a pas déclaré »."""
        self.repondre("/api/tags", TAGS_SANS_CAPACITES)
        self.assertIsNone(ollama_memoire.capacites_installees()["vieux:7b"])

    def test_ollama_injoignable_rend_none(self):
        self.echouer("/api/tags", OSError("connexion refusee"))
        self.assertIsNone(ollama_memoire.capacites_installees())


class TroisEtatsTest(unittest.TestCase):
    """oui / non / on ne sait pas — la distinction doit survivre au transport.

    C'est la leçon de `charges: null` (#26), rejouée sur les capacités : un
    `False` affirme « ce modèle ne le fait pas », un `None` dit « personne ne
    l'a mesuré ». Les confondre ferait afficher une absence comme un fait.
    """

    def test_capacite_declaree_donne_vrai_ou_faux(self):
        caps = ollama_memoire.decrire_capacites({"completion", "tools"})
        self.assertTrue(caps["outils"])
        self.assertFalse(caps["vision"], "déclaré et absent = un FAIT négatif")
        self.assertFalse(caps["raisonnement"])

    def test_rien_de_declare_donne_trois_none(self):
        caps = ollama_memoire.decrire_capacites(None)
        self.assertEqual(caps, {"outils": None, "vision": None, "raisonnement": None})

    def test_les_trois_cles_sont_toujours_presentes(self):
        """Émises explicitement, y compris à `None` : une clé absente arrive en
        `undefined` côté TypeScript, indistinguable d'un `null`, et les
        normaliseurs du frontend (§8) l'écraseraient vers « absent »."""
        for source in (None, set(), {"vision"}):
            with self.subTest(source=source):
                self.assertEqual(sorted(ollama_memoire.decrire_capacites(source)),
                                 ["outils", "raisonnement", "vision"])


class SurfaceHttpTest(_OllamaBouchonne):
    """Les trois routes, vues du client.

    Le contrat qui compte ici est le SILENCE sur Ollama absent : la machine sans
    Ollama est le cas nominal, `charges: null` est une réponse **200**, pas une
    erreur. Un 500 ferait afficher un bandeau rouge à quelqu'un dont
    l'installation n'a rien d'anormal.
    """

    def setUp(self):
        super().setUp()
        os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from fastapi.testclient import TestClient
        import main
        from core.auth import get_api_token
        self.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54321))
        self.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    def test_loaded_rend_la_liste(self):
        self.repondre("/api/ps", PS_MOONDREAM)
        r = self.client.get("/models/loaded", headers=self.entetes)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([m["id"] for m in r.json()["charges"]], ["moondream:latest"])

    def test_ollama_absent_repond_200_avec_null(self):
        self.echouer("/api/ps", OSError("connexion refusee"))
        r = self.client.get("/models/loaded", headers=self.entetes)
        self.assertEqual(r.status_code, 200, "Ollama absent n'est pas une panne du backend")
        self.assertIsNone(r.json()["charges"])

    def test_rien_de_charge_repond_une_liste_vide(self):
        self.repondre("/api/ps", PS_VIDE)
        r = self.client.get("/models/loaded", headers=self.entetes)
        self.assertEqual(r.json()["charges"], [])

    def test_load_transmet_le_modele(self):
        self.repondre("/api/generate", {"done": True, "done_reason": "load"})
        self.repondre("/api/ps", PS_MOONDREAM)
        r = self.client.post("/models/load", json={"model": "moondream:latest"},
                             headers=self.entetes)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_unload_toujours_charge_repond_200_mais_ok_faux(self):
        """Pas un code d'erreur HTTP : la requête a bien été traitée, c'est le
        RÉSULTAT qui est « non ». Un 4xx/5xx ici ferait chercher une panne."""
        self.repondre("/api/generate", {"done": True, "done_reason": "unload"})
        self.repondre("/api/ps", PS_MOONDREAM)
        r = self.client.post("/models/unload", json={"model": "moondream:latest"},
                             headers=self.entetes)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertIn("génération", r.json()["message"].lower())

    def test_le_token_est_exige(self):
        self.repondre("/api/ps", PS_VIDE)
        self.assertEqual(self.client.get("/models/loaded").status_code, 401)


class CapacitesDansModelsTest(_OllamaBouchonne):
    """`GET /models` porte les capacités, avec une SOURCE par fournisseur.

    Trois régimes, et ils ne se valent pas :

    * **Ollama** — capacités déclarées par le serveur lui-même. Les trois états
      sont connus : une capacité absente de la liste déclarée est un FAIT.
    * **FLM** — pas de source à l'échelle d'une liste. `/v1/models` ne rend que
      `{created, id, object, owned_by}` (mesuré). Seule la vision est connue,
      et par un registre TENU À LA MAIN qui existe déjà dans le dépôt
      (`FLM_MODELS_STATIC`, flag `vision`) — pas une déduction sur le nom.
    * **LM Studio** — rien de mesurable ici (serveur éteint sur ce poste). Les
      trois sont `None`.

    **Aucune heuristique de nom.** « vl » ne vaut pas vision, « r1 » ne vaut pas
    raisonnement : une icône est lue comme un fait, et une supposition affichée
    comme un fait est un mensonge. Là où il n'y a pas de source, il y a `None`.
    """

    def setUp(self):
        super().setUp()
        os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from fastapi.testclient import TestClient
        import main
        from core.auth import get_api_token
        self.main = main
        self.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54321))
        self.entetes = {"Authorization": f"Bearer {get_api_token()}"}

    def _modeles(self, **surcharges):
        """`GET /models` avec les sondes externes neutralisées."""
        defauts = {
            "get_ollama_installed": ["qwen2.5:7b", "moondream:latest"],
            "get_lmstudio_installed": [],
            "check_flm": False,
        }
        defauts.update(surcharges)
        with mock.patch.object(self.main, "get_ollama_installed",
                               return_value=defauts["get_ollama_installed"]), \
             mock.patch.object(self.main, "get_lmstudio_installed",
                               return_value=defauts["get_lmstudio_installed"]), \
             mock.patch.object(self.main, "check_flm",
                               return_value=defauts["check_flm"]):
            return self.client.get("/models", headers=self.entetes).json()

    def test_ollama_porte_des_capacites_connues(self):
        self.repondre("/api/tags", TAGS_AVEC_CAPACITES)
        par_id = {m["id"]: m for m in self._modeles()["local"]}
        self.assertEqual(par_id["qwen2.5:7b"]["capacites"],
                         {"outils": True, "vision": False, "raisonnement": False})
        self.assertEqual(par_id["moondream:latest"]["capacites"],
                         {"outils": False, "vision": True, "raisonnement": False})

    def test_ollama_injoignable_rend_trois_inconnues(self):
        """Pas de source jointe = pas de fait. Surtout pas trois `False`, qui
        affirmeraient que ces modèles ne savent rien faire."""
        self.echouer("/api/tags", OSError("connexion refusee"))
        for m in self._modeles()["local"]:
            self.assertEqual(m["capacites"],
                             {"outils": None, "vision": None, "raisonnement": None})

    def test_lmstudio_a_trois_inconnues(self):
        self.repondre("/api/tags", TAGS_AVEC_CAPACITES)
        self.echouer("/v1/models", OSError("eteint"))
        rep = self._modeles(get_lmstudio_installed=["llama-3.1-8b-instruct"])
        for m in rep["local_lmstudio"]:
            self.assertEqual(m["capacites"],
                             {"outils": None, "vision": None, "raisonnement": None})

    def test_les_trois_cles_existent_toujours_dans_le_json(self):
        """Émises même à `null` : une clé absente arrive `undefined` côté
        TypeScript et se confond avec « pas de capacité »."""
        self.repondre("/api/tags", TAGS_AVEC_CAPACITES)
        for m in self._modeles()["local"]:
            self.assertEqual(sorted(m["capacites"]),
                             ["outils", "raisonnement", "vision"])

    def test_les_modeles_cloud_ne_portent_pas_le_champ(self):
        """Hors périmètre de ce lot : le champ est ABSENT plutôt que `null`
        partout, ce qui ferait apparaître un marqueur « inconnu » sur chaque
        ligne cloud — beaucoup de bruit pour une question que personne n'a
        posée. Le distinguer d'un `null` est délibéré."""
        self.repondre("/api/tags", TAGS_AVEC_CAPACITES)
        rep = self._modeles()
        for models in rep["cloud"].values():
            for m in models:
                self.assertNotIn("capacites", m)


class AucunAppelReseauReelTest(_OllamaBouchonne):
    """Aucun appel ne sort du process, même sur une URL qu'aucun test n'a prévue.

    Sans cette propriété, un test oublié partirait sur le vrai Ollama du poste de
    l'auteur et passerait au vert — puis échouerait en CI, où il n'y en a aucun.
    C'est l'écart local/CI de §8, rejoué sur le réseau.

    Le bouchon LÈVE sur une URL imprévue, et `lister_charges` absorbe l'échec en
    « injoignable » — c'est son contrat, il ne doit jamais lever vers l'appelant.
    Ce test ne peut donc PAS s'écrire en `assertRaises` : on vérifie que l'appel
    a été intercepté (donc jamais parti) et refusé. Écrit d'abord en
    `assertRaises`, il échouait — le module faisait exactement ce qu'il fallait.
    """

    def test_urlopen_est_bien_remplace_pour_toute_la_suite(self):
        self.assertIsInstance(ollama_memoire.urllib.request.urlopen, mock.MagicMock)

    def test_une_url_non_prevue_est_interceptee_et_refusee(self):
        self.assertIsNone(ollama_memoire.lister_charges())
        self.assertEqual(len(self.appels), 1, "l'appel n'a pas été intercepté")
        self.assertTrue(self.appels[0][0].endswith("/api/ps"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
