"""Le raisonnement et la réponse partagent le même budget — et ça se voyait pas.

**Le bug**, mesuré chez un destinataire : 613 tokens d'entrée, 2048 produits,
réponse finale **vide**. Un modèle qui pense longtemps consomme tout le plafond
en réfléchissant, et n'écrit jamais sa réponse. Le chat affichait alors une bulle
vide, indiscernable d'un modèle qui n'aurait rien à dire.

**Ce qui n'est pas possible**, et qu'il faut savoir avant de chercher : donner un
budget séparé à la réflexion. ``num_predict`` (Ollama) et ``max_tokens`` (API
compatible OpenAI) sont des plafonds **uniques** ; aucune des deux API n'expose
de quota dédié au raisonnement. Le seul levier est de relever le plafond quand on
sait qu'il devra couvrir deux productions.

**Ce qui est possible, et que le code ignorait** : les deux API DISENT quand
elles coupent — ``done_reason == "length"`` et ``finish_reason == "length"``.
C'est le signal exact du bug, et il était jeté.

Ce fichier verrouille les trois moitiés du traitement :

1. le plafond est relevé quand le raisonnement est actif, **sans écraser** un
   ``max_tokens`` explicite ;
2. la troncature est détectée et remonte dans la sentinelle ``__stats__`` ;
3. une réponse **vide** après troncature produit un message explicite, pas un
   silence.

Usage :
    python test_budget_raisonnement.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les dossiers AVANT tout import de core.*

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import ollama  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import modules.chat.router as routeur_chat  # noqa: E402
from core import llm as module_llm  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.llm import LLMEngine  # noqa: E402
from core.runtime import history_engine  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
_WS = "ws://localhost/ws/chat?token={t}"


def _chunk(content="", thinking=None, done=False, done_reason=None):
    """Chunk Ollama avec les VRAIES classes du client (cf. test_raisonnement_stream)."""
    return ollama.ChatResponse(
        model="qwen3:8b",
        created_at="2026-08-27T12:00:00Z",
        done=done,
        done_reason=done_reason if done else None,
        message=ollama.Message(role="assistant", content=content, thinking=thinking),
        prompt_eval_count=613 if done else None,
        eval_count=2048 if done else None,
        eval_duration=60_000_000_000 if done else None,
        prompt_eval_duration=120_000_000 if done else None,
    )


def _rejouer(chunks, **kwargs):
    """Remplace `ollama_client.chat` et rend (sorties, kwargs de l'appel)."""
    appels: dict = {}

    def faux_chat(**kw):
        appels.update(kw)
        return iter(chunks)

    original = module_llm.ollama_client.chat
    module_llm.ollama_client.chat = faux_chat
    try:
        moteur = LLMEngine(config_path=_CONFIG)
        sortie = list(moteur.stream([{"role": "user", "content": "question"}],
                                    model="qwen3:8b", **kwargs))
    finally:
        module_llm.ollama_client.chat = original
    return sortie, appels


def _stats(sortie):
    for piece in sortie:
        if isinstance(piece, dict) and piece.get("__stats__"):
            return piece
    return {}


class BudgetTest(unittest.TestCase):
    """Le plafond suit l'état du raisonnement — sans écraser un choix explicite."""

    def _num_predict(self, **kwargs) -> int:
        _, appels = _rejouer([_chunk(content="ok"), _chunk(done=True, done_reason="stop")],
                             **kwargs)
        return appels["options"]["num_predict"]

    def test_raisonnement_actif_releve_le_plafond(self):
        moteur = LLMEngine(config_path=_CONFIG)
        attendu = moteur._gen["max_tokens_raisonnement"]
        self.assertGreater(attendu, moteur._gen["max_tokens"],
                           "le plafond de raisonnement doit être plus haut, sinon il ne sert à rien")
        self.assertEqual(self._num_predict(raisonnement=True), attendu)

    def test_raisonnement_coupe_garde_le_plafond_ordinaire(self):
        moteur = LLMEngine(config_path=_CONFIG)
        self.assertEqual(self._num_predict(raisonnement=False), moteur._gen["max_tokens"])

    def test_un_max_tokens_explicite_l_emporte_toujours(self):
        """Les appelants qui en passent un l'ont dimensionné pour leur tâche
        (résumés, agent de code, étapes du pipeline) : le raisonnement n'a pas à
        le doubler dans leur dos."""
        self.assertEqual(self._num_predict(max_tokens=256, raisonnement=True), 256)
        self.assertEqual(self._num_predict(max_tokens=256, raisonnement=False), 256)

    def test_un_config_sans_le_reglage_retombe_sur_l_ancien(self):
        """Un `config.yaml` écrit avant ce champ ne doit pas casser le démarrage."""
        moteur = LLMEngine(config_path=_CONFIG)
        moteur._gen = {"temperature": 0.7, "top_p": 0.9, "max_tokens": 2048}
        self.assertEqual(moteur._budget(None, raisonnement=True), 2048)


class DetectionTroncatureTest(unittest.TestCase):
    """`done_reason` était ignoré — c'est ce qui rendait le bug muet."""

    def test_une_generation_coupee_est_signalee(self):
        sortie, _ = _rejouer([
            _chunk(thinking="je réfléchis longuement"),
            _chunk(done=True, done_reason="length"),
        ])
        self.assertTrue(_stats(sortie)["tronqué"])

    def test_une_generation_terminee_ne_l_est_pas(self):
        sortie, _ = _rejouer([
            _chunk(content="la réponse"),
            _chunk(done=True, done_reason="stop"),
        ])
        self.assertFalse(_stats(sortie)["tronqué"])

    def test_un_done_reason_absent_ne_pretend_rien(self):
        """Un serveur plus ancien peut ne pas le renseigner : on ne devine pas."""
        sortie, _ = _rejouer([
            _chunk(content="la réponse"),
            _chunk(done=True, done_reason=None),
        ])
        self.assertFalse(_stats(sortie)["tronqué"])

    def test_le_texte_et_le_raisonnement_ne_changent_pas(self):
        """Le correctif ne doit rien modifier au flux lui-même."""
        sortie, _ = _rejouer([
            _chunk(thinking="hmm"),
            _chunk(content="17 x 23"),
            _chunk(content=" = 391."),
            _chunk(done=True, done_reason="length"),
        ])
        self.assertEqual("".join(p for p in sortie if isinstance(p, str)), "17 x 23 = 391.")
        self.assertEqual(
            [p["content"] for p in sortie
             if isinstance(p, dict) and p.get("__reasoning__")], ["hmm"])


class SignalDansLeChatTest(unittest.TestCase):
    """Une réponse vide après troncature doit se DIRE, pas se taire."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54321))
        cls.token = get_api_token()

    def setUp(self):
        self._original = routeur_chat.llm.stream
        self.addCleanup(setattr, routeur_chat.llm, "stream", self._original)

    def _poser_flux(self, pieces):
        def faux_stream(messages, model=None, raisonnement=True, **kw):
            return iter(list(pieces))
        routeur_chat.llm.stream = faux_stream

    def _echanger(self):
        conv = history_engine.create_conversation()
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            ws.send_text(json.dumps({"role": "user", "content": "question",
                                     "direct": True, "conversation_id": conv["id"]}))
            trames = []
            while True:
                t = json.loads(ws.receive_text())
                if t["type"] in ("conversation", "meta_message", "titre"):
                    continue
                trames.append(t)
                if t["type"] == "done":
                    return trames, conv["id"]

    def _stats_tronquees(self, tronque: bool):
        return {"__stats__": True, "prompt_tokens": 613, "output_tokens": 2048,
                "eval_duration_ns": 1, "prompt_duration_ns": 1, "tronqué": tronque}

    def test_reponse_vide_apres_troncature_produit_une_erreur_explicite(self):
        """LE symptôme mesuré : le budget épuisé par la réflexion, rien à dire."""
        self._poser_flux([
            {"__reasoning__": True, "content": "je réfléchis très longuement"},
            self._stats_tronquees(True),
        ])
        trames, _ = self._echanger()

        erreurs = [t for t in trames if t["type"] == "error"]
        self.assertEqual(len(erreurs), 1, f"aucun signal : {[t['type'] for t in trames]}")
        self.assertIn("budget", erreurs[0]["content"])
        self.assertIn("raisonnement", erreurs[0]["content"],
                      "le message doit nommer le geste qui lève la panne")

    def test_une_reponse_normale_ne_declenche_rien(self):
        self._poser_flux(["la réponse", self._stats_tronquees(False)])
        trames, _ = self._echanger()
        self.assertEqual([t for t in trames if t["type"] == "error"], [])

    def test_une_reponse_TRONQUEE_mais_non_vide_ne_declenche_pas_l_erreur(self):
        """Coupée en cours de phrase, mais l'utilisateur a du texte : l'indicateur
        part dans les stats, on ne remplace pas une réponse partielle par une
        erreur rouge."""
        self._poser_flux(["un début de rép", self._stats_tronquees(True)])
        trames, _ = self._echanger()
        self.assertEqual([t for t in trames if t["type"] == "error"], [])
        stats = [t for t in trames if t["type"] == "stats"][0]
        self.assertTrue(stats["tronqué"])

    def test_rien_n_est_ecrit_sur_le_disque_quand_la_reponse_est_vide(self):
        """La question reste, la non-réponse n'est pas enregistrée comme un tour."""
        self._poser_flux([
            {"__reasoning__": True, "content": "je réfléchis"},
            self._stats_tronquees(True),
        ])
        _, conv_id = self._echanger()
        roles = [m["role"] for m in history_engine.get_conversation(conv_id)["messages"]]
        self.assertEqual(roles, ["user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
