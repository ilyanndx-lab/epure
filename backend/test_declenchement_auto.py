"""Phase 6 — déclenchement automatique de la recherche web, câblage bout en
bout côté `/ws/chat` : un message SANS `web_search_override` mais contenant
un motif détecté par `core.websearch.detecter_intention_recherche` doit
produire une étape `declenchement_auto` dans la trace persistée ET déclencher
effectivement le pipeline de recherche existant — pas un chemin dupliqué.

Suit le style de `test_chat_ws_conversation.py` (TestClient + WebSocket réel,
LLM/titrage neutralisés) et neutralise `_rechercher_pour_prompt` (le point où
`modules/chat/router.py` appelle `core.websearch`) pour isoler du réseau,
dans le style déjà en place dans `test_web_search.py`.

Usage :
    python test_declenchement_auto.py
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les dossiers AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import modules.chat.router as routeur_chat  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.history import HistoryEngine  # noqa: E402
from core.runtime import history_engine  # noqa: E402
from core.websearch import ResultatWeb  # noqa: E402

_WS = "ws://localhost/ws/chat?token={t}"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54322))
        cls.token = get_api_token()

    def setUp(self):
        self._stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._stream_original)

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            return iter(["réponse"])
        routeur_chat.llm.stream = faux_stream

        self._titre_original = HistoryEngine._generate_title
        self.addCleanup(setattr, HistoryEngine, "_generate_title", self._titre_original)
        HistoryEngine._generate_title = lambda self_, messages: "Titre auto"

        # Isolation du réseau : `_rechercher_pour_prompt` est le point d'appel
        # de `core.websearch` depuis le router (cf. sa docstring). On y capture
        # la requête reçue pour vérifier que le pipeline @web normal a bien
        # tourné, sans dépendre de DuckDuckGo.
        self.requetes_recherche: list[str] = []
        self._rechercher_original = routeur_chat._rechercher_pour_prompt

        def _faux_rechercher(query, on_etape=None):
            self.requetes_recherche.append(query)
            resultats = [ResultatWeb(rang=1, titre="Résultat", url="https://exemple.org/",
                                      extrait="Un extrait.", moteur="ddg-html")]
            if on_etape is not None:
                on_etape({"etape": "recherche_debut", "requete": query, "moteur": "ddg-instant"})
                on_etape({"etape": "recherche_resultats", "nombre": 1, "moteur": "ddg-instant",
                           "ms": 5, "resultats": [{"rang": 1, "titre": "Résultat", "url": "https://exemple.org/"}]})
            return routeur_chat.formater_pour_llm(resultats), resultats

        patcher = mock.patch.object(routeur_chat, "_rechercher_pour_prompt", side_effect=_faux_rechercher)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _envoyer(self, ws, texte, web_search_override=False, conversation_id=None):
        corps = {"role": "user", "content": texte, "direct": True,
                 "web_search_override": web_search_override}
        if conversation_id is not None:
            corps["conversation_id"] = conversation_id
        ws.send_text(json.dumps(corps))
        trames, annonce = [], None
        while True:
            t = json.loads(ws.receive_text())
            if t["type"] == "conversation":
                annonce = t["id"]
                continue
            trames.append(t)
            if t["type"] in ("done", "error"):
                return trames, annonce


class DeclenchementAutoTest(_Base):
    def test_motif_sans_override_declenche_la_recherche_et_la_trace(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, conv_id = self._envoyer(ws, "Quelle est la météo à Paris ?")

        self.assertEqual(trames[-1]["type"], "done")
        # Le pipeline @web normal a bien tourné (pas un chemin dupliqué).
        self.assertEqual(self.requetes_recherche, ["Quelle est la météo à Paris ?"])

        conv = history_engine.get_conversation(conv_id)
        trace = conv["messages"][-1]["trace_recherche"]
        etapes = [e["etape"] for e in trace]
        self.assertIn("declenchement_auto", etapes)
        etape_auto = next(e for e in trace if e["etape"] == "declenchement_auto")
        self.assertEqual(etape_auto["categorie"], "factuel_temps_reel")
        self.assertEqual(etape_auto["mode"], "simple")
        self.assertIn("declencheur", etape_auto)

    def test_message_ordinaire_sans_motif_aucune_recherche_ni_trace(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, conv_id = self._envoyer(ws, "explique-moi la récursivité")

        self.assertEqual(trames[-1]["type"], "done")
        self.assertEqual(self.requetes_recherche, [])
        conv = history_engine.get_conversation(conv_id)
        trace = conv["messages"][-1].get("trace_recherche", [])
        self.assertNotIn("declenchement_auto", [e["etape"] for e in trace])

    def test_override_manuel_avec_motif_present_aucune_etape_declenchement_auto(self):
        """Message qui aurait matché un motif automatique, MAIS envoyé avec
        `web_search_override=True` (le client a tapé @web) : la trace ne doit
        porter AUCUNE étape `declenchement_auto` — pas la même origine."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, conv_id = self._envoyer(
                ws, "Quelle est la météo à Paris ?", web_search_override=True,
            )

        self.assertEqual(trames[-1]["type"], "done")
        # La recherche a bien eu lieu (override manuel toujours honoré).
        self.assertEqual(self.requetes_recherche, ["Quelle est la météo à Paris ?"])
        conv = history_engine.get_conversation(conv_id)
        trace = conv["messages"][-1]["trace_recherche"]
        self.assertNotIn("declenchement_auto", [e["etape"] for e in trace])

    def test_recherche_academique_ne_declenche_pas_la_recherche_web(self):
        """Non-régression bout en bout du piège central de cette phase."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, conv_id = self._envoyer(
                ws, "je fais une recherche sur les tenseurs pour ma prépa",
            )
        self.assertEqual(trames[-1]["type"], "done")
        self.assertEqual(self.requetes_recherche, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
