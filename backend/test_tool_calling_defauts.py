"""`recherche_approfondie` désactivée par défaut — et le bouton du chat INCHANGÉ.

Depuis le 2026-09-23, `core.memory._CONTEXT_DEFAULT["tool_calling"]` ne
propose plus `recherche_approfondie` au modèle à chaque tour : activée, elle
ajoutait jusqu'à 4 recherches web aux 2 de `web_search` sur des questions qui
n'en demandaient pas. Le bouton « Recherche approfondie » du chat
(`deep_search_override`, `modules/chat/router.py`) doit continuer de la
forcer pour UN message, exactement comme avant — y compris quand le skill,
ou tout le tool-calling, est désactivé dans les Réglages.

Ce fichier pilote le vrai `/ws/chat` et capture ce que le routeur transmet à
`llm.stream` (`outils`, `budgets_override`) : c'est la frontière qui décide de
ce que le modèle se voit proposer, indépendamment du fournisseur. Aucun appel
réseau : `llm.stream` est remplacé, et aucun message n'envoie
`web_search_override` (qui déclencherait la vraie recherche du classifieur).

Usage :
    python test_tool_calling_defauts.py
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — isole les dossiers AVANT tout import de core.*/main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import modules.chat.router as routeur_chat  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.history import HistoryEngine  # noqa: E402
from core.memory import _CONTEXT_DEFAULT, normaliser_tool_calling  # noqa: E402
from core.runtime import memory  # noqa: E402

#: URL ABSOLUE — cf. test_chat_ws_conversation.py, piège TrustedHostMiddleware.
_WS = "ws://localhost/ws/chat?token={t}"


class DefautTest(unittest.TestCase):
    """Le défaut lui-même, et ce que la fusion fait d'un fichier existant."""

    def test_recherche_approfondie_desactivee_par_defaut(self):
        skills = _CONTEXT_DEFAULT["tool_calling"]["skills"]
        self.assertFalse(skills["recherche_approfondie"]["enabled"])
        self.assertEqual(skills["recherche_approfondie"]["budget"], 4)
        # Les deux autres skills natifs ne changent pas.
        self.assertTrue(skills["web_search"]["enabled"])
        self.assertTrue(skills["history_search"]["enabled"])

    def test_un_fichier_existant_active_le_reste(self):
        """`context_session.json` déjà écrit avec `enabled: true` (l'état de
        toute instance antérieure) : la fusion ne réécrit pas une valeur
        présente — le nouveau défaut ne vaut que pour une clé ABSENTE."""
        sur_disque = {"enabled": True, "skills": {
            "web_search": {"enabled": True}, "history_search": {"enabled": True},
            "recherche_approfondie": {"enabled": True, "budget": 6},
        }}
        resultat = normaliser_tool_calling(sur_disque)
        self.assertTrue(resultat["skills"]["recherche_approfondie"]["enabled"])
        self.assertEqual(resultat["skills"]["recherche_approfondie"]["budget"], 6)


class RouteurTest(unittest.TestCase):
    """Ce que le modèle se voit proposer, lu à la sortie du routeur."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54324))
        cls.token = get_api_token()

    def setUp(self):
        self.appels: list[dict] = []

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            self.appels.append(kw)
            return iter(["ok"])

        stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", stream_original)
        routeur_chat.llm.stream = faux_stream

        titre_original = HistoryEngine._generate_title
        self.addCleanup(setattr, HistoryEngine, "_generate_title", titre_original)
        HistoryEngine._generate_title = lambda self_, messages: "Titre auto"

        avant = copy.deepcopy(memory.get_context().get("tool_calling"))
        self.addCleanup(memory.update_context, tool_calling=avant)
        memory.update_context(tool_calling=copy.deepcopy(_CONTEXT_DEFAULT["tool_calling"]))

    def _tour(self, **extra) -> dict:
        corps = {"role": "user", "content": "question", "direct": True, **extra}
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps(corps))
            while json.loads(ws.receive_text())["type"] not in ("done", "error"):
                pass
        self.assertEqual(len(self.appels), 1)
        return self.appels[0]

    def test_par_defaut_le_modele_ne_se_voit_pas_proposer_la_recherche_approfondie(self):
        kw = self._tour()
        self.assertEqual(sorted(kw["outils"]), ["history_search", "web_search"])

    def test_le_bouton_la_force_pour_ce_message_avec_le_budget_configure(self):
        memory.update_context(tool_calling={"enabled": True, "skills": {
            "web_search": {"enabled": True}, "history_search": {"enabled": True},
            "recherche_approfondie": {"enabled": False, "budget": 7},
        }})
        kw = self._tour(deep_search_override=True)
        self.assertIn("recherche_approfondie", kw["outils"])
        self.assertEqual(kw["budgets_override"]["recherche_approfondie"], 7)

    def test_le_bouton_passe_outre_l_interrupteur_general(self):
        memory.update_context(tool_calling={"enabled": False, "skills": {}})
        kw = self._tour(deep_search_override=True)
        self.assertEqual(kw["outils"], ["recherche_approfondie"])

    def test_le_bouton_ne_reste_pas_arme_au_message_suivant(self):
        self._tour(deep_search_override=True)
        self.appels.clear()
        kw = self._tour()
        self.assertNotIn("recherche_approfondie", kw["outils"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
