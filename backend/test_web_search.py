#!/usr/bin/env python3
"""Tests pour perform_web_search.

Par défaut, exécute des tests *offline* avec le HTTP mocké (aucun accès
réseau) : Instant Answer, fallback HTML, cache LRU et chemin d'erreur.

Usage :
    python test_web_search.py            # tests offline (unittest)
    python test_web_search.py --live     # vraie recherche réseau (démo)
"""

import os
import sys
import unittest
from unittest import mock

# S'assurer de pouvoir importer main depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

# perform_web_search a migré dans le module chat (modules/chat/router.py).
from modules.chat import router as chat_router
perform_web_search = chat_router.perform_web_search


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


_INSTANT_JSON = (
    '{"Abstract": "Python est un langage de programmation.",'
    ' "AbstractSource": "Wikipedia",'
    ' "AbstractURL": "https://fr.wikipedia.org/wiki/Python",'
    ' "RelatedTopics": []}'
)
_EMPTY_JSON = '{"Abstract": "", "RelatedTopics": []}'
_HTML_BODY = """
<div class="result results_links">
  <a class="result__a" href="x">Python (langage)</a>
  <a class="result__snippet">Python est un langage interpr&eacute;t&eacute; multiparadigme.</a>
</div>
<div class="result results_links">
  <a class="result__a" href="y">Tutoriel Python</a>
  <a class="result__snippet">Apprendre les bases de Python.</a>
</div>
"""


def _urlopen_router(responses):
    """Construit un faux urlopen choisissant la réponse selon l'URL appelée.

    `responses` mappe un fragment d'URL vers soit un corps (str), soit une
    Exception à lever (simulation d'erreur réseau).
    """

    def _fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        for needle, resp in responses.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return _FakeResponse(resp)
        raise AssertionError(f"URL inattendue dans le test : {url}")

    return _fake


class WebSearchOfflineTest(unittest.TestCase):
    def setUp(self):
        # Le cache est un état global au module : repartir propre à chaque test.
        chat_router._web_search_cache.clear()

    def test_empty_query_returns_empty(self):
        self.assertEqual(perform_web_search("   "), "")

    def test_instant_answer(self):
        fake = _urlopen_router({"api.duckduckgo.com": _INSTANT_JSON})
        with mock.patch.object(chat_router.urllib.request, "urlopen", side_effect=fake):
            out = perform_web_search("python instant")
        self.assertIn("Instant Answer", out)
        self.assertIn("langage de programmation", out)
        self.assertIn("Wikipedia", out)

    def test_html_fallback(self):
        fake = _urlopen_router(
            {
                "api.duckduckgo.com": _EMPTY_JSON,  # Instant Answer vide
                "html.duckduckgo.com": _HTML_BODY,  # → déclenche le fallback
            }
        )
        with mock.patch.object(chat_router.urllib.request, "urlopen", side_effect=fake):
            out = perform_web_search("python fallback")
        self.assertIn("DuckDuckGo HTML", out)
        self.assertIn("langage interprété", out)  # entité HTML déséchappée
        self.assertIn("Tutoriel Python", out)

    def test_cache_avoids_second_fetch(self):
        fake = mock.Mock(side_effect=_urlopen_router({"api.duckduckgo.com": _INSTANT_JSON}))
        with mock.patch.object(chat_router.urllib.request, "urlopen", fake):
            first = perform_web_search("python cache")
            calls_after_first = fake.call_count
            second = perform_web_search("python cache")
        self.assertEqual(first, second)
        self.assertEqual(calls_after_first, 1)
        self.assertEqual(fake.call_count, 1)  # 2e appel servi par le cache

    def test_network_error_returns_message(self):
        err = chat_router.urllib.error.URLError("offline")
        fake = _urlopen_router({"api.duckduckgo.com": err, "html.duckduckgo.com": err})
        with mock.patch.object(chat_router.urllib.request, "urlopen", side_effect=fake):
            out = perform_web_search("python erreur")
        self.assertTrue(out.startswith("Erreur de recherche web"), out)


def _live():
    query = "Python programming"
    print(f"Query: {query}")
    print("Result:")
    print(perform_web_search(query))


if __name__ == "__main__":
    if "--live" in sys.argv:
        _live()
    else:
        unittest.main()
