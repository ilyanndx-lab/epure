#!/usr/bin/env python3
"""Raisonnement sur les tours de lecture d'URL et de recherche web (#76).

Constat mesuré pendant #75 (LM Studio, qwen3.5-9b, 8 192 tokens chargés) : sur
un tour qui injecte une page lue ou des résultats web, le raisonnement d'un
modèle local a consommé 1 390 tokens sur 1 448 (réponse après 245 s), puis,
sur le cas le plus chargé, TOUT le plafond de sortie : 4 096 tokens de
réflexion, réponse vide. Le plafond est unique, réflexion et réponse y
puisent au même endroit.

Ce fichier verrouille les trois parades :

  1. **Raisonnement coupé par défaut** sur ces tours, avec un modèle local
     seulement (un modèle cloud ne coûte rien à la machine et son plafond
     n'est pas en cause), et dit dans la trace (`raisonnement_coupe`). Pour
     LM Studio, le levier est `reasoning_effort: "none"`, le seul mesuré
     efficace (2026-10-19 : 0 token de raisonnement, contre 400/400 pour
     `chat_template_kwargs.enable_thinking: false` et `reasoning: "off"`,
     ignorés en silence). Ollama et FLM gardent leur `think: false`, mesuré.
  2. **Détection** d'un raisonnement produit malgré tout (vu sur un appareil
     LM Link distant) : étape `raisonnement_non_coupe`.
  3. **Réserve de 512 tokens pour la réponse** : un raisonnement qui atteint
     le plafond de sortie moins 512 est interrompu, et la réponse est
     relancée sans raisonnement (`raisonnement_plafonne`) — une seule fois.

Usage :
    python test_raisonnement_tours_web.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — avant tout import de core.*

from core.llm import LLMEngine  # noqa: E402
from test_chat_lecture_url import _URL, _Base, routeur_chat  # noqa: E402
from test_tool_calling_lmstudio import _CONFIG, _HISTORIQUE, _chunk, _ClientFactice, _usage  # noqa: E402

_RESERVE = 512


def _raisonnement(n):
    return [{"__reasoning__": True, "content": "pensée "} for _ in range(n)]


class ReasoningEffortLMStudioTest(unittest.TestCase):
    def setUp(self):
        self.moteur = LLMEngine(config_path=_CONFIG)

    def _corps(self, provider, raisonnement):
        client = _ClientFactice([[_chunk(content="ok"), _usage(10, 1)]])
        list(self.moteur._stream_openai(list(_HISTORIQUE), "m", client, provider,
                                        raisonnement=raisonnement))
        return client.appels[0]

    def test_lmstudio_raisonnement_coupe_envoie_reasoning_effort_none(self):
        self.assertEqual(self._corps("lmstudio", False).get("extra_body"),
                         {"reasoning_effort": "none"})

    def test_lmstudio_raisonnement_actif_corps_inchange(self):
        self.assertNotIn("extra_body", self._corps("lmstudio", True))

    def test_flm_garde_think(self):
        self.assertEqual(self._corps("flm", False).get("extra_body"), {"think": False})

    def test_fournisseurs_cloud_rien_de_nouveau(self):
        for provider in ("groq", "mistral", "deepseek"):
            with self.subTest(provider=provider):
                self.assertNotIn("extra_body", self._corps(provider, False))


class ReserveReponseTest(unittest.TestCase):
    """`stream(..., reserve_reponse=512)` : réserve garantie à la réponse."""

    def setUp(self):
        self.moteur = LLMEngine(config_path=_CONFIG)
        self.appels = []
        self.scripts = []

        def faux_brut(messages, model=None, max_tokens=None, raisonnement=True, **kw):
            self.appels.append({"max_tokens": max_tokens, "raisonnement": raisonnement})
            yield from self.scripts.pop(0)

        patcher = mock.patch.object(self.moteur, "_stream_brut", side_effect=faux_brut)
        patcher.start()
        self.addCleanup(patcher.stop)
        #: Plafond de sortie d'un tour à raisonnement coupé (config.yaml).
        self.plafond = self.moteur._budget(None, False)

    def _flux(self):
        return list(self.moteur.stream(list(_HISTORIQUE), model="lmstudio:m",
                                       raisonnement=False, reserve_reponse=_RESERVE))

    def test_raisonnement_court_rien_ne_change(self):
        self.scripts = [_raisonnement(10) + ["réponse"]]
        sortie = self._flux()
        self.assertEqual(len(self.appels), 1)
        self.assertIn("réponse", sortie)
        self.assertFalse([s for s in sortie if isinstance(s, dict) and "__raisonnement_plafonne__" in s])

    def test_raisonnement_au_plafond_moins_la_reserve_coupe_et_relance(self):
        limite = self.plafond - _RESERVE
        self.scripts = [_raisonnement(limite + 50) + ["jamais atteint"], ["réponse relancée"]]
        sortie = self._flux()
        self.assertNotIn("jamais atteint", sortie)
        self.assertIn("réponse relancée", sortie)
        # Pas une pensée de plus que la limite avant la coupe.
        self.assertEqual(sum(1 for s in sortie if isinstance(s, dict) and s.get("__reasoning__")), limite)
        sentinelle = next(s for s in sortie if isinstance(s, dict) and "__raisonnement_plafonne__" in s)
        self.assertEqual(sentinelle["tokens_raisonnement"], limite)
        self.assertEqual(sentinelle["reserve_reponse"], _RESERVE)
        # Relance sans raisonnement, avec au moins la réserve pour répondre.
        self.assertEqual(len(self.appels), 2)
        self.assertFalse(self.appels[1]["raisonnement"])
        self.assertGreaterEqual(self.appels[1]["max_tokens"], _RESERVE)

    def test_une_seule_relance(self):
        limite = self.plafond - _RESERVE
        self.scripts = [_raisonnement(limite + 1), _raisonnement(limite + 1) + ["x"], ["jamais"]]
        sortie = self._flux()
        self.assertEqual(len(self.appels), 2)
        sentinelles = [s for s in sortie if isinstance(s, dict) and "__raisonnement_plafonne__" in s]
        self.assertEqual(len(sentinelles), 2)
        self.assertTrue(sentinelles[1]["relance"])

    def test_sans_reserve_comportement_historique(self):
        self.scripts = [_raisonnement(self.plafond) + ["fin"]]
        sortie = list(self.moteur.stream(list(_HISTORIQUE), model="lmstudio:m", raisonnement=False))
        self.assertEqual(len(self.appels), 1)
        self.assertIn("fin", sortie)


class FluxLMStudioReelTest(unittest.TestCase):
    """De bout en bout depuis la FORME RÉELLE d'un flux LM Studio (mesurée le
    2026-10-19 sur qwen3.5-9b : `delta.reasoning_content`, un token par chunk),
    pas depuis des sentinelles toutes faites. Sans ce test, la détection et la
    réserve passaient leurs tests alors que `_stream_openai` ne remontait
    AUCUN raisonnement de LM Studio (garde réservée à `flm`) : elles étaient
    aveugles sur le cas même qui a motivé #76."""

    def _chunk_raisonnement(self, texte):
        from openai.types.chat import ChatCompletionChunk
        from openai.types.chat.chat_completion_chunk import Choice, ChoiceDelta
        delta = ChoiceDelta(role="assistant", content=None)
        delta.reasoning_content = texte  # champ hors modèle du SDK, comme mesuré
        return ChatCompletionChunk(id="c", object="chat.completion.chunk", created=1, model="m",
                                   choices=[Choice(index=0, delta=delta, finish_reason=None)])

    def test_raisonnement_lmstudio_compte_et_plafonne(self):
        moteur = LLMEngine(config_path=_CONFIG)
        plafond = moteur._budget(None, False)
        limite = plafond - _RESERVE
        rounds = [
            [self._chunk_raisonnement("pensée ") for _ in range(limite + 5)] + [_chunk(content="trop tard")],
            [_chunk(content="réponse relancée"), _usage(10, 3)],
        ]
        client = _ClientFactice(rounds)
        with mock.patch.object(moteur, "_openai_client", return_value=client):
            sortie = list(moteur.stream(list(_HISTORIQUE), model="lmstudio:m",
                                        raisonnement=False, reserve_reponse=_RESERVE))
        self.assertEqual(sum(1 for s in sortie if isinstance(s, dict) and s.get("__reasoning__")), limite)
        self.assertTrue(any(isinstance(s, dict) and s.get("__raisonnement_plafonne__") for s in sortie))
        self.assertIn("réponse relancée", sortie)
        self.assertNotIn("trop tard", sortie)
        # Les deux appels demandaient le raisonnement coupé.
        self.assertEqual([a.get("extra_body") for a in client.appels],
                         [{"reasoning_effort": "none"}] * 2)


class _BaseTour(_Base):
    """Le faux `llm.stream` du chat, qui capture aussi les arguments du tour."""

    #: Ce que le faux flux rend, par défaut une réponse simple.
    flux = ["réponse"]

    def setUp(self):
        super().setUp()
        self.appels_stream = []

        def faux_stream(messages, model=None, raisonnement=True, **kw):
            self.prompts.append(list(messages))
            self.appels_stream.append({"model": model, "raisonnement": raisonnement, **kw})
            return iter(list(self.flux))

        routeur_chat.llm.stream = faux_stream
        patcher = mock.patch.object(routeur_chat, "fenetre_de", return_value=(None, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _modele(self, modele):
        original = routeur_chat.memory.get_context
        patcher = mock.patch.object(routeur_chat.memory, "get_context",
                                    lambda: {**original(), "modèle_actif": modele})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _etapes(self, conv_id, nom):
        return [e for e in self._trace(conv_id) if e["etape"] == nom]


class RaisonnementCoupeTest(_BaseTour):
    def test_lecture_url_modele_local_coupe_avec_reserve(self):
        self._modele("lmstudio:huihui-qwen3.5-9b-abliterated")
        _trames, conv_id = self._envoyer(f"Que dit cette page ? {_URL}")
        appel = self.appels_stream[-1]
        self.assertFalse(appel["raisonnement"])
        self.assertEqual(appel["reserve_reponse"], _RESERVE)
        etape = self._etapes(conv_id, "raisonnement_coupe")
        self.assertEqual(len(etape), 1)
        self.assertEqual(etape[0]["raison"], "lecture_url")

    def test_recherche_web_modele_local_coupe(self):
        self._modele("qwen2.5:7b")
        _trames, conv_id = self._envoyer("quelle est la météo à Lyon", web_search_override=True)
        self.assertFalse(self.appels_stream[-1]["raisonnement"])
        self.assertEqual(self._etapes(conv_id, "raisonnement_coupe")[0]["raison"], "contenu_web")

    def test_modele_cloud_inchange(self):
        self._modele("mistral:mistral-small-latest")
        _trames, conv_id = self._envoyer(f"Que dit cette page ? {_URL}")
        appel = self.appels_stream[-1]
        self.assertTrue(appel["raisonnement"])
        self.assertFalse(appel.get("reserve_reponse"))
        self.assertEqual(self._etapes(conv_id, "raisonnement_coupe"), [])

    def test_tour_sans_contenu_web_inchange(self):
        self._modele("lmstudio:huihui-qwen3.5-9b-abliterated")
        _trames, conv_id = self._envoyer("explique-moi le théorème de Rolle")
        appel = self.appels_stream[-1]
        self.assertTrue(appel["raisonnement"])
        self.assertFalse(appel.get("reserve_reponse"))
        self.assertEqual(self._etapes(conv_id, "raisonnement_coupe"), [])


class RaisonnementMalgreToutTest(_BaseTour):
    def test_raisonnement_produit_malgre_la_coupure_signale_une_fois(self):
        self._modele("lmstudio:huihui-qwen3.5-9b-abliterated")
        self.flux = _raisonnement(5) + ["réponse"]
        _trames, conv_id = self._envoyer(f"Que dit cette page ? {_URL}")
        self.assertEqual(len(self._etapes(conv_id, "raisonnement_non_coupe")), 1)

    def test_raisonnement_plafonne_dans_la_trace(self):
        self._modele("lmstudio:huihui-qwen3.5-9b-abliterated")
        self.flux = _raisonnement(3) + [
            {"__raisonnement_plafonne__": True, "tokens_raisonnement": 1536,
             "plafond_sortie": 2048, "reserve_reponse": 512, "relance": False},
            "réponse relancée",
        ]
        trames, conv_id = self._envoyer(f"Que dit cette page ? {_URL}")
        etape = self._etapes(conv_id, "raisonnement_plafonne")
        self.assertEqual(len(etape), 1)
        self.assertEqual(etape[0]["tokens_raisonnement"], 1536)
        self.assertEqual(etape[0]["reserve_reponse"], 512)
        self.assertIn("réponse relancée", "".join(t["content"] for t in trames if t["type"] == "token"))

    def test_relance_qui_raisonne_encore_erreur_nommee(self):
        """Deux coupes, aucune réponse : pas de bulle vide."""
        self._modele("lmstudio:huihui-qwen3.5-9b-abliterated")
        sentinelle = {"__raisonnement_plafonne__": True, "tokens_raisonnement": 1536,
                      "plafond_sortie": 2048, "reserve_reponse": 512}
        self.flux = [{**sentinelle, "relance": False}, {**sentinelle, "relance": True}]
        trames, _conv_id = self._envoyer(f"Que dit cette page ? {_URL}")
        erreurs = [t for t in trames if t["type"] == "error"]
        self.assertEqual(len(erreurs), 1)
        self.assertIn("raisonnement", erreurs[0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
