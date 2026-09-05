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
