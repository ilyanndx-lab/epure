#!/usr/bin/env python3
"""`LLMEngine.generate()` LÈVE en cas d'échec, il ne rend jamais le message
d'échec comme s'il était la réponse du modèle — cf. docstring de
`core/llm.py::LLMEngine.generate`.

Incident reproduit (2026-09-22) : FLM éteint, `modele_local_defaut()` valant
``flm:qwen3:4b``. `_generate_openai` attrapait l'exception du SDK et
RETOURNAIT ``"[flm:qwen3:4b] échec d'appel : Connection error."`` — une
chaîne courte, sur une ligne, sans backtick : elle passait toutes les
heuristiques de `_generate_title` et de `calibrer_prompt`, et s'affichait
telle quelle comme titre de conversation ou comme prompt calibré.

La panne est simulée UNE COUCHE SOUS `generate()` (le client openai lève),
pas en faisant retourner la chaîne à un `generate` mocké : c'est le vrai
`generate` qui doit transformer l'échec en exception — un mock qui
retournerait le texte testerait une détection de motif que le correctif a
délibérément écartée.

Usage :
    python test_generate_echec_leve.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

import core.history as core_history  # noqa: E402
import modules.image.router as image_router  # noqa: E402
from core.history import HistoryEngine  # noqa: E402
from core.runtime import llm  # noqa: E402

_MODELE_FLM = "flm:qwen3:4b"


class _ClientQuiLeve:
    """Client openai minimal dont `chat.completions.create` échoue comme le
    SDK quand FLM n'écoute pas (`openai.APIConnectionError`, message
    « Connection error. »)."""

    def __init__(self):
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        raise Exception("Connection error.")


class _BasePanneFlm(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(llm, "_openai_client", return_value=_ClientQuiLeve())
        p.start()
        self.addCleanup(p.stop)


class GenerateLeveTest(_BasePanneFlm):
    def test_generate_leve_avec_le_message_actionnable(self):
        with self.assertRaises(RuntimeError) as ctx:
            llm.generate([{"role": "user", "content": "x"}], model=_MODELE_FLM)
        # Le message reste celui de `_provider_error_message` : il sort
        # désormais par l'exception (logs, SSE d'erreur), pas par le retour.
        self.assertIn("[flm:qwen3:4b]", str(ctx.exception))

    def test_gemini_sans_cle_leve_aussi(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            # Mêmes types que `_stream_gemini` : ValueError sans clé,
            # RuntimeError si le paquet manque.
            with self.assertRaises((RuntimeError, ValueError)):
                llm.generate([{"role": "user", "content": "x"}], model="gemini:gemini-2.0-flash")


class TitreConversationFlmEteintTest(_BasePanneFlm):
    def test_repli_sur_le_titre_date(self):
        moteur = object.__new__(HistoryEngine)
        moteur._llm = llm  # noqa: SLF001
        with mock.patch.object(core_history, "modele_local_defaut", return_value=_MODELE_FLM), \
             self.assertLogs("core.history", level="ERROR"):
            titre = HistoryEngine._generate_title(
                moteur, [{"role": "user", "content": "bonjour"}])
        self.assertNotIn("[flm:", titre)
        self.assertTrue(titre.startswith("Conversation du "), titre)


class CalibrerPromptFlmEteintTest(_BasePanneFlm):
    def test_repli_sur_le_prompt_brut(self):
        with mock.patch.object(image_router, "modele_local_defaut", return_value=_MODELE_FLM):
            out = image_router.calibrer_prompt("un renard roux")
        self.assertEqual(out, "un renard roux")


if __name__ == "__main__":
    unittest.main()
