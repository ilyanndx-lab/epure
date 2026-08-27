"""La consigne générale survit au redémarrage — le reste du contexte, non.

`context_session.json` est réinitialisé à chaque construction de `MemoryEngine`,
c'est-à-dire à chaque démarrage du backend. C'est sa raison d'être : le modèle
actif, le mode strict, le raisonnement sont des réglages de séance.

`instruction_générale` fait exception depuis le 2026-08-27. Elle s'appelait
`session_instruction`, et le nom disait vrai — elle s'effaçait à chaque
lancement. Ça se défendait tant qu'aucune consigne ne persistait ; ça ne se
défend plus depuis que la conversation en porte une qui, elle, survit.

Ce fichier verrouille les deux moitiés :

* ce qui doit survivre survit, y compris à plusieurs redémarrages ;
* ce qui doit être remis à zéro l'est toujours.

La seconde compte autant que la première : une exception qui déborderait
transformerait des réglages de séance en états collants, et le symptôme
(« pourquoi suis-je encore en mode strict ? ») serait cherché ailleurs.

Usage :
    python test_consigne_generale.py
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les dossiers AVANT tout import de core.*

from core.jsonstore import read_json, write_json  # noqa: E402
from core.memory import _CLES_PERSISTANTES, _CONTEXT_DEFAULT, MemoryEngine  # noqa: E402


class _DossierNeuf(unittest.TestCase):
    """Pose EPURE_DATA_DIR sur un temporaire, APRÈS les imports ci-dessus."""

    def setUp(self):
        self._prev = os.environ.get("EPURE_DATA_DIR")
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-consigne-"))
        os.environ["EPURE_DATA_DIR"] = str(self.tmp)
        self.addCleanup(self._restaurer)

    def _restaurer(self):
        if self._prev is None:
            os.environ.pop("EPURE_DATA_DIR", None)
        else:
            os.environ["EPURE_DATA_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redemarrer(self) -> MemoryEngine:
        """Un nouveau moteur = un redémarrage du backend, pour ce qui nous occupe."""
        return MemoryEngine()

    def _sur_disque(self) -> dict:
        return read_json(self.tmp / "context_session.json", {})


class PersistanceTest(_DossierNeuf):
    def test_la_consigne_survit_a_un_redemarrage(self):
        moteur = self._redemarrer()
        moteur.update_context(**{"instruction_générale": "Réponds toujours en français."})

        self.assertEqual(
            self._redemarrer().get_context()["instruction_générale"],
            "Réponds toujours en français.")

    def test_elle_survit_a_plusieurs_redemarrages(self):
        """Un seul redémarrage ne prouve rien : la valeur pourrait être reprise
        une fois puis perdue au suivant, si la réécriture ne la reposait pas."""
        self._redemarrer().update_context(**{"instruction_générale": "Consigne durable"})
        for _ in range(3):
            moteur = self._redemarrer()
        self.assertEqual(moteur.get_context()["instruction_générale"], "Consigne durable")

    def test_une_consigne_vide_ne_bloque_pas_le_defaut(self):
        moteur = self._redemarrer()
        moteur.update_context(**{"instruction_générale": ""})
        self.assertEqual(self._redemarrer().get_context()["instruction_générale"], "")

    def test_effacer_la_consigne_l_efface_pour_de_bon(self):
        """Vider doit tenir au redémarrage : une consigne qu'on ne peut pas
        retirer serait pire que pas de consigne du tout."""
        self._redemarrer().update_context(**{"instruction_générale": "à retirer"})
        self._redemarrer().update_context(**{"instruction_générale": ""})
        self.assertEqual(self._redemarrer().get_context()["instruction_générale"], "")


class ReinitialisationTest(_DossierNeuf):
    """L'exception ne doit pas déborder sur les réglages de séance."""

    def test_les_autres_cles_sont_bien_remises_a_zero(self):
        moteur = self._redemarrer()
        moteur.update_context(**{
            "modèle_actif": "gemini-2.0-flash",
            "strict_mode": True,
            "raisonnement": False,
        })

        contexte = self._redemarrer().get_context()
        self.assertEqual(contexte["modèle_actif"], _CONTEXT_DEFAULT["modèle_actif"])
        self.assertEqual(contexte["strict_mode"], _CONTEXT_DEFAULT["strict_mode"])
        self.assertEqual(contexte["raisonnement"], _CONTEXT_DEFAULT["raisonnement"])

    def test_une_cle_inconnue_ne_survit_pas(self):
        """Seules les clés NOMMÉES persistent — pas tout ce qui traîne."""
        moteur = self._redemarrer()
        moteur.update_context(**{"cle_parasite": "valeur"})
        self.assertNotIn("cle_parasite", self._redemarrer().get_context())

    def test_la_liste_des_persistantes_est_courte_et_explicite(self):
        """Garde-fou de conception : si cette liste enfle, c'est que le fichier
        n'est plus un contexte de SESSION et qu'il faut le dire autrement."""
        self.assertEqual(_CLES_PERSISTANTES, ("instruction_générale",))


class DemarrageRobusteTest(_DossierNeuf):
    """Un fichier absent, vide ou illisible ne doit pas empêcher de démarrer."""

    def test_sans_fichier_prealable(self):
        self.assertEqual(self._redemarrer().get_context()["instruction_générale"], "")

    def test_sur_un_fichier_illisible(self):
        (self.tmp / "context_session.json").write_text("{pas du JSON", encoding="utf-8")
        contexte = self._redemarrer().get_context()
        self.assertEqual(contexte["instruction_générale"], "")
        self.assertEqual(contexte["modèle_actif"], _CONTEXT_DEFAULT["modèle_actif"])

    def test_sur_une_valeur_du_mauvais_type(self):
        """Un JSON édité à la main peut contenir n'importe quoi ; une consigne
        qui n'est pas du texte ne doit pas partir dans le prompt."""
        write_json(self.tmp / "context_session.json", {"instruction_générale": ["une", "liste"]})
        self.assertEqual(self._redemarrer().get_context()["instruction_générale"], "")


class InjectionTest(_DossierNeuf):
    def test_la_consigne_entre_dans_le_prompt_systeme(self):
        moteur = self._redemarrer()
        moteur.update_context(**{"instruction_générale": "Sois bref."})
        rendu = moteur.build_system_context("une question assez longue pour compter")
        self.assertIn("[INSTRUCTION GÉNÉRALE]", rendu)
        self.assertIn("Sois bref.", rendu)

    def test_l_ancien_libelle_a_disparu(self):
        """Le nom disait une durée que le champ n'a plus."""
        moteur = self._redemarrer()
        moteur.update_context(**{"instruction_générale": "Sois bref."})
        self.assertNotIn("INSTRUCTION DE SESSION",
                         moteur.build_system_context("une question assez longue"))

    def test_sans_consigne_aucun_bloc(self):
        rendu = self._redemarrer().build_system_context("une question assez longue")
        self.assertNotIn("[INSTRUCTION GÉNÉRALE]", rendu)


if __name__ == "__main__":
    unittest.main(verbosity=2)
