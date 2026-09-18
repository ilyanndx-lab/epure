#!/usr/bin/env python3
"""Tool-calling natif `web_search` — bout en bout, sur un VRAI Ollama.

Nommé `integration_` et NON `test_` volontairement (cf. CLAUDE.md §2) : la CI
n'a pas Ollama, et ce script fait de VRAIS appels réseau — Ollama en local
ET DuckDuckGo (`core.websearch.rechercher` n'est PAS mocké ici, à la
différence de `test_tool_calling_ollama.py`). `unittest discover -p
'test_*.py'` ne le ramasse donc pas.

Ce que ce fichier PROUVE, que le mocké ne peut pas prouver à lui seul : un
VRAI modèle Ollama, informé du schéma `web_search`, DÉCIDE de l'appeler sur
une question qui le justifie, et la réponse finale intègre bien le résultat
de la vraie recherche (mesuré : citations `[n]` correspondant à de vrais prix
Bitcoin de Binance/CoinMarketCap/CoinGecko, jamais présentes dans les poids
d'un modèle local).

**Question choisie et mesurée** (ce poste, 2026-09-15) — « Quel est le cours
actuel du bitcoin en dollars ? » déclenche `web_search` de façon fiable sur
les deux modèles ci-dessous (2/2 essais chacun), avec un `requete` reformulé
par le modèle lui-même (jamais la question mot pour mot — normal, c'est lui
qui choisit les termes) et 5 résultats à chaque fois. Un prix concret sert
aussi de garde-fou de non-invention : sans recherche, aucun modèle local ne
connaît le cours du jour.

Deux modèles réellement installés sur ce poste (`qwen2.5:7b`, sans
raisonnement — le modèle par défaut de `config.yaml` — et `qwen3:8b`, avec),
pour la même raison que `test_raisonnement_stream.py` distingue les deux
familles : un mécanisme sondé sur un seul modèle ne dit rien de l'autre. Un
modèle absent du poste qui lance ce script fait SKIP ce cas (pas d'échec —
l'installation d'Ollama et son catalogue de modèles ne sont pas le sujet de ce
test), Ollama injoignable fait SKIP toute la classe.

Coût : deux appels réseau réels par test (Ollama + DuckDuckGo + récupération
de pages), 15-25 s chacun mesuré ici. Ne pas ajouter au job `backend` de la CI
— c'est précisément ce que le nom `integration_` évite.

Usage :
    python integration_tool_calling_ollama.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR ; sans effet sur Ollama/le réseau

os.environ.setdefault("EPURE_ALLOWED_HOSTS", "localhost,127.0.0.1,::1")

import ollama  # noqa: E402

from core.llm import LLMEngine, ollama_host  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

#: Déclenche `web_search` de façon fiable sur les deux modèles ci-dessous
#: (mesuré, cf. docstring de module) — et un vrai prix sert de garde-fou
#: contre une réponse inventée sans recherche.
_QUESTION = "Quel est le cours actuel du bitcoin en dollars ? Utilise la recherche web si besoin."


def _modeles_installes() -> set[str]:
    try:
        return {m.model for m in ollama.Client(host=ollama_host).list().models}
    except Exception:
        return set()


def _executer(model: str):
    """Un tour complet, sans rien mocker. Rend (texte, appels_outil, stats)."""
    moteur = LLMEngine(config_path=_CONFIG)
    texte: list[str] = []
    appels_outil: list[dict] = []
    stats = None
    for item in moteur.stream([{"role": "user", "content": _QUESTION}],
                              model=model, outils=["web_search"]):
        if isinstance(item, str):
            texte.append(item)
        elif isinstance(item, dict) and item.get("__tool_call__"):
            appels_outil.append(item)
        elif isinstance(item, dict) and item.get("__stats__"):
            stats = item
        # __reasoning__ ignoré ici : hors sujet de ce test (couvert par
        # test_raisonnement_stream.py), et qwen3:8b en produit.
    return "".join(texte), appels_outil, stats


class ToolCallingBoutEnBoutTest(unittest.TestCase):
    MODELES_INSTALLES: set[str] = set()

    @classmethod
    def setUpClass(cls):
        cls.MODELES_INSTALLES = _modeles_installes()
        if not cls.MODELES_INSTALLES:
            raise unittest.SkipTest(
                f"Ollama injoignable sur {ollama_host} — ce test a besoin d'un "
                "vrai serveur avec des modèles installés."
            )

    def _verifier_round_trip_reel(self, model: str):
        if model not in self.MODELES_INSTALLES:
            self.skipTest(f"{model!r} non installé sur ce poste ({sorted(self.MODELES_INSTALLES)})")

        texte, appels_outil, stats = _executer(model)

        self.assertGreaterEqual(
            len(appels_outil), 1,
            f"{model} n'a jamais appelé web_search sur une question qui le justifie",
        )
        premier = appels_outil[0]
        self.assertEqual(premier["outil"], "web_search")
        self.assertTrue(premier["arguments"].get("requete"), "requête vide envoyée à DuckDuckGo")
        self.assertGreater(
            len(premier["resultats"]), 0,
            "la vraie recherche DuckDuckGo n'a rendu aucun résultat — "
            "réseau indisponible, ou DuckDuckGo a changé de structure (cf. RechercheWebErreur)",
        )

        self.assertTrue(texte.strip(), "la réponse finale est vide")
        # Le résultat de la recherche a atteint la réponse : au moins une
        # citation `[n]` correspondant à un résultat réellement récupéré,
        # PAS une invention — c'est le point de ce test, pas seulement
        # « un tool_call a eu lieu ».
        rangs_valides = {r.rang for r in premier["resultats"]}
        rangs_cites = {int(n) for n in __import__("re").findall(r"\[(\d+)\]", texte)}
        self.assertTrue(
            rangs_cites & rangs_valides,
            f"aucune citation [n] de la réponse ({rangs_cites}) ne correspond "
            f"à un résultat réellement récupéré ({rangs_valides}) — texte : {texte[:300]!r}",
        )

        self.assertIsNotNone(stats, "aucun __stats__ agrégé n'est sorti du tour")
        self.assertGreater(stats["output_tokens"], 0)

    def test_qwen2_5_7b_sans_raisonnement(self):
        self._verifier_round_trip_reel("qwen2.5:7b")

    def test_qwen3_8b_avec_raisonnement(self):
        self._verifier_round_trip_reel("qwen3:8b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
