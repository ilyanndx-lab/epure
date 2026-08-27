"""Le WebSocket de chat est devenu conversationnel — étape 4 du chantier.

Ce qui change, et que ce fichier verrouille :

* la liste `history` en fermeture du handler a DISPARU. L'historique du prompt
  est relu sur le disque à chaque tour, donc ce que voit le modèle et ce
  qu'affiche l'écran ont enfin la même source (constat §0.4 du document) ;
* un `conversation_id` transite dans chaque message, et son ABSENCE veut dire
  « poursuis », jamais « recommence » ;
* le titrage, la consolidation et l'indexation vectorielle ne sont plus
  accrochés à la déconnexion.

Le piège que ces tests ont attrapé pendant l'écriture : sans repli sur la
conversation de la connexion, un client qui n'envoie pas d'identifiant obtenait
une conversation NEUVE à chaque message — le modèle perdait tout le contexte au
deuxième tour et la liste se remplissait d'un fil par message.

Usage :
    python test_chat_ws_conversation.py
"""

import json
import os
import sys
import time
import unittest

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

#: URL ABSOLUE — `TestClient(base_url=…)` ne reporte pas son hôte sur les
#: WebSockets, et un chemin relatif partirait sur `testserver`, refusé par
#: TrustedHostMiddleware (cf. l'en-tête de test_raisonnement_stream).
_WS = "ws://localhost/ws/chat?token={t}"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.token = get_api_token()

    def setUp(self):
        self.prompts: list[list[dict]] = []
        self._stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._stream_original)
        self._poser_reponse("réponse")

        # Le titrage appelle le LLM dans un thread de fond ; on le neutralise.
        #
        # ⚠️ Le remplacement se fait sur la CLASSE, pas sur `history_engine`.
        # Celui-ci est un `_LazyEngine`, donc un proxy : lui poser un attribut le
        # pose sur le proxy, que le moteur réel ne consulte jamais — ses méthodes
        # s'appellent entre elles via `self`. Mesuré en écrivant ce fichier : le
        # stub était ignoré, un VRAI appel Ollama partait (titre « Salut » au lieu
        # de « Titre auto », 28 s de suite au lieu d'une seconde).
        self._titre_original = HistoryEngine._generate_title
        self.addCleanup(setattr, HistoryEngine, "_generate_title", self._titre_original)
        HistoryEngine._generate_title = lambda self_, messages: "Titre auto"

    def _attendre(self, predicat, timeout=5.0, quoi="condition"):
        """Attend un effet produit par un THREAD DE FOND.

        Titrage, consolidation et indexation vectorielle sont lancés après que la
        réponse est partie (c'est tout leur intérêt : ne rien mettre sur le chemin
        du message). Assertion juste après le `done` = course perdue d'avance —
        c'est ce qui faisait échouer le test de consolidation.
        """
        limite = time.time() + timeout
        while time.time() < limite:
            if predicat():
                return True
            time.sleep(0.02)
        self.fail(f"{quoi} n'est pas survenu en {timeout} s")

    def _poser_reponse(self, texte: str):
        """Fixe la réponse du modèle et enregistre les prompts reçus."""
        def faux_stream(messages, model=None, raisonnement=True, **kw):
            self.prompts.append([dict(m) for m in messages])
            return iter([texte])
        routeur_chat.llm.stream = faux_stream

    def _envoyer(self, ws, texte, conversation_id=None):
        """Un tour complet ; rend (trames de flux, id de conversation annoncé)."""
        corps = {"role": "user", "content": texte, "direct": True}
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


class CreationParesseuseTest(_Base):
    def test_le_premier_message_cree_et_annonce_la_conversation(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            _, annonce = self._envoyer(ws, "bonjour")
        self.assertIsNotNone(annonce, "le client doit apprendre l'id créé pour lui")
        self.assertIsNotNone(history_engine.get_conversation(annonce))

    def test_ouvrir_un_socket_ne_cree_rien(self):
        """Création PARESSEUSE : sinon la liste se remplit de coquilles vides
        que l'utilisateur devrait nettoyer à la main."""
        avant = len(history_engine.list_conversations(0))
        with self.client.websocket_connect(_WS.format(t=self.token)):
            pass
        self.assertEqual(len(history_engine.list_conversations(0)), avant)

    def test_le_tour_est_bien_sur_le_disque(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "question")
        conv = history_engine.get_conversation(
            history_engine.list_conversations(0)[0]["id"])
        self.assertEqual(
            [(m["role"], m["content"]) for m in conv["messages"]],
            [("user", "question"), ("assistant", "réponse")],
        )


class ContinuiteTest(_Base):
    def test_sans_identifiant_le_second_message_poursuit(self):
        """LE piège attrapé pendant l'écriture de l'étape.

        Sans repli sur la conversation de la connexion, chaque message ouvrait un
        fil neuf : le prompt du second tour ne contenait plus le premier échange,
        donc le modèle repartait de zéro sans que rien ne le signale.
        """
        avant = len(history_engine.list_conversations(0))
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "premier")
            self._envoyer(ws, "second")

        self.assertEqual(len(history_engine.list_conversations(0)), avant + 1,
                         "un second message a ouvert une seconde conversation")
        contenus = [m["content"] for m in self.prompts[-1]]
        self.assertIn("premier", contenus)
        self.assertIn("réponse", contenus)
        self.assertIn("second", contenus)

    def test_un_identifiant_explicite_est_respecte(self):
        a = history_engine.create_conversation(titre="A")
        b = history_engine.create_conversation(titre="B")
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "pour A", conversation_id=a["id"])
            self._envoyer(ws, "pour B", conversation_id=b["id"])

        self.assertEqual(
            [m["content"] for m in history_engine.get_conversation(a["id"])["messages"]],
            ["pour A", "réponse"])
        self.assertEqual(
            [m["content"] for m in history_engine.get_conversation(b["id"])["messages"]],
            ["pour B", "réponse"])

    def test_le_contexte_survit_a_une_reconnexion(self):
        """Ce que l'ancien modèle ne pouvait pas faire.

        `history` vivait dans la fermeture du handler : une déconnexion — donc un
        rechargement de page — la vidait, et le modèle repartait de zéro pendant
        que l'écran affichait toujours la conversation (constat §0.4).
        """
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            _, conv_id = self._envoyer(ws, "avant la coupure")

        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "après la coupure", conversation_id=conv_id)

        contenus = [m["content"] for m in self.prompts[-1]]
        self.assertIn("avant la coupure", contenus,
                      "le prompt doit repartir du disque, pas d'une liste perdue")

    def test_un_identifiant_inconnu_ne_perd_pas_le_message(self):
        """On repart sur une conversation neuve et on l'ANNONCE.

        Le message vient d'être tapé : le perdre serait le pire comportement.
        L'annonce permet au client de se recaler au lieu d'écrire dans le vide.
        """
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, annonce = self._envoyer(
                ws, "message orphelin",
                conversation_id="11111111-2222-3333-4444-555555555555")

        self.assertIsNotNone(annonce)
        self.assertNotEqual(annonce, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(trames[-1]["type"], "done")
        conv = history_engine.get_conversation(annonce)
        self.assertIn("message orphelin", [m["content"] for m in conv["messages"]])


class MetadonneesDuTourTest(_Base):
    """Le protocole porte les métadonnées, pour que le client n'ait pas à relire.

    Sans ça, il faudrait un `GET` de la conversation entière après chaque tour —
    ou laisser le navigateur poser l'heure lui-même, auquel cas l'heure affichée
    avant un rechargement viendrait de son horloge et celle d'après du disque.
    """

    def test_le_done_porte_l_horodatage_et_le_modele_de_la_reponse(self):
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, _ = self._envoyer(ws, "question", conversation_id=conv["id"])

        done = trames[-1]
        self.assertEqual(done["type"], "done")
        self.assertRegex(done["horodatage"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertTrue(done["modèle"], "le modèle de la réponse doit remonter")

        # Et c'est bien ce qui est SUR LE DISQUE, pas une valeur recalculée.
        stocke = history_engine.get_conversation(conv["id"])["messages"][-1]
        self.assertEqual(done["horodatage"], stocke["horodatage"])
        self.assertEqual(done["modèle"], stocke["modèle"])

    def test_le_message_utilisateur_recoit_son_horodatage_du_serveur(self):
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, _ = self._envoyer(ws, "question", conversation_id=conv["id"])

        metas = [t for t in trames if t["type"] == "meta_message"]
        self.assertEqual(len(metas), 1, "un seul `meta_message` par tour")
        self.assertEqual(metas[0]["role"], "user")

        stocke = history_engine.get_conversation(conv["id"])["messages"][0]
        self.assertEqual(metas[0]["horodatage"], stocke["horodatage"])

    def test_le_message_utilisateur_porte_le_modele_interroge(self):
        """Le modèle À QUI la question part, remonté au client et écrit sur disque.

        Le libellé de l'interface (« envoyé à ») fait que rien n'est affirmé de
        faux : ce texte ne vient pas du modèle, il lui a été envoyé.
        """
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, _ = self._envoyer(ws, "question", conversation_id=conv["id"])

        utilisateur = history_engine.get_conversation(conv["id"])["messages"][0]
        self.assertTrue(utilisateur["modèle"])

        meta = [t for t in trames if t["type"] == "meta_message"][0]
        self.assertEqual(meta["modèle"], utilisateur["modèle"])


class TitrageTest(_Base):
    def test_le_titre_arrive_apres_le_premier_tour(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps({"role": "user", "content": "salut", "direct": True}))
            titre, conv_id = None, None
            while True:
                t = json.loads(ws.receive_text())
                if t["type"] == "conversation":
                    conv_id = t["id"]
                elif t["type"] == "titre":
                    titre = t["titre"]
                    break
                elif t["type"] == "error":
                    self.fail(f"erreur inattendue : {t}")

        self.assertEqual(titre, "Titre auto")
        self.assertEqual(history_engine.get_conversation(conv_id)["titre"], "Titre auto")

    def test_une_conversation_deja_titree_n_est_pas_retitree(self):
        """Le titrage est un appel LLM : le rejouer à chaque tour serait une
        tâche de fond permanente pour un résultat déjà acquis."""
        conv = history_engine.create_conversation(titre="Titre choisi")
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "un message", conversation_id=conv["id"])
        time.sleep(0.3)
        self.assertEqual(history_engine.get_conversation(conv["id"])["titre"],
                         "Titre choisi")


class ConsolidationTest(_Base):
    """Le déclencheur a changé de nature : par NOMBRE DE MESSAGES, plus par
    déconnexion. L'ancien ne partait qu'au plus une fois par connexion, et jamais
    pour qui laisse son onglet ouvert."""

    def setUp(self):
        super().setUp()
        self.consolidations: list = []
        original = routeur_chat.consolidation_engine.consolidate_history
        self.addCleanup(setattr, routeur_chat.consolidation_engine,
                        "consolidate_history", original)
        routeur_chat.consolidation_engine.consolidate_history = (
            lambda cid, cloud=False: self.consolidations.append(cid) or {}
        )

    def test_sous_le_seuil_rien_ne_part(self):
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "un", conversation_id=conv["id"])
        # Laisser au thread de fond le temps de mal se comporter s'il le doit :
        # sans cette pause, le test passerait même si le seuil était ignoré.
        time.sleep(0.3)
        self.assertEqual(self.consolidations, [])

    def test_le_seuil_est_garde_par_derniere_consolidation(self):
        """Idempotence : sans la marque, chaque tour au-delà du seuil relancerait
        un appel LLM. Et un multiple exact ne suffirait pas — un tour ajoute DEUX
        messages, donc `n % 10 == 0` saute dès qu'un tour n'en ajoute qu'un.
        """
        conv = history_engine.create_conversation()
        history_engine.append_messages(conv["id"], [
            {"role": "user", "content": f"m{i}"} for i in range(9)
        ])
        # 9 messages : le tour suivant en ajoute 2, donc franchit 10.
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._envoyer(ws, "dixième", conversation_id=conv["id"])
            self._attendre(lambda: self.consolidations,
                           quoi="la consolidation du franchissement du seuil")
            self._envoyer(ws, "onzième", conversation_id=conv["id"])

        self.assertEqual(self.consolidations, [conv["id"]],
                         "la consolidation a été relancée au tour suivant")
        self.assertEqual(
            history_engine.get_conversation(conv["id"])["dernière_consolidation"], 11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
