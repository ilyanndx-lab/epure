"""Chaque trame du WebSocket de chat porte `conversation_id` — le bug de fuite.

Constat qui a lancé ce correctif : une seule connexion `/ws/chat` sert TOUTES
les conversations d'un onglet (CLAUDE.md, « une seule connexion WS pour toutes
les conversations » — voulu, pas remis en cause ici). Le frontend, lui,
mutait un état `messages` qui ne représente QUE la conversation actuellement
affichée, sans jamais vérifier qu'un événement entrant lui appartenait. Deux
onglets... non, un seul onglet suffit : basculer d'une conversation A (qui
génère encore) vers une conversation B pendant le streaming faisait atterrir
le texte de A dans B, puisque rien sur le fil n'identifiait à qui appartenait
un `token`.

Le correctif est bilatéral : ce fichier ne verrouille QUE le côté serveur (le
fil porte l'identifiant). Le filtrage côté client est verrouillé par
`frontend/src/modules/chat/Component.conversation.test.tsx`.

Le backend NE PEUT PAS multiplexer deux tours sur la même connexion (la boucle
`while True: await websocket.receive_text()` traite un message jusqu'au `done`
avant de relire le suivant) — donc chaque trame émise pendant le traitement
d'UN message porte le `conversation_id` de CE message, jamais un autre. C'est
ce qui rend le filtrage client suffisant sans qu'aucun changement de protocole
de reconnexion ne soit nécessaire.

Usage :
    python test_chat_conversation_id_trames.py
"""

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
from core.runtime import history_engine  # noqa: E402

#: URL ABSOLUE — cf. test_chat_ws_conversation.py, même piège TrustedHostMiddleware.
_WS = "ws://localhost/ws/chat?token={t}"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54323))
        cls.token = get_api_token()

    def setUp(self):
        self._stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._stream_original)
        self._poser_reponse("réponse")

        # Titrage neutralisé — cf. test_chat_ws_conversation.py : `history_engine`
        # est un `_LazyEngine`, patcher l'instance ne serait pas vu.
        self._titre_original = HistoryEngine._generate_title
        self.addCleanup(setattr, HistoryEngine, "_generate_title", self._titre_original)
        HistoryEngine._generate_title = lambda self_, messages: "Titre auto"

    def _poser_reponse(self, texte: str):
        def faux_stream(messages, model=None, raisonnement=True, **kw):
            return iter([texte])
        routeur_chat.llm.stream = faux_stream

    def _envoyer(self, ws, texte, conversation_id=None, **extra):
        corps = {"role": "user", "content": texte, "direct": True, **extra}
        if conversation_id is not None:
            corps["conversation_id"] = conversation_id
        ws.send_text(json.dumps(corps))
        trames = []
        while True:
            t = json.loads(ws.receive_text())
            trames.append(t)
            if t["type"] in ("done", "error"):
                return trames


class ConversationIdSurChaqueTrameTest(_Base):
    """Le tour mono-modèle (chemin direct, le plus fréquent) : chaque trame
    reçue pour ce tour porte le MÊME `conversation_id`, et c'est celui de la
    conversation réellement utilisée."""

    def test_toutes_les_trames_du_tour_portent_le_bon_conversation_id(self):
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames = self._envoyer(ws, "question", conversation_id=conv["id"])

        self.assertTrue(trames, "au moins une trame doit avoir été reçue")
        sans_id = [t for t in trames if "conversation_id" not in t]
        self.assertEqual(sans_id, [], f"trames sans conversation_id : {sans_id}")
        mauvais_id = [t for t in trames if t["conversation_id"] != conv["id"]]
        self.assertEqual(mauvais_id, [], f"trames avec un autre id : {mauvais_id}")

        types_vus = {t["type"] for t in trames}
        self.assertIn("token", types_vus)
        self.assertIn("done", types_vus)
        self.assertIn("meta_message", types_vus)

    def test_l_annonce_de_creation_paresseuse_porte_aussi_l_id(self):
        """Sans identifiant fourni : la conversation naît au premier message,
        et l'événement qui l'annonce doit porter la MÊME valeur que son `id`
        historique — un client qui compare les deux ne doit jamais les voir
        diverger."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps({"role": "user", "content": "bonjour", "direct": True}))
            trames = []
            while True:
                t = json.loads(ws.receive_text())
                trames.append(t)
                if t["type"] in ("done", "error"):
                    break

        annonce = next(t for t in trames if t["type"] == "conversation")
        self.assertEqual(annonce["conversation_id"], annonce["id"])
        autres = [t for t in trames if t["type"] != "conversation"]
        mauvais_id = [t for t in autres if t.get("conversation_id") != annonce["id"]]
        self.assertEqual(mauvais_id, [], f"trames avec un autre id : {mauvais_id}")


class ConversationIdDeuxFilsDistinctsTest(_Base):
    """Deux tours successifs sur deux conversations DIFFÉRENTES, sur la MÊME
    connexion : le `conversation_id` de chaque lot de trames doit suivre le
    tour, jamais rester collé au précédent."""

    def test_conv_id_ne_fuit_pas_d_un_tour_au_suivant(self):
        a = history_engine.create_conversation(titre="A")
        b = history_engine.create_conversation(titre="B")
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames_a = self._envoyer(ws, "pour A", conversation_id=a["id"])
            trames_b = self._envoyer(ws, "pour B", conversation_id=b["id"])

        self.assertTrue(all(t["conversation_id"] == a["id"] for t in trames_a))
        self.assertTrue(all(t["conversation_id"] == b["id"] for t in trames_b))


class ConversationIdComparaisonTest(_Base):
    """Vocabulaire `compare_*` : même exigence, même mécanisme."""

    def setUp(self):
        super().setUp()
        self._ids_original = routeur_chat.ids_disponibles

        async def _faux_ids_disponibles(_registry):
            return {"modele-a", "modele-b"}
        routeur_chat.ids_disponibles = _faux_ids_disponibles
        self.addCleanup(setattr, routeur_chat, "ids_disponibles", self._ids_original)

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            return iter(["Bon", "jour"])
        routeur_chat.llm.stream = faux_stream

    def test_trames_compare_portent_le_conversation_id(self):
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps({
                "role": "user", "content": "question",
                "compare_models": ["modele-a", "modele-b"],
                "conversation_id": conv["id"],
            }))
            trames = []
            while True:
                t = json.loads(ws.receive_text())
                trames.append(t)
                if t["type"] in ("compare_all_done", "error"):
                    break

        sans_id = [t for t in trames if "conversation_id" not in t]
        self.assertEqual(sans_id, [])
        mauvais_id = [t for t in trames if t["conversation_id"] != conv["id"]]
        self.assertEqual(mauvais_id, [])

    def test_erreur_de_choix_perime_porte_un_conversation_id_du_client(self):
        """`etat is None` (comparaison déjà résolue, ou jamais lancée) : la
        seule information de conversation disponible vient du message du
        client — elle ne doit jamais manquer purement et simplement."""
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps({
                "type": "compare_choix", "conv_id": conv["id"], "model": "modele-a",
            }))
            t = json.loads(ws.receive_text())

        self.assertEqual(t["type"], "error")
        self.assertEqual(t["conversation_id"], conv["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
