#!/usr/bin/env python3
"""LM Studio comme quatrième fournisseur OpenAI-compatible — le ROUTAGE, pas la
détection.

`test_models_lmstudio.py` couvre `check_lmstudio()`/`get_lmstudio_installed()`
et les blocs `local_lmstudio` de `GET /models`/`GET /health` (commit 2699561,
« LM Studio comme troisième fournisseur local ») : un modèle LM Studio se
laissait déjà DÉCOUVRIR par ces routes. Mais le sélectionner ne l'aurait pas
réellement appelé — `_OPENAI_COMPAT` (`core/llm.py`) n'avait aucune entrée
`lmstudio`, et `LLMEngine._parse_model` ne reconnaît un préfixe QUE s'il y
figure (ou vaut `"gemini"`) : sans elle, `lmstudio:llama-3.1-8b-instruct`
retombait sur le repli par défaut, `("ollama", model)` — donc partait vers
Ollama avec un `model_id` qu'il ne connaît pas, sans la moindre erreur qui
distingue ce cas d'un modèle Ollama absent.

Fichier DÉDIÉ plutôt qu'une extension de `test_models_lmstudio.py`, même
raisonnement que celui que CE fichier tient déjà pour `test_vision_images.py` :
la détection d'un fournisseur (HTTP direct, `urllib`) et le routage de
génération (`LLMEngine.stream`, le SDK `openai`) sont deux sujets sans rapport,
qui ne doivent pas se recasser l'un l'autre en silence si on les mélange.

Ce que ces tests gardent :

1. `_parse_model("lmstudio:...")` rend `("lmstudio", "...")`, et l'entrée
   existe bien dans `_OPENAI_COMPAT` ;
2. `_openai_client("lmstudio")` ne demande AUCUNE clé d'API (serveur local,
   même contrat que `flm`) et construit son `base_url` depuis
   `core.llm.lmstudio_host` — configurable via `LMSTUDIO_HOST`, jamais un port
   en dur ;
3. `LLMEngine.stream()` route un modèle `lmstudio:` vers `_stream_openai`,
   JAMAIS vers `_stream_ollama` — c'est exactement le bug qu'une entrée
   manquante aurait reproduit silencieusement ;
4. la bascule `raisonnement` reste INERTE pour `lmstudio`, au même titre que
   groq/cerebras/mistral/nvidia/deepseek (cf. le docstring de
   `LLMEngine._stream_openai`) : LM Studio expose deux mécanismes concurrents
   selon le modèle chargé (`reasoning_effort` / `chat_template_kwargs.
   enable_thinking`), et son propre suivi de bugs rapporte le second ignoré
   sur certains modèles récents (Qwen3.5). Rien de mesuré sur un vrai serveur
   LM Studio ici, donc rien de câblé tant que ça ne l'est pas.

Usage :
    python test_llm_lmstudio.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.*

os.environ.setdefault("HF_HUB_OFFLINE", "1")

from openai.types.chat import ChatCompletionChunk  # noqa: E402
from openai.types.chat.chat_completion_chunk import (  # noqa: E402
    Choice as ChoiceOai, ChoiceDelta,
)
from openai.types.completion_usage import CompletionUsage  # noqa: E402

from core import llm as module_llm  # noqa: E402
from core.llm import LLMEngine  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


def _chunk_oai(content=None, reasoning=None, usage=None):
    """Chunk OpenAI-compatible minimal, avec les VRAIES classes du SDK.

    Construction reprise de `test_raisonnement_stream._chunk_oai` et dupliquée
    ici plutôt qu'importée : chaque test `test_*.py` reste autonome (CLAUDE.md
    §2 — pas de dossier `tests/` partagé, chaque fichier fait son propre
    `sys.path.insert`).
    """
    champs = {"role": "assistant", "content": content}
    if reasoning is not None:
        champs["reasoning_content"] = reasoning
    delta = ChoiceDelta(**champs)
    return ChatCompletionChunk(
        id="chatcmpl-test", object="chat.completion.chunk", created=1787582789,
        model="un-modele",
        choices=[] if usage is not None else [
            ChoiceOai(index=0, delta=delta, finish_reason=None)],
        usage=usage,
    )


def _raisonnements(sortie):
    return [p["content"] for p in sortie if isinstance(p, dict) and p.get("__reasoning__")]


def _textes(sortie):
    return [p for p in sortie if isinstance(p, str)]


class ParseModelTest(unittest.TestCase):

    def test_prefixe_lmstudio_reconnu(self):
        self.assertEqual(
            LLMEngine._parse_model("lmstudio:llama-3.1-8b-instruct"),
            ("lmstudio", "llama-3.1-8b-instruct"),
        )

    def test_entree_presente_dans_openai_compat(self):
        """La condition dont dépend le point précédent : sans elle,
        `_parse_model` ne reconnaît aucun préfixe `lmstudio` et retombe sur
        `("ollama", model)`."""
        self.assertIn("lmstudio", module_llm._OPENAI_COMPAT)


class ClientLmStudioTest(unittest.TestCase):
    """`_openai_client("lmstudio")` : mêmes garanties que pour `flm`."""

    def test_aucune_cle_api_requise(self):
        moteur = LLMEngine(config_path=_CONFIG)
        client = moteur._openai_client("lmstudio")
        self.assertEqual(client.api_key, "not-needed")

    def test_base_url_derive_de_lmstudio_host(self):
        """Pas un port en dur : `core.llm.lmstudio_host`, dérivé de
        `LMSTUDIO_HOST` — LM Studio est une application de bureau dont le port
        se change dans ses réglages, contrairement à celui de `flm`."""
        moteur = LLMEngine(config_path=_CONFIG)
        client = moteur._openai_client("lmstudio")
        self.assertEqual(str(client.base_url).rstrip("/"),
                         f"{module_llm.lmstudio_host}/v1")


class StreamRoutageTest(unittest.TestCase):
    """`stream()` doit appeler `_stream_openai`, jamais `_stream_ollama` — le
    cœur du correctif."""

    def test_route_vers_openai_compatible_pas_ollama(self):
        appels_ollama = []
        original = module_llm.ollama_client.chat
        module_llm.ollama_client.chat = lambda **kw: appels_ollama.append(kw) or iter([])
        try:
            with mock.patch.object(LLMEngine, "_stream_openai",
                                   return_value=iter(["ok"])) as m:
                moteur = LLMEngine(config_path=_CONFIG)
                sortie = list(moteur.stream(
                    [{"role": "user", "content": "?"}], model="lmstudio:qwen2.5-7b"))
        finally:
            module_llm.ollama_client.chat = original

        self.assertEqual(sortie, ["ok"])
        self.assertEqual(appels_ollama, [], "n'aurait jamais dû atteindre Ollama")
        messages, model_id, client, provider, max_tokens = m.call_args.args
        self.assertEqual(model_id, "qwen2.5-7b")
        self.assertEqual(provider, "lmstudio")


class BasculeRaisonnementInerteTest(unittest.TestCase):
    """La bascule `raisonnement` ne doit RIEN envoyer de neuf à LM Studio.

    Même statut que groq/cerebras/mistral/nvidia/deepseek (cf.
    `test_raisonnement_stream.BasculeFlmTest.
    test_les_fournisseurs_cloud_ne_recoivent_rien_de_neuf`), mais pour une
    raison propre à LM Studio et pas seulement « pas mesuré » : voir le
    docstring de `LLMEngine._stream_openai`.
    """

    def _capturer(self, **kwargs):
        vus = {}

        class _Completions:
            def create(_self, **kw):
                vus.update(kw)
                return iter([])

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        moteur = LLMEngine(config_path=_CONFIG)
        list(moteur._stream_openai([{"role": "user", "content": "17 x 23 ?"}],
                                   "un-modele", _Client(), "lmstudio", 300, **kwargs))
        return vus

    def test_aucun_extra_body_quelle_que_soit_la_valeur(self):
        for valeur in (True, False):
            vus = self._capturer(raisonnement=valeur)
            self.assertNotIn("extra_body", vus, valeur)

    def test_le_raisonnement_ne_remonte_pas_meme_si_le_champ_existe(self):
        """Même forme de flux que FLM (`reasoning_content` présent) — la garde
        sur `provider` doit l'ignorer côté LM Studio comme côté cloud."""
        mesure = [
            _chunk_oai(reasoning=""),
            _chunk_oai(reasoning="Okay, 17 x 23"),
            _chunk_oai(reasoning=". Let me compute."),
            _chunk_oai(content="17 x 23"),
            _chunk_oai(content=" = 391."),
            _chunk_oai(usage=CompletionUsage(prompt_tokens=28, completion_tokens=742,
                                            total_tokens=770)),
        ]

        class _Completions:
            def create(_self, **kw):
                return iter(mesure)

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        moteur = LLMEngine(config_path=_CONFIG)
        sortie = list(moteur._stream_openai(
            [{"role": "user", "content": "17 x 23 ?"}], "un-modele",
            _Client(), "lmstudio", 300))
        self.assertEqual(_raisonnements(sortie), [])
        self.assertEqual("".join(_textes(sortie)), "17 x 23 = 391.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
