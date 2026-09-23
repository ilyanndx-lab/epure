#!/usr/bin/env python3
"""Fenêtre de contexte d'un modèle : la règle est « None plutôt qu'un défaut ».

Ce fichier verrouille UNE propriété, et c'est la seule qui compte : **le module
ne fabrique jamais de fenêtre**. Il la lit chez le fournisseur, ou il rend
`None`. Un défaut inventé ici — `n_ctx: 4096`, une fenêtre devinée depuis le nom
du modèle, la valeur d'un autre modèle de la même liste — ne produirait pas une
imprécision mais un chiffre faux et crédible, affiché à l'utilisateur au moment
précis où il décide s'il peut encore poser sa question.

Deux familles de cas, donc :

* **rien n'est su** → `(None, None)`, et pour les fournisseurs mesurés comme
  muets (Cerebras, NVIDIA, DeepSeek), sans même un appel réseau ;
* **le fournisseur a répondu** → sa valeur, plus le libellé de sa SOURCE — qui
  doit être exacte (Gemini passe par le SDK, pas par `/v1/models`).

AUCUN TEST NE TOUCHE LE RÉSEAU. `lister_charges` et `_http_json` sont remplacés
pour toute la classe ; un appel qui passerait au travers lève au lieu de partir
sur le poste de l'auteur, où un Ollama qui tourne ferait passer le test au vert
pour de mauvaises raisons.

Usage :
    python test_fenetre_contexte.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — AVANT tout import de core.*

from core import fenetre_contexte  # noqa: E402


class _SondeBouchonnee(unittest.TestCase):
    """Aucun accès réseau, et le mémo est vidé entre deux tests.

    Le mémo est un état de MODULE (une fenêtre cloud ne change pas en cours de
    session) : sans ce nettoyage, un test qui remplit le cache rendrait le
    suivant vert pour la mauvaise raison — la sonde ne serait même pas appelée.
    """

    def setUp(self):
        fenetre_contexte._MEMO.clear()
        self.addCleanup(fenetre_contexte._MEMO.clear)
        self.charges = mock.patch.object(
            fenetre_contexte, "lister_charges", return_value=[]
        )
        self.lister = self.charges.start()
        self.addCleanup(self.charges.stop)
        self.http = mock.patch.object(fenetre_contexte, "_http_json")
        self.requete = self.http.start()
        self.addCleanup(self.http.stop)


class DecoupageTest(unittest.TestCase):
    """L'identifiant composite porte le fournisseur, mais un nom de modèle
    Ollama contient lui-même un deux-points — couper sur le premier `:` sans
    vérifier le préfixe lirait `qwen2.5` comme un fournisseur."""

    def test_modele_ollama_sans_prefixe(self):
        self.assertEqual(
            fenetre_contexte._decouper("qwen2.5:7b"), ("ollama", "qwen2.5:7b"))

    def test_prefixe_cloud(self):
        self.assertEqual(
            fenetre_contexte._decouper("mistral:mistral-small-latest"),
            ("mistral", "mistral-small-latest"))

    def test_lmstudio_garde_son_prefixe_et_son_modele_a_deux_points(self):
        self.assertEqual(
            fenetre_contexte._decouper("lmstudio:qwen2.5:7b"),
            ("lmstudio", "qwen2.5:7b"))

    def test_premier_segment_inconnu_reste_ollama(self):
        """`qwen2.5` n'est pas un fournisseur : c'est la suite du nom."""
        self.assertEqual(
            fenetre_contexte._decouper("qwen2.5-coder:7b"),
            ("ollama", "qwen2.5-coder:7b"))


class OllamaTest(_SondeBouchonnee):
    def test_modele_charge_rend_sa_fenetre_runtime(self):
        self.lister.return_value = [
            {"id": "moondream:latest", "fenetre_contexte": 2048},
        ]
        self.assertEqual(
            fenetre_contexte.fenetre_de("moondream:latest"), (2048, "ollama /api/ps"))

    def test_modele_non_charge_rend_none(self):
        """`/api/ps` ne liste que les RÉSIDENTS. Un modèle pas encore chargé n'a
        pas de fenêtre connue — c'est le cas normal d'un premier message, et
        l'indicateur doit disparaître plutôt que d'afficher autre chose."""
        self.lister.return_value = []
        self.assertEqual(fenetre_contexte.fenetre_de("qwen2.5:7b"), (None, None))

    def test_ollama_injoignable_rend_none_sans_lever(self):
        """`lister_charges` rend `None` quand Ollama ne répond pas (cas nominal
        d'une machine sans Ollama). Une exception ici remonterait en 500 sur un
        endpoint d'affichage."""
        self.lister.return_value = None
        self.assertEqual(fenetre_contexte.fenetre_de("qwen2.5:7b"), (None, None))

    def test_champ_absent_ne_devient_pas_une_fenetre(self):
        self.lister.return_value = [{"id": "moondream:latest", "fenetre_contexte": None}]
        self.assertEqual(fenetre_contexte.fenetre_de("moondream:latest"), (None, None))

    def test_la_fenetre_ollama_n_est_pas_memoisee(self):
        """Contrairement au cloud, cette valeur décrit un ÉTAT : éjecter puis
        recharger un modèle avec un autre `num_ctx` la change. Une mémo de
        session afficherait l'ancienne jusqu'au redémarrage."""
        self.lister.return_value = [{"id": "m", "fenetre_contexte": 4096}]
        fenetre_contexte.fenetre_de("m")
        self.lister.return_value = [{"id": "m", "fenetre_contexte": 8192}]
        self.assertEqual(fenetre_contexte.fenetre_de("m")[0], 8192)


class FournisseursMuctsTest(_SondeBouchonnee):
    """Cerebras, NVIDIA et DeepSeek ont été MESURÉS le 2026-09-20 : leurs items
    de `/v1/models` ne portent que `id`, `object`, `owned_by` (et `created` pour
    les deux premiers). Aucun équivalent d'une fenêtre. Ils ne doivent donc pas
    être appelés — une requête sortante qui ne peut rien rapporter est un coût
    sans contrepartie."""

    def test_les_trois_mucts_rendent_none_sans_appel_reseau(self):
        for modele in ("cerebras:llama3.1-8b", "nvidia/nemotron-3-super-120b-a12b",
                       "deepseek:deepseek-flash"):
            with self.subTest(modele=modele):
                self.assertEqual(fenetre_contexte.fenetre_de(modele), (None, None))
        self.requete.assert_not_called()

    def test_groq_non_mesure_rend_none_sans_appel_reseau(self):
        """Sa clé répond 401 `expired_api_key` (constaté le 2026-09-20) : le
        champ n'a donc PAS pu être mesuré. Ne pas le brancher au jugé — une
        documentation n'est pas une mesure."""
        self.assertEqual(
            fenetre_contexte.fenetre_de("groq:openai/gpt-oss-120b"), (None, None))
        self.requete.assert_not_called()

    def test_lmstudio_et_flm_rendent_none(self):
        for modele in ("lmstudio:qwen2.5:7b", "flm:qwen3:4b"):
            with self.subTest(modele=modele):
                self.assertEqual(fenetre_contexte.fenetre_de(modele), (None, None))
        self.requete.assert_not_called()


class MistralTest(_SondeBouchonnee):
    """`_fenetre_mistral` rend `None` SANS appel HTTP quand `MISTRAL_API_KEY`
    est vide. La clé est donc posée ici, factice : sans elle, ces tests ne
    passaient que sur un poste dont le `backend/.env` porte une vraie clé
    (chargée par `core.llm` à l'import) — rouges en CI et dans tout worktree,
    verts sur le poste de dev, sans que rien ne le signale."""

    def setUp(self):
        super().setUp()
        cle = mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "cle-factice-de-test"})
        cle.start()
        self.addCleanup(cle.stop)

    def test_la_fenetre_du_modele_demande_est_lue(self):
        """La liste Mistral sert 46 modèles aux fenêtres DIFFÉRENTES (le premier
        annonce 256 000, `mistral-small-latest` 262 144) : c'est celui qui est
        demandé qu'il faut lire, pas le premier de la liste."""
        self.requete.return_value = {"data": [
            {"id": "un-autre-modele", "max_context_length": 256000},
            {"id": "mistral-small-latest", "max_context_length": 262144},
        ]}
        self.assertEqual(
            fenetre_contexte.fenetre_de("mistral:mistral-small-latest"),
            (262144, "mistral /v1/models"))

    def test_modele_absent_de_la_liste_rend_none(self):
        self.requete.return_value = {"data": [{"id": "autre", "max_context_length": 1000}]}
        self.assertEqual(
            fenetre_contexte.fenetre_de("mistral:mistral-small-latest"), (None, None))

    def test_un_succes_est_memoise(self):
        self.requete.return_value = {"data": [{"id": "m", "max_context_length": 32000}]}
        fenetre_contexte.fenetre_de("mistral:m")
        fenetre_contexte.fenetre_de("mistral:m")
        self.assertEqual(self.requete.call_count, 1)

    def test_sans_cle_aucun_appel_reseau(self):
        """Le cas que la clé factice du `setUp` masquerait sinon : sans
        `MISTRAL_API_KEY`, pas de requête et pas de fenêtre — jamais un appel
        anonyme vers l'API."""
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": ""}):
            self.assertEqual(fenetre_contexte.fenetre_de("mistral:m"), (None, None))
        self.requete.assert_not_called()

    def test_un_echec_n_est_pas_memoise(self):
        """Mémoriser un échec réseau figerait pour toute la session une absence
        qui n'était que passagère."""
        self.requete.side_effect = OSError("reseau indisponible")
        self.assertEqual(fenetre_contexte.fenetre_de("mistral:m"), (None, None))
        self.requete.side_effect = None
        self.requete.return_value = {"data": [{"id": "m", "max_context_length": 32000}]}
        self.assertEqual(fenetre_contexte.fenetre_de("mistral:m")[0], 32000)


class RobustesseTest(_SondeBouchonnee):
    def test_une_sonde_qui_leve_ne_remonte_pas(self):
        """Un indicateur qui disparaît est un désagrément ; une exception qui
        remonte ferait tomber le chat pour une donnée d'affichage."""
        self.requete.side_effect = RuntimeError("SDK casse")
        self.assertEqual(fenetre_contexte.fenetre_de("mistral:m"), (None, None))

    def test_une_valeur_non_entier_est_refusee(self):
        """Un fournisseur qui renverrait `"32000"` ou `null` ne doit pas
        produire un dénominateur inutilisable — la comparaison `> 0` sur une
        chaîne lèverait plus loin, dans l'interface."""
        for brute in ("32000", None, 0, -1, 32000.5):
            with self.subTest(valeur=brute):
                fenetre_contexte._MEMO.clear()
                self.requete.return_value = {"data": [{"id": "m", "max_context_length": brute}]}
                self.assertEqual(fenetre_contexte.fenetre_de("mistral:m"), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
