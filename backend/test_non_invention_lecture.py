#!/usr/bin/env python3
"""Tests de #86 — consigne de non-invention en lecture d'URL.

Constat (tirages de fidélité du 2026-10-29, `tools/eval-lecture-url/resultats/
fidelite/tirages.json`) : à 4 000 caractères, sur « photoélectrique », le
modèle donne 5 fois sur 5 des valeurs d'exposant absentes de l'extrait, y
compris juste après avoir écrit qu'elles n'y étaient pas, et recopie les
renvois de notes de la page (« Millikan [4] », « les sections [1] à [10] »)
comme s'ils étaient des numéros de source. Deux corrections, verrouillées ici :

  a. les renvois de notes sont retirés des extraits AVANT injection, par la
     même fonction que celle qui les retire de la source comparée par
     `verifier_fidelite` — les deux restent alignées par construction ;
  b. le contrat de citation demande de dire l'absence, d'annoncer ce qui vient
     des connaissances générales sans lui donner de [n], et de ne citer que
     les numéros listés.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — convention du dépôt, avant tout core.*

from core.citations import retirer_renvois_de_notes, verifier_fidelite
from core.websearch import ResultatWeb, formater_pour_llm
from core.webcontent import LectureUrl
from modules.chat import router as chat_router

EXTRAITS = (Path(__file__).resolve().parents[1] / "tools" / "eval-lecture-url" / "resultats"
            / "fidelite" / "extraits.json")


def _lue(r):
    return LectureUrl(url=r.url, statut="succès", raison="", passages=1, resultat=r)


def _extrait_photoelectrique_4k():
    cas = json.loads(EXTRAITS.read_text(encoding="utf-8"))["cas"]
    return next(c for c in cas if c["page"] == "photoelectrique" and c["budget"] == 4000)


class RetirerRenvoisDeNotesTest(unittest.TestCase):

    def test_formes_isolees(self):
        for texte in ("Hertz en 1887 [ 4 ] observe", "Hertz en 1887 [4] observe",
                      "Hertz en 1887 [ N 1 ] observe", "Hertz en 1887 [réf 4] observe",
                      "Hertz en 1887 [ note 3 ] observe"):
            with self.subTest(texte=texte):
                self.assertEqual(retirer_renvois_de_notes(texte), "Hertz en 1887 observe")

    def test_suites_de_renvois_et_leurs_separateurs(self):
        for texte in ("Hertz en 1887 [ 4 ] , [ 5 ] observe", "Hertz en 1887 [4][5] observe",
                      "Hertz en 1887 [ 1 ] , [ 4 ] , [ 5 ] , [ N 1 ] observe",
                      "Hertz en 1887 [4] ; [5] observe"):
            with self.subTest(texte=texte):
                self.assertEqual(retirer_renvois_de_notes(texte), "Hertz en 1887 observe")

    def test_plages(self):
        for texte in ("Hertz en 1887 [ 4 – 6 ] observe", "Hertz en 1887 [4-6] observe",
                      "Hertz en 1887 [ 4 , 5 ] observe", "Hertz en 1887 [4]–[7] observe",
                      "Hertz en 1887 [ 4 ] à [ 7 ] observe"):
            with self.subTest(texte=texte):
                self.assertEqual(retirer_renvois_de_notes(texte), "Hertz en 1887 observe")

    def test_ponctuation_finale_conservee(self):
        self.assertEqual(retirer_renvois_de_notes("le photo-courant [ 2 ] ."), "le photo-courant .")
        self.assertEqual(retirer_renvois_de_notes("le photo-courant [2]."), "le photo-courant.")

    def test_indices_de_code_conserves(self):
        """Le cœur est générique : une page de programmation garde `arr[0]`
        et `matrice[1][2]` — un crochet collé à un identifiant n'est pas un
        renvoi de note."""
        for texte in ("on lit arr[0] puis tab[12]", "matrice[1][2] vaut 3", "f(x)[3] et d[i]"):
            with self.subTest(texte=texte):
                self.assertEqual(retirer_renvois_de_notes(texte), texte)

    def test_texte_sans_renvoi_inchange(self):
        texte = "La section efficace croît avec Z ; l'exposant varie de 4 à 5."
        self.assertEqual(retirer_renvois_de_notes(texte), texte)


class InjectionSansRenvoisTest(unittest.TestCase):
    """a. Ce que le modèle reçoit ne contient plus de renvois de notes."""

    def setUp(self):
        c = _extrait_photoelectrique_4k()
        self.extrait = c["extrait"]
        self.resultat = ResultatWeb(rang=1, titre=c["titre"], url="https://fr.wikipedia.org/wiki/Effet",
                                    extrait=self.extrait, moteur="lecture")

    def test_la_fixture_contient_des_renvois(self):
        """Garde-fou : sans renvois dans la fixture, le test suivant ne prouverait rien."""
        self.assertIn("[ 4 ] , [ 5 ]", self.extrait)

    def test_extrait_injecte_sans_renvois(self):
        ctx = chat_router._construire_ctx_lecture([_lue(self.resultat)])
        bloc = formater_pour_llm([self.resultat])
        self.assertIn(bloc, ctx)
        # Seul crochet numéroté restant : le rang [1] posé par formater_pour_llm.
        self.assertTrue(bloc.startswith("[1] "))
        self.assertEqual(retirer_renvois_de_notes(bloc[4:]), bloc[4:])
        self.assertNotIn("[ 4 ]", ctx)
        self.assertNotIn("[ 5 ]", ctx)

    def test_le_resultat_garde_son_extrait_brut(self):
        """Le retrait se fait à l'injection : le `ResultatWeb` (trace, bloc
        Sources, `verifier_fidelite`) n'est pas modifié."""
        formater_pour_llm([self.resultat])
        self.assertEqual(self.resultat.extrait, self.extrait)

    def test_verifier_fidelite_utilise_la_meme_fonction(self):
        """Une plage retirée à l'injection l'est aussi de la source comparée :
        « 4 » et « 6 » ne sont étayés que par le renvoi [4-6]."""
        res = verifier_fidelite("Il varie de 4 à 6 [1].", {1: "Hertz en 1887 [4-6] observe"})
        self.assertEqual([a.element for a in res], ["4", "6"])


class ContratDeCitationTest(unittest.TestCase):
    """b. Consigne de non-invention, dans les deux contrats de citation."""

    def _contextes(self):
        r = ResultatWeb(rang=1, titre="Page", url="https://exemple.org/p", extrait="extrait", moteur="lecture")
        return {
            "lecture": chat_router._construire_ctx_lecture([_lue(r)]),
            "recherche": chat_router._construire_web_ctx("[1] Titre (exemple.org) — extrait"),
        }

    def test_dire_l_absence(self):
        for nom, ctx in self._contextes().items():
            with self.subTest(contexte=nom):
                self.assertIn("n'est pas dans", ctx)
                self.assertIn("dis-le explicitement", ctx)

    def test_connaissances_generales_annoncees_et_sans_numero(self):
        for nom, ctx in self._contextes().items():
            with self.subTest(contexte=nom):
                self.assertIn("« hors extrait, d'après mes connaissances : … »", ctx)
                self.assertIn("ne porte JAMAIS de numéro [n]", ctx)

    def test_seulement_les_numeros_listes(self):
        for nom, ctx in self._contextes().items():
            with self.subTest(contexte=nom):
                self.assertIn("Ne cite QUE les numéros [n] de la liste", ctx)

    def test_page_non_lue_inchangee(self):
        """La consigne d'échec de lecture garde sa forme propre : aucun
        extrait, donc pas de contrat de citation."""
        ctx = chat_router._construire_ctx_lecture(
            [LectureUrl(url="https://exemple.org/p", statut="échec", raison="délai dépassé", passages=0, resultat=None)])
        self.assertIn("n'a PAS pu être lue", ctx)
        self.assertNotIn("hors extrait", ctx)


if __name__ == "__main__":
    unittest.main()
