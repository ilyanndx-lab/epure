#!/usr/bin/env python3
"""`history_search`, deuxième entrée du registre de tool-calling natif
`core.llm._SKILLS` — calqué sur `test_tool_calling_ollama.py`, qui reste la
source de vérité pour `web_search` seul et pour la non-régression du chemin
`outils=None` (comportement d'avant tout ce chantier, à l'octet).

Ce fichier couvre ce qui est NOUVEAU ou PARTAGÉ entre skills :

* `history_search` seul (round-trip, sonde de capacité partagée, absence de
  résultat) ;
* les deux skills actifs dans le même tour, avec des budgets d'invocations
  INDÉPENDANTS (épuiser `web_search` ne doit pas empêcher `history_search`
  de continuer à répondre, et réciproquement) ;
* `history_search` rend TOUJOURS `resultats=[]` — jamais des `ResultatWeb`
  citables par `[n]` ;
* le correctif de `modules/chat/router.py` (§3 du chantier) : un
  `__tool_call__` d'origine `history_search` ne doit JAMAIS étendre
  `web_resultats`, même s'il portait (cas adversarial, jamais produit
  aujourd'hui par `_executer_outil_history_search`) des résultats non vides —
  sans cette garde explicite, ils se mélangeraient à la résolution des
  citations `[n]`, qui résout par RANG.

Réutilise le harnais de `test_tool_calling_ollama.py` (`_Rejoueur`, `_chunk`,
`_tool_call`, `_stream`, les extracteurs `_textes`/`_stats`/`_appels_outil`)
plutôt que de le dupliquer — seul le mock de
`core.runtime.history_engine.search_history` est ajouté ici, propre à ce
fichier. La partie routeur reprend le harnais WebSocket de
`test_chat_conversation_id_trames.py` (`TestClient`, `routeur_chat.llm.stream`
remplacé).

Usage :
    python test_skills_history.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — isole EPURE_DATA_DIR AVANT tout import de core.*

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import modules.chat.router as routeur_chat  # noqa: E402
from core import llm as module_llm  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.runtime import history_engine  # noqa: E402
from core.websearch import ResultatWeb  # noqa: E402

from test_tool_calling_ollama import (  # noqa: E402
    _Rejoueur, _appels_outil, _chunk, _stats, _stream, _textes, _tool_call,
)


class _HistoriqueMocke:
    """Remplace `core.runtime.history_engine.search_history`, le temps d'un
    test — SANS jamais résoudre le vrai moteur. `history_engine` est un
    `_LazyEngine` (core/runtime.py) : `__getattr__` ne se déclenche que si
    l'attribut n'existe pas déjà sur l'INSTANCE, donc une simple assignation
    directe pose un attribut d'instance qui masque le proxy sans jamais
    construire le `HistoryEngine` réel (coûteux : store vectoriel, modèle
    d'embedding). `del` en sortie retire cet attribut et rend le proxy à son
    état paresseux d'origine.
    """

    def __init__(self, resultats=None):
        self._resultats = resultats if resultats is not None else [
            {"id": "c1", "date": "2026-09-01", "titre": "Intégrales",
             "modèle": "qwen2.5:7b", "extrait": "On avait vu le changement de variable."},
        ]
        self.appels: list[str] = []

    def __enter__(self):
        def _faux_search(requete):
            self.appels.append(requete)
            return list(self._resultats)
        history_engine.search_history = _faux_search
        return self

    def __exit__(self, *exc):
        del history_engine.search_history


class RegistreSkillsTest(unittest.TestCase):
    """Le registre `_SKILLS` (core/llm.py) porte les deux skills, avec un
    exécuteur et un plafond propres à chacun."""

    def test_deux_entrees_avec_schema_executeur_et_budget(self):
        self.assertEqual(set(module_llm._SKILLS), {"web_search", "history_search"})
        for nom, skill in module_llm._SKILLS.items():
            self.assertEqual(skill["schema"]["function"]["name"], nom)
            self.assertTrue(callable(skill["executor"]))
            self.assertGreater(skill["budget_max"], 0)
        self.assertIs(module_llm._SKILLS["web_search"]["schema"], module_llm._OUTIL_WEB_SEARCH)
        self.assertIs(module_llm._SKILLS["history_search"]["schema"], module_llm._OUTIL_HISTORY_SEARCH)


class HistorySearchSeulTest(unittest.TestCase):
    """`history_search` suit le même round-trip que `web_search`, avec deux
    différences assumées : `resultats=[]` toujours, et son texte vient de
    `HistoryEngine.search_history`, pas de `core.websearch`."""

    def test_tool_call_puis_reponse_finale(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(nom="history_search", requete="intégrales")],
                    done=True, prompt_tokens=10, output_tokens=5)],
            [_chunk(content="On en avait parlé.", done=True, prompt_tokens=40, output_tokens=8)],
        ]) as r, _HistoriqueMocke() as h:
            sortie, _ = _stream(r, outils=["history_search"])

        self.assertEqual(h.appels, ["intégrales"])
        self.assertEqual(r.appels_recherche, [], "web_search ne doit pas être sollicité")

        appels_outil = _appels_outil(sortie)
        self.assertEqual(len(appels_outil), 1)
        self.assertEqual(appels_outil[0]["outil"], "history_search")
        self.assertEqual(appels_outil[0]["resultats"], [], "jamais des ResultatWeb citables")

        self.assertEqual("".join(_textes(sortie)), "On en avait parlé.")

        message_outil = r.appels[1]["messages"][-1]
        self.assertEqual(message_outil["tool_name"], "history_search")
        self.assertIn("Intégrales", message_outil["content"])
        self.assertIn("2026-09-01", message_outil["content"])
        self.assertNotIn("tool_call_id", message_outil)

        stats = _stats(sortie)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["prompt_tokens"], 50)

    def test_capacite_gatee_comme_web_search(self):
        """Même sonde `tools` que `web_search` : `_capacite_tools_disponible`
        gate le registre ENTIER, pas un skill à la fois."""
        with _Rejoueur(
            rounds=[[_chunk(content="ok", done=True, output_tokens=1)]],
            capacites={"qwen2.5:7b": {"completion"}},
        ) as r:
            sortie, _ = _stream(r, outils=["history_search"])
        self.assertNotIn("tools", r.appels[0])
        self.assertEqual(_textes(sortie), ["ok"])

    def test_aucun_resultat(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(nom="history_search", requete="x")], done=True)],
            [_chunk(content="Rien trouvé.", done=True)],
        ]) as r, _HistoriqueMocke(resultats=[]):
            _stream(r, outils=["history_search"])
        message_outil = r.appels[1]["messages"][-1]
        self.assertIn("Aucun résultat", message_outil["content"])

    def test_requete_vide_ne_cherche_pas(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(nom="history_search", requete="")], done=True)],
            [_chunk(content="ok", done=True)],
        ]) as r, _HistoriqueMocke() as h:
            _stream(r, outils=["history_search"])
        self.assertEqual(h.appels, [])
        message_outil = r.appels[1]["messages"][-1]
        self.assertIn("vide", message_outil["content"].lower())


class DeuxSkillsMemeTourTest(unittest.TestCase):
    """`web_search` et `history_search` actifs ensemble : budgets indépendants."""

    def test_les_deux_skills_sont_exposes_et_appelables(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[
                _tool_call(nom="web_search", requete="météo"),
                _tool_call(nom="history_search", requete="intégrales"),
            ], done=True)],
            [_chunk(content="Fini.", done=True)],
        ]) as r, _HistoriqueMocke() as h:
            sortie, _ = _stream(r, outils=["web_search", "history_search"])

        self.assertIn("tools", r.appels[0])
        noms_schemas = {t["function"]["name"] for t in r.appels[0]["tools"]}
        self.assertEqual(noms_schemas, {"web_search", "history_search"})

        appels_outil = _appels_outil(sortie)
        self.assertEqual({a["outil"] for a in appels_outil}, {"web_search", "history_search"})
        self.assertEqual(r.appels_recherche, ["météo"])
        self.assertEqual(h.appels, ["intégrales"])

    def test_epuiser_web_search_n_empeche_pas_history_search(self):
        with _Rejoueur(rounds=[
            [_chunk(tool_calls=[_tool_call(nom="web_search", requete="un")], done=True)],
            [_chunk(tool_calls=[_tool_call(nom="web_search", requete="deux")], done=True)],
            # web_search épuisé (budget 2/2 consommé ci-dessus) ; history_search
            # doit encore répondre normalement dans ce même round.
            [_chunk(tool_calls=[_tool_call(nom="web_search", requete="trois"),
                                 _tool_call(nom="history_search", requete="q")], done=True)],
            [_chunk(content="Fini.", done=True)],
        ]) as r, _HistoriqueMocke() as h:
            _stream(r, outils=["web_search", "history_search"])

        self.assertEqual(r.appels_recherche, ["un", "deux"], "le 3e web_search ne doit pas chercher")
        self.assertEqual(h.appels, ["q"], "history_search doit passer malgré web_search épuisé")

        # `msgs` (core/llm.py) est un seul objet muté en place à travers toute
        # la boucle `while True` : `r.appels[i]["messages"]` référence donc,
        # pour TOUT `i`, la même liste — celle accumulée jusqu'à la FIN du
        # tour (propriété déjà vraie dans `test_tool_calling_ollama.py`, qui
        # ne l'exploite qu'en lisant les DERNIERS éléments). On lit le dernier
        # round ici et on distingue les réponses par contenu, pas par round.
        messages_finaux = r.appels[-1]["messages"]
        reponses_web = [m for m in messages_finaux if m.get("tool_name") == "web_search"]
        reponses_hist = [m for m in messages_finaux if m.get("tool_name") == "history_search"]
        self.assertEqual(len(reponses_web), 3, "un, deux, et le 3e (épuisé)")
        self.assertEqual(len(reponses_hist), 1)
        self.assertIn("épuisé", reponses_web[-1]["content"], "le 3e appel web_search trouve le budget à 0")
        self.assertNotIn("épuisé", reponses_hist[0]["content"])


# ── Correctif routeur §3 : pas de mélange dans `web_resultats` ──────────────
#
# Harnais repris de `test_chat_conversation_id_trames.py` : `TestClient` +
# `routeur_chat.llm.stream` remplacé par un faux générateur, pour éprouver le
# comportement RÉEL de `modules/chat/router.py` (pas seulement `core/llm.py`).

_WS = "ws://localhost/ws/chat?token={t}"


class RouterHistorySearchNeFuitPasDansWebTest(unittest.TestCase):
    """Un `__tool_call__` d'origine `history_search`, même avec des résultats
    non vides (cas adversarial — `_executer_outil_history_search` n'en produit
    jamais aujourd'hui), ne doit JAMAIS se retrouver dans `web_resultats`, donc
    jamais dans le bloc `sources` de la trame `done`."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                 client=("127.0.0.1", 54329))
        cls.token = get_api_token()

    def setUp(self):
        self._stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._stream_original)

    def _envoyer(self, ws, texte, conversation_id):
        ws.send_text(json.dumps({
            "role": "user", "content": texte, "direct": True,
            "conversation_id": conversation_id,
        }))
        trames = []
        while True:
            t = json.loads(ws.receive_text())
            trames.append(t)
            if t["type"] in ("done", "error"):
                return trames

    def test_resultats_history_search_absents_des_sources(self):
        conv = history_engine.create_conversation()
        faux_resultat = ResultatWeb(
            rang=1, titre="Ne doit jamais apparaître", url="https://ne-doit-pas.fr",
            extrait="…", moteur="ddg-html",
        )

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            yield {
                "__tool_call__": True, "outil": "history_search",
                "arguments": {"requete": "q"}, "resultats": [faux_resultat],
            }
            yield "Réponse utile [1]."
        routeur_chat.llm.stream = faux_stream

        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames = self._envoyer(ws, "question", conv["id"])

        done = next(t for t in trames if t["type"] == "done")
        self.assertEqual(done.get("sources", []), [],
                          "un résultat history_search ne doit jamais alimenter les Sources")

    def test_resultats_web_search_restent_dans_les_sources(self):
        """Non-régression du même test : un VRAI `web_search` continue de
        remplir `sources` normalement — la garde ne doit exclure QUE
        `history_search`."""
        conv = history_engine.create_conversation()
        vrai_resultat = ResultatWeb(
            rang=1, titre="Page réelle", url="https://exemple.fr/page",
            extrait="…", moteur="ddg-html",
        )

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            yield {
                "__tool_call__": True, "outil": "web_search",
                "arguments": {"requete": "q"}, "resultats": [vrai_resultat],
            }
            yield "Réponse utile [1]."
        routeur_chat.llm.stream = faux_stream

        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames = self._envoyer(ws, "question", conv["id"])

        done = next(t for t in trames if t["type"] == "done")
        self.assertEqual(len(done.get("sources", [])), 1)
        self.assertEqual(done["sources"][0]["url"], "https://exemple.fr/page")


if __name__ == "__main__":
    unittest.main(verbosity=2)
