#!/usr/bin/env python3
"""Tool-calling natif `web_search` — bout en bout, sur un VRAI LM Studio.

Nommé `integration_` et NON `test_` volontairement (cf. CLAUDE.md §2) : la CI
n'a pas LM Studio, et ce script fait de VRAIS appels réseau — LM Studio en
local ET DuckDuckGo (`core.websearch.rechercher` n'est PAS mocké ici, à la
différence de `test_tool_calling_lmstudio.py`). `unittest discover -p
'test_*.py'` ne le ramasse donc pas.

Ce que ce fichier PROUVE, que le mocké ne peut pas prouver à lui seul :

* la sonde `capacites_outils_lmstudio` lit bien `trained_for_tool_use` sur
  la version installée de LM Studio, pour le modèle testé ;
* LM Studio diffuse réellement les `tool_calls` en FRAGMENTS (forme que le
  mocké rejoue) — mesuré ici sur le flux brut du SDK, pas supposé ;
* un vrai modèle, informé du schéma `web_search`, DÉCIDE de l'appeler, et la
  réponse finale intègre le résultat de la vraie recherche (citations `[n]`
  qui correspondent à des résultats réellement récupérés).

**Modèle : `mistralai/ministral-3-3b`, PRÉCHARGÉ** (`lms load
mistralai/ministral-3-3b`). Ce script ne charge rien lui-même : un modèle non
chargé fait SKIP, pour qu'un lancement distrait ne déclenche pas un
chargement JIT. **Ne pas y substituer le 9B** (`huihui-qwen3.5-9b-
abliterated`) : il gèle entièrement ce poste (CLAUDE.md §8) — et il n'est de
toute façon pas entraîné au tool-calling (`trained_for_tool_use: false`),
donc ne recevrait aucun outil.

**Mesuré sur ce poste le 2026-09-22** : les trois tests passent, deux
lancements sur deux, ~50 s en tout (dont la vraie recherche DuckDuckGo) ; le
modèle a appelé `web_search` avec une requête reformulée par lui-même et cité
des rangs `[n]` réellement récupérés.

Usage :
    lms load mistralai/ministral-3-3b
    python integration_tool_calling_lmstudio.py
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR ; sans effet sur LM Studio/le réseau

os.environ.setdefault("EPURE_ALLOWED_HOSTS", "localhost,127.0.0.1,::1")

from core.llm import LLMEngine, lmstudio_host  # noqa: E402
from core.models import capacites_outils_lmstudio, etat_modele_lmstudio  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
_MODELE = "mistralai/ministral-3-3b"
_QUESTION = "Quel est le cours actuel du bitcoin en dollars ? Utilise la recherche web."


class ToolCallingLmStudioBoutEnBoutTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        etat = etat_modele_lmstudio(_MODELE)
        if etat is None:
            raise unittest.SkipTest(
                f"LM Studio injoignable sur {lmstudio_host}, ou {_MODELE} absent du catalogue.")
        if etat != "loaded":
            raise unittest.SkipTest(
                f"{_MODELE} n'est pas chargé (état : {etat}) — `lms load {_MODELE}` d'abord.")

    def test_la_sonde_declare_la_capacite(self):
        caps = capacites_outils_lmstudio()
        self.assertIsNotNone(caps)
        self.assertIs(caps.get(_MODELE), True)

    def test_les_tool_calls_arrivent_fragmentes(self):
        """Flux BRUT du SDK, sans `LLMEngine` : c'est la forme que
        `_stream_openai` doit savoir recoller."""
        from core.llm import _OUTIL_WEB_SEARCH
        client = LLMEngine(config_path=_CONFIG)._openai_client("lmstudio")
        flux = client.chat.completions.create(
            model=_MODELE, stream=True, temperature=0, max_tokens=256,
            messages=[{"role": "user", "content": _QUESTION}],
            tools=[_OUTIL_WEB_SEARCH],
        )
        fragments, ids, finish = [], [], None
        for chunk in flux:
            if not chunk.choices:
                continue
            for tc in chunk.choices[0].delta.tool_calls or []:
                fragments.append(tc)
                ids.append(tc.id)
            finish = chunk.choices[0].finish_reason or finish
        self.assertEqual(finish, "tool_calls", "le modèle n'a pas appelé l'outil")
        self.assertGreater(len(fragments), 2, "arguments non fragmentés — forme inattendue")
        self.assertTrue(ids[0], "pas d'id sur le premier fragment")
        self.assertTrue(all(not i for i in ids[1:]),
                        "l'id est répété sur les fragments suivants — l'accumulation "
                        "par index reste correcte, mais la mesure documentée a changé")

    def test_round_trip_reel(self):
        moteur = LLMEngine(config_path=_CONFIG)
        texte, appels, stats, etapes = [], [], [], []
        for item in moteur.stream([{"role": "user", "content": _QUESTION}],
                                  model=f"lmstudio:{_MODELE}", outils=["web_search"],
                                  on_etape_recherche=etapes.append):
            if isinstance(item, str):
                texte.append(item)
            elif isinstance(item, dict) and item.get("__tool_call__"):
                appels.append(item)
            elif isinstance(item, dict) and item.get("__stats__"):
                stats.append(item)
        texte = "".join(texte)

        self.assertGreaterEqual(len(appels), 1, f"web_search jamais appelé — texte : {texte[:300]!r}")
        premier = appels[0]
        self.assertEqual(premier["outil"], "web_search")
        self.assertTrue(premier["arguments"].get("requete"))
        self.assertGreater(len(premier["resultats"]), 0,
                           "DuckDuckGo n'a rien rendu — réseau indisponible ?")
        self.assertIn("tool_call_web_search", [e.get("etape") for e in etapes])

        self.assertTrue(texte.strip(), "réponse finale vide")
        rangs_valides = {r.rang for a in appels for r in a["resultats"]}
        rangs_cites = {int(n) for n in re.findall(r"\[(\d+)\]", texte)}
        self.assertTrue(rangs_cites & rangs_valides,
                        f"aucune citation [n] ({rangs_cites}) ne correspond aux résultats "
                        f"({rangs_valides}) — texte : {texte[:300]!r}")

        self.assertEqual(len(stats), 1, "un seul __stats__ agrégé par tour")
        self.assertGreater(stats[0]["output_tokens"], 0)
        self.assertLess(stats[0]["contexte_tokens"], stats[0]["prompt_tokens"],
                        "multi-round : le contexte est le dernier round, pas la somme")


if __name__ == "__main__":
    unittest.main(verbosity=2)
