#!/usr/bin/env python3
"""Connexions aux serveurs LOCAUX (LM Studio, Ollama) : adresse, réutilisation,
contexte chargé gardé en cache.

Mesuré le 2026-10-19 sur ce poste, pendant le diagnostic d'un premier tour
lent (#80) :

  * `localhost` résout d'abord `::1` ; LM Studio n'écoute qu'en IPv4
    (`127.0.0.1:1234`), donc chaque NOUVELLE connexion attend ~2 s avant de
    retomber sur IPv4 — 2,09 s contre 0,03 s (urllib), 2,6 s contre 0,5 s
    (httpx). Ollama, qui écoute ici sur `0.0.0.0` ET `[::]`, n'en souffre
    pas (0,00 à 0,06 s par les deux noms) ; un Ollama à sa configuration par
    défaut (IPv4 local seulement) y serait exposé de la même façon.
  * un client OpenAI NEUF à chaque appel (`_openai_client`) : une nouvelle
    connexion, donc les 2 s, à chaque tour ;
  * la sonde du contexte chargé (`fenetre_de`) refaite à chaque tour d'URL :
    encore 2 s.

Ce fichier verrouille les trois parades : `127.0.0.1` par défaut (toujours
surchargeable par `OLLAMA_HOST` / `LMSTUDIO_HOST`), un client réutilisé par
fournisseur, et un contexte chargé relu seulement quand le modèle actif change
ou qu'une requête échoue.

Usage :
    python test_connexions_locales.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — avant tout import de core.*

from core import fenetre_contexte  # noqa: E402
from core import llm as module_llm  # noqa: E402
from core.llm import LLMEngine  # noqa: E402
from test_chat_lecture_url import _URL, _Base, routeur_chat  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


class HotesParDefautTest(unittest.TestCase):
    def test_lmstudio_127_0_0_1_par_defaut(self):
        self.assertEqual(module_llm.hote_lmstudio(""), "http://127.0.0.1:1234")

    def test_ollama_127_0_0_1_par_defaut(self):
        self.assertEqual(module_llm.hote_ollama(""), "http://127.0.0.1:11434")

    def test_toujours_configurables(self):
        self.assertEqual(module_llm.hote_lmstudio("http://192.168.1.20:4321"), "http://192.168.1.20:4321")
        self.assertEqual(module_llm.hote_lmstudio("machine"), "http://machine:1234")
        self.assertEqual(module_llm.hote_ollama("http://autre:11434"), "http://autre:11434")
        self.assertEqual(module_llm.hote_ollama("autre"), "http://autre:11434")

    def test_ollama_ecoute_tout_on_parle_a_127_0_0_1(self):
        """`OLLAMA_HOST=0.0.0.0` est une adresse d'ÉCOUTE (incident connu) :
        le client parle à la boucle locale IPv4, plus à `localhost`."""
        self.assertEqual(module_llm.hote_ollama("0.0.0.0"), "http://127.0.0.1:11434")
        self.assertEqual(module_llm.hote_ollama("http://0.0.0.0:11434"), "http://127.0.0.1:11434")

    def test_valeurs_du_module(self):
        """Sans variable d'environnement (cas de la CI), le module les applique."""
        if not os.environ.get("LMSTUDIO_HOST"):
            self.assertEqual(module_llm.lmstudio_host, "http://127.0.0.1:1234")
        if not os.environ.get("OLLAMA_HOST"):
            self.assertEqual(module_llm.ollama_host, "http://127.0.0.1:11434")


class ClientReutiliseTest(unittest.TestCase):
    def setUp(self):
        self.moteur = LLMEngine(config_path=_CONFIG)

    def test_meme_client_d_un_appel_a_l_autre(self):
        self.assertIs(self.moteur._openai_client("lmstudio"), self.moteur._openai_client("lmstudio"))

    def test_un_client_par_fournisseur(self):
        self.assertIsNot(self.moteur._openai_client("lmstudio"), self.moteur._openai_client("flm"))

    def test_cle_changee_nouveau_client(self):
        """Une clé saisie dans Réglages doit prendre effet sans redémarrage."""
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "cle-a"}):
            a = self.moteur._openai_client("mistral")
            self.assertIs(self.moteur._openai_client("mistral"), a)
        with mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "cle-b"}):
            self.assertIsNot(self.moteur._openai_client("mistral"), a)


class ContexteChargeEnCacheTest(unittest.TestCase):
    def setUp(self):
        fenetre_contexte.oublier_contexte_charge()
        self.addCleanup(fenetre_contexte.oublier_contexte_charge)
        self.sonde = mock.patch.object(fenetre_contexte, "fenetre_de",
                                       return_value=(8192, "lmstudio /api/v1/models"))
        self.fenetre = self.sonde.start()
        self.addCleanup(self.sonde.stop)

    def test_meme_modele_une_seule_sonde(self):
        for _ in range(3):
            self.assertEqual(fenetre_contexte.contexte_charge("lmstudio:m"),
                             (8192, "lmstudio /api/v1/models"))
        self.assertEqual(self.fenetre.call_count, 1)

    def test_modele_actif_change_relu(self):
        fenetre_contexte.contexte_charge("lmstudio:m")
        fenetre_contexte.contexte_charge("lmstudio:autre")
        self.assertEqual(self.fenetre.call_count, 2)

    def test_inconnu_jamais_garde(self):
        """Un modèle pas encore chargé rend `None` ; le garder figerait le
        plancher alors qu'il sera chargé au tour suivant."""
        self.fenetre.return_value = (None, None)
        fenetre_contexte.contexte_charge("lmstudio:m")
        fenetre_contexte.contexte_charge("lmstudio:m")
        self.assertEqual(self.fenetre.call_count, 2)

    def test_oublier_force_la_relecture(self):
        fenetre_contexte.contexte_charge("lmstudio:m")
        fenetre_contexte.oublier_contexte_charge()
        fenetre_contexte.contexte_charge("lmstudio:m")
        self.assertEqual(self.fenetre.call_count, 2)


class ContexteChargeDansLeChatTest(_Base):
    def setUp(self):
        super().setUp()
        fenetre_contexte.oublier_contexte_charge()
        self.addCleanup(fenetre_contexte.oublier_contexte_charge)
        patcher = mock.patch.object(fenetre_contexte, "fenetre_de",
                                    return_value=(8192, "lmstudio /api/v1/models"))
        self.fenetre = patcher.start()
        self.addCleanup(patcher.stop)

    def test_deux_tours_d_url_une_seule_sonde(self):
        self._envoyer(f"Résume {_URL}")
        self._envoyer(f"Et encore {_URL}")
        self.assertEqual(self.fenetre.call_count, 1)

    def test_requete_en_echec_relit_au_tour_suivant(self):
        self._envoyer(f"Résume {_URL}")

        def stream_en_echec(messages, model=None, raisonnement=True, **kw):
            raise RuntimeError("context length exceeded")
        routeur_chat.llm.stream = stream_en_echec
        self._envoyer(f"Résume {_URL}")
        self.assertEqual(self.fenetre.call_count, 2)
        self._envoyer(f"Résume {_URL}")
        self.assertEqual(self.fenetre.call_count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
