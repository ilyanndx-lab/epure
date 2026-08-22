"""Le chemin d'un message ne doit appeler AUCUN LLM pour préparer la mémoire.

`MemoryEngine.retrieve_relevant_context` demandait au modèle local (Ollama)
quelles sections du profil injecter, sous un `future.result(timeout=2.0)`.
Mesuré sur `epure_tray.log` avant correction : 30 appels, 30 timeouts, zéro
sélection utilisée — donc 2,000 s ajoutées à **chaque** message, y compris vers
un fournisseur cloud qui n'a rien à voir avec Ollama. Pire, le timeout
n'annulait rien (`shutdown(wait=False)` ne tue pas le thread) : l'appel
continuait de charger le modèle, mesuré à 13,8 s à froid dont 10,2 s de
chargement, en concurrence disque avec la requête cloud qui suivait.

Ce fichier tient la frontière : ce n'est pas une relecture du code mais une
exécution avec un LLM piégé, qui lève si on le touche. Le régresser demande donc
de faire échouer un test, pas d'oublier une revue.

Usage :
    python test_memory_sans_llm.py
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.*

from core.memory import MemoryEngine  # noqa: E402


class LLMPiégé:
    """Tout appel depuis le chemin d'un message est un échec de test.

    Porte `_model` parce que d'autres moteurs le lisent (`core/orchestrator.py`) :
    le piège doit ressembler à un vrai moteur, sinon il échouerait pour la
    mauvaise raison.
    """

    _model = "modèle-piégé"

    def __init__(self):
        self.appels = 0

    def generate(self, *args, **kwargs):
        self.appels += 1
        raise AssertionError("appel LLM interdit sur le chemin d'un message")

    def stream(self, *args, **kwargs):
        self.appels += 1
        raise AssertionError("appel LLM interdit sur le chemin d'un message")


class TestMémoireSansLLM(unittest.TestCase):
    def setUp(self):
        self.llm = LLMPiégé()
        self.moteur = MemoryEngine(llm=self.llm)
        with self.moteur.profile_transaction() as profil:
            profil["forces"] = ["théorèmes"]
            profil["lacunes_confirmées"] = ["formules"]
            profil["préférences_interaction"] = {"style": "direct", "ne_pas_faire": []}

    def test_retrieve_n_appelle_pas_le_llm(self):
        sections = self.moteur.retrieve_relevant_context(
            "une question suffisamment longue pour dépasser le seuil"
        )
        self.assertEqual(self.llm.appels, 0)
        self.assertEqual(sorted(sections), ["forces", "lacunes", "style"])

    def test_build_system_context_n_appelle_pas_le_llm(self):
        """C'est le vrai point d'entrée : `modules/chat/router.py` appelle celui-ci."""
        contexte = self.moteur.build_system_context(
            "une question suffisamment longue pour dépasser le seuil"
        )
        self.assertEqual(self.llm.appels, 0)
        self.assertIn("[PROFIL ÉLÈVE]", contexte)
        # Les trois sections disponibles sont injectées, plus de sélection.
        self.assertIn("direct", contexte)
        self.assertIn("théorèmes", contexte)
        self.assertIn("formules", contexte)

    def test_message_court_reste_court_circuite(self):
        self.assertEqual(self.moteur.retrieve_relevant_context("court"), [])
        self.assertEqual(self.llm.appels, 0)

    def test_cout_negligeable(self):
        """Le budget ici est de l'ordre de la milliseconde (mesuré ~5 ms sur
        les données réelles). Le seuil est lâche à 0,5 s : il ne mesure pas une
        performance, il attrape le retour d'un appel réseau ou d'un chargement de
        modèle sur ce chemin — l'ancien code prenait 2,000 s ferme.
        """
        message = "une question suffisamment longue pour dépasser le seuil"
        self.moteur.build_system_context(message)  # hors mesure : premières lectures
        début = time.perf_counter()
        for _ in range(5):
            self.moteur.build_system_context(message)
        écoulé = (time.perf_counter() - début) / 5
        self.assertLess(écoulé, 0.5, f"{écoulé:.3f}s par appel — un appel bloquant est revenu")


if __name__ == "__main__":
    unittest.main(verbosity=2)
