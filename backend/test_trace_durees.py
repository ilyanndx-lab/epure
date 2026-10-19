#!/usr/bin/env python3
"""Durées dans la trace de recherche et de lecture d'URL — diagnostic d'un
premier tour à 146 s (mesuré pendant #78 : 2 minutes passées DANS Épure entre
la mémoire et l'envoi au modèle, sans une ligne de journal ; 6,6 s au tour
suivant).

Sans durée par étape, on ne pouvait que deviner. Ce fichier verrouille :

  * `t_ms` sur CHAQUE étape poussée par le routeur : temps écoulé depuis le
    début du tour, donc une chronologie lisible d'un coup d'œil ;
  * pour une lecture d'URL réussie, le détail `fetch_ms` (réseau),
    `extraction_ms` (readability + découpage) et `selection_ms` (embedding
    local + choix des passages — c'est là qu'un premier chargement du modèle
    d'embedding se verrait).

Usage :
    python test_trace_durees.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — avant tout import de core.*

from core import webcontent  # noqa: E402
from test_chat_lecture_url import _URL, _Base  # noqa: E402
from test_lecture_url import _article, _dns_public, _FakeVectorStore, _FausseReponse  # noqa: E402


class DureesLectureTest(unittest.TestCase):
    def _lire(self, question):
        etapes = []
        with mock.patch.object(webcontent.socket, "getaddrinfo", _dns_public), \
             mock.patch.object(webcontent, "_vecteurs_partages", lambda: _FakeVectorStore()), \
             mock.patch.object(webcontent, "_ouvrir",
                               side_effect=lambda req, timeout: _FausseReponse(_article(), url_finale=req.full_url)):
            webcontent.lire_urls(["https://site.example/a"], question, on_etape=etapes.append)
        return next(e for e in etapes if e["etape"] == "lecture_url")

    def test_lecture_reussie_detaille_ses_durees(self):
        etape = self._lire("de quoi parle la page")
        for cle in ("fetch_ms", "extraction_ms", "selection_ms"):
            with self.subTest(cle=cle):
                self.assertIsInstance(etape[cle], int)
                self.assertGreaterEqual(etape[cle], 0)

    def test_selection_mesuree_meme_en_ordre_de_page(self):
        self.assertIsInstance(self._lire("")["selection_ms"], int)


class DureeDeChaqueEtapeTest(_Base):
    def test_chaque_etape_porte_t_ms_croissant(self):
        _trames, conv_id = self._envoyer(f"Résume cette page : {_URL}")
        trace = self._trace(conv_id)
        self.assertTrue(trace)
        t = [e.get("t_ms") for e in trace]
        for etape, valeur in zip(trace, t):
            with self.subTest(etape=etape["etape"]):
                self.assertIsInstance(valeur, int)
        self.assertEqual(t, sorted(t))


if __name__ == "__main__":
    unittest.main(verbosity=2)
