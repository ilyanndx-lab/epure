"""Comparaison multi-modèles côte à côte — chemin PARALLÈLE au mono-modèle.

Verrouille : validation stricte (2-3 modèles, tous réellement disponibles,
tout ou rien, jamais de troncature silencieuse), routage des événements
`compare_*` par modèle indépendamment de l'ordre d'arrivée, persistance d'UNE
SEULE réponse (celle choisie) et absence totale des autres sur le disque,
résolution d'un état périmé sans crash, et la précédence de `compare_models`
sur `effort`/`steps`.

Usage :
    python test_chat_comparaison.py
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
from core.paths import resolve_history_dir  # noqa: E402
from core.runtime import history_engine  # noqa: E402

#: URL ABSOLUE — cf. test_chat_ws_conversation.py, même piège TrustedHostMiddleware.
_WS = "ws://localhost/ws/chat?token={t}"

_DISPONIBLES = {"modele-a", "modele-b", "modele-c"}


async def _faux_ids_disponibles(_registry):
    """Remplace `core.models.ids_disponibles` — pas de réseau, pas d'Ollama/FLM
    réel dans ces tests, juste un ensemble fixe de modèles « disponibles »."""
    return set(_DISPONIBLES)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54322))
        cls.token = get_api_token()

    def setUp(self):
        self._ids_original = routeur_chat.ids_disponibles
        self.addCleanup(setattr, routeur_chat, "ids_disponibles", self._ids_original)
        routeur_chat.ids_disponibles = _faux_ids_disponibles

        self.appels: list[str] = []
        self._stream_original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._stream_original)
        self._poser_reponses({
            "modele-a": ["Bon", "jour"],
            "modele-b": ["Hello", " world"],
            "modele-c": ["Hola", " mundo"],
        })

        # Titrage neutralisé — cf. test_chat_ws_conversation.py : `history_engine`
        # est un `_LazyEngine`, patcher l'instance ne serait pas vu.
        self._titre_original = HistoryEngine._generate_title
        self.addCleanup(setattr, HistoryEngine, "_generate_title", self._titre_original)
        HistoryEngine._generate_title = lambda self_, messages: "Titre auto"

    def _poser_reponses(self, par_modele: dict):
        """Séquences DISTINCTES et déterministes par modèle — pour vérifier le
        routage des `compare_*`, pas seulement leur présence."""
        def faux_stream(messages, model=None, raisonnement=True, **kw):
            self.appels.append(model)
            return iter(list(par_modele.get(model, [f"réponse de {model}"])))
        routeur_chat.llm.stream = faux_stream

    def _comparer(self, ws, texte, modeles, conversation_id=None):
        """Lance une comparaison ; rend (trames reçues, id de conversation)."""
        corps = {"role": "user", "content": texte, "compare_models": modeles}
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
            if t["type"] in ("compare_all_done", "error"):
                return trames, annonce

    def _choisir(self, ws, conv_id, modele):
        ws.send_text(json.dumps({
            "type": "compare_choix", "conv_id": conv_id, "model": modele,
        }))
        while True:
            t = json.loads(ws.receive_text())
            if t["type"] in ("done", "error"):
                return t


class ValidationTest(_Base):
    """Tout ou rien : au moindre problème, AUCUNE génération n'est lancée —
    ni pour les IDs invalides, ni pour les valides du même lot."""

    def test_quatre_modeles_refuses_sans_generation(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, _ = self._comparer(
                ws, "question", ["modele-a", "modele-b", "modele-c", "modele-d"])
        self.assertEqual(trames[-1]["type"], "error")
        self.assertEqual(self.appels, [])

    def test_un_seul_modele_est_refuse(self):
        """2 est le minimum — comparer un modèle à lui-même n'a pas de sens."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, _ = self._comparer(ws, "question", ["modele-a"])
        self.assertEqual(trames[-1]["type"], "error")
        self.assertEqual(self.appels, [])

    def test_modele_indisponible_nomme_et_aucune_generation(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, _ = self._comparer(ws, "question", ["modele-a", "modele-inconnu"])
        self.assertEqual(trames[-1]["type"], "error")
        self.assertIn("modele-inconnu", trames[-1]["content"])
        self.assertEqual(self.appels, [],
                         "modele-a est valide mais ne doit pas démarrer non plus")


class RoutingTest(_Base):
    def test_chaque_compare_token_porte_le_bon_modele_et_contenu(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, _ = self._comparer(
                ws, "question", ["modele-a", "modele-b", "modele-c"])

        par_modele: dict[str, str] = {}
        for t in trames:
            if t["type"] == "compare_token":
                par_modele[t["model"]] = par_modele.get(t["model"], "") + t["content"]

        # Pas d'hypothèse d'ordre entre modèles : on regroupe par modèle et on
        # compare le texte reconstruit, quel que soit l'ordre d'arrivée réel.
        self.assertEqual(par_modele.get("modele-a"), "Bonjour")
        self.assertEqual(par_modele.get("modele-b"), "Hello world")
        self.assertEqual(par_modele.get("modele-c"), "Hola mundo")

        dones = {t["model"] for t in trames if t["type"] == "compare_done"}
        self.assertEqual(dones, {"modele-a", "modele-b", "modele-c"})
        self.assertEqual(trames[-1]["type"], "compare_all_done")

    def test_aucune_trame_mono_modele_en_mode_comparaison(self):
        """Deux vocabulaires disjoints — un `token`/`stats`/`reasoning` brut ne
        doit jamais fuiter en mode comparaison."""
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            trames, _ = self._comparer(ws, "question", ["modele-a", "modele-b"])
        types = {t["type"] for t in trames}
        self.assertFalse(types & {"token", "stats", "reasoning", "done"})


class PersistenceTest(_Base):
    def test_seul_le_modele_choisi_est_persiste(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            _, conv_id = self._comparer(ws, "question", ["modele-a", "modele-b"])
            resultat = self._choisir(ws, conv_id, "modele-b")

        self.assertEqual(resultat["type"], "done")
        self.assertEqual(resultat["modèle"], "modele-b")

        conv = history_engine.get_conversation(conv_id)
        assistants = [m for m in conv["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(assistants), 1, "une seule réponse assistant persistée")
        self.assertEqual(assistants[0]["content"], "Hello world")
        self.assertEqual(assistants[0]["modèle"], "modele-b")

        # Nulle part le texte de la réponse écartée — y compris dans le
        # fichier BRUT, pas seulement dans la vue reconstruite par l'API.
        brut = (resolve_history_dir() / f"{conv_id}.json").read_text(encoding="utf-8")
        self.assertNotIn("Bonjour", brut)


class EtatPerimeTest(_Base):
    def test_choix_sans_comparaison_en_cours_ne_plante_pas(self):
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            resultat = self._choisir(ws, conv["id"], "modele-a")
        self.assertEqual(resultat["type"], "error")
        self.assertEqual(history_engine.get_conversation(conv["id"])["messages"], [])

    def test_double_choix_le_second_est_refuse(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            _, conv_id = self._comparer(ws, "question", ["modele-a", "modele-b"])
            premier = self._choisir(ws, conv_id, "modele-a")
            second = self._choisir(ws, conv_id, "modele-b")
        self.assertEqual(premier["type"], "done")
        self.assertEqual(second["type"], "error")
        assistants = [
            m for m in history_engine.get_conversation(conv_id)["messages"]
            if m["role"] == "assistant"
        ]
        self.assertEqual(len(assistants), 1)


class PrecedenceTest(_Base):
    """`compare_models` ET `effort`/`steps` fournis ensemble : la comparaison
    l'emporte, le pipeline orchestrateur n'est jamais consulté."""

    def test_compare_models_prime_sur_effort(self):
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            corps = {
                "role": "user", "content": "question",
                "compare_models": ["modele-a", "modele-b"],
                "effort": "high", "steps": [{"role": "x", "model": "y"}],
            }
            ws.send_text(json.dumps(corps))
            trames = []
            while True:
                t = json.loads(ws.receive_text())
                if t["type"] == "conversation":
                    continue
                trames.append(t)
                if t["type"] in ("compare_all_done", "error", "pipeline_info"):
                    break

        types = {t["type"] for t in trames}
        self.assertNotIn("pipeline_info", types,
                         "le pipeline ne doit jamais démarrer en mode comparaison")
        self.assertIn("compare_all_done", types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
