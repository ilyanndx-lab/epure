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
  b. le contrat de citation demande de dire l'absence et de ne citer que les
     numéros listés.

Décision produit qui a suivi (branche feat/arret-info-absente) : sur un tour
de lecture d'URL, une information absente des extraits n'est PAS complétée de
mémoire, même marquée « hors extrait » — le modèle le dit et propose @web. La
mesure de #86 avait montré que le contenu marqué restait peu fiable (t4 :
« la section efficace diminue quand Z augmente »). Une phrase « hors extrait »
devient donc une violation, comptée par l'étape de trace
`hors_extrait_present`.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — convention du dépôt, avant tout core.*

from core.citations import phrases_hors_extrait, retirer_renvois_de_notes, verifier_fidelite
from core.websearch import TRACE_LISTE_MAX
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
    """Contrats de citation : arrêt sur information absente, aucune
    connaissance générale ajoutée."""

    def _lecture(self):
        r = ResultatWeb(rang=1, titre="Page", url="https://exemple.org/p", extrait="extrait", moteur="lecture")
        return chat_router._construire_ctx_lecture([_lue(r)])

    def _recherche(self):
        return chat_router._construire_web_ctx("[1] Titre (exemple.org) — extrait")

    def test_lecture_dit_l_absence_et_propose_web(self):
        ctx = self._lecture()
        self.assertIn("Si l'information demandée n'est pas dans les extraits, dis-le explicitement "
                      "(« La page ne contient pas cette information. ») et propose de relancer avec @web.", ctx)

    def test_lecture_aucune_connaissance_ajoutee(self):
        self.assertIn("N'ajoute aucune valeur, aucun nom, aucune notion venant de tes connaissances.", self._lecture())

    def test_lecture_information_partielle(self):
        self.assertIn("Si l'information est partiellement présente, donne seulement la partie présente, "
                      "citée avec [n], et signale ce qui manque.", self._lecture())

    def test_recherche_meme_regle_sans_proposition_web(self):
        ctx = self._recherche()
        self.assertIn("Si l'information demandée n'est pas dans les résultats, dis-le explicitement "
                      "(« Les résultats ne contiennent pas cette information. »).", ctx)
        self.assertIn("N'ajoute aucune valeur, aucun nom, aucune notion venant de tes connaissances.", ctx)
        self.assertIn("Si l'information est partiellement présente, donne seulement la partie présente, "
                      "citée avec [n], et signale ce qui manque.", ctx)
        self.assertNotIn("@web", ctx)

    def test_plus_de_consigne_de_marquage(self):
        for nom, ctx in (("lecture", self._lecture()), ("recherche", self._recherche())):
            with self.subTest(contexte=nom):
                self.assertNotIn("hors extrait", ctx)
                self.assertNotIn("d'après mes connaissances", ctx)

    def test_seulement_les_numeros_listes(self):
        for nom, ctx in (("lecture", self._lecture()), ("recherche", self._recherche())):
            with self.subTest(contexte=nom):
                self.assertIn("Ne cite QUE les numéros [n] de la liste", ctx)

    def test_page_non_lue_inchangee(self):
        """La consigne d'échec de lecture garde sa forme propre : aucun
        extrait, donc pas de contrat de citation."""
        ctx = chat_router._construire_ctx_lecture(
            [LectureUrl(url="https://exemple.org/p", statut="échec", raison="délai dépassé", passages=0, resultat=None)])
        self.assertIn("n'a PAS pu être lue", ctx)
        self.assertNotIn("@web", ctx)


class PhrasesHorsExtraitTest(unittest.TestCase):
    """`phrases_hors_extrait` : même marque que l'exclusion de
    `verifier_fidelite` (`_HORS_EXTRAIT_RE`), sur le même découpage."""

    def test_phrases_marquees_listees(self):
        rep = ("La page ne contient pas cette information [1].\n\n"
               "Hors extrait, d'après mes connaissances : l'exposant vaut 4. Il vaut 5 ensuite.\n\n"
               "*Hors des extraits : Z⁴ à Z⁵.*")
        self.assertEqual(phrases_hors_extrait(rep),
                         ["Hors extrait, d'après mes connaissances : l'exposant vaut 4.",
                          "Hors des extraits : Z⁴ à Z⁵."])

    def test_aucune_marque(self):
        self.assertEqual(phrases_hors_extrait("La page ne contient pas cette information. "
                                              "Je peux relancer avec @web."), [])

    def test_marque_entre_guillemets_ignoree(self):
        """Citer la consigne n'est pas la violer."""
        self.assertEqual(phrases_hors_extrait("La consigne dit « hors extrait » [1]."), [])

    def test_code_ignore(self):
        self.assertEqual(phrases_hors_extrait("Voir `hors extrait` [1]."), [])

    def test_reponse_vide(self):
        self.assertEqual(phrases_hors_extrait(""), [])

    def test_mesure_de_86_t4(self):
        """Mesure « après » de #86 : t4 annonce une valeur hors extrait."""
        essais = json.loads((EXTRAITS.parent / "tirages-non-invention.json").read_text(encoding="utf-8"))["essais"]
        t4 = next(e for e in essais if e["page"] == "photoelectrique" and e["budget"] == 4000 and e["tirage"] == 4)
        self.assertEqual(len(phrases_hors_extrait(t4["reponse"])), 1)


class TraceHorsExtraitTest(unittest.TestCase):
    """Étape `hors_extrait_present` : une violation à compter, réservée en fin
    de trace comme les deux autres signalements."""

    def _resultats(self):
        return [ResultatWeb(rang=1, titre="Page", url="https://exemple.org/p",
                            extrait="La limite vaut 4.", moteur="lecture")]

    def test_etape_construite_quand_la_reponse_en_contient(self):
        _, trace = chat_router._finaliser_citations_et_trace(
            "c1", "La page ne le dit pas [1]. Hors extrait, d'après mes connaissances : 4.",
            self._resultats(), "", set(), [])
        self.assertEqual([e["etape"] for e in trace], ["hors_extrait_present"])
        self.assertEqual(trace[0]["phrases"], ["Hors extrait, d'après mes connaissances : 4."])

    def test_pas_d_etape_sans_marque(self):
        _, trace = chat_router._finaliser_citations_et_trace(
            "c1", "La limite vaut 4 [1].", self._resultats(), "", set(), [])
        self.assertEqual(trace, [])

    def test_pas_d_etape_sans_source(self):
        """Hors d'un tour de lecture ou de recherche, « hors extrait » ne veut
        rien dire : aucune consigne ne l'interdisait."""
        _, trace = chat_router._finaliser_citations_et_trace(
            "c1", "Hors extrait, d'après mes connaissances : 4.", [], "", set(), [])
        self.assertEqual(trace, [])

    def test_place_reservee_en_dernier(self):
        from core.citations import RapportCitations
        from core.websearch import TRACE_MAX_ETAPES
        beaucoup = [{"etape": "recherche_debut", "requete": str(i)} for i in range(TRACE_MAX_ETAPES + 5)]
        non_etayees = verifier_fidelite("La limite vaut 42 [1].", {1: "rien"})
        trace = chat_router._construire_trace_finale(
            beaucoup, RapportCitations(rangs_hors_plage=[7]), True, non_etayees, ["Hors extrait : 4."])
        self.assertEqual(len(trace), TRACE_MAX_ETAPES)
        self.assertEqual([e["etape"] for e in trace[-3:]],
                         ["citations_invalides", "citations_non_etayees", "hors_extrait_present"])

    def test_liste_bornee(self):
        phrases = [f"Hors extrait : {i}." for i in range(30)]
        trace = chat_router._construire_trace_finale([], None, False, [], phrases)
        self.assertEqual(len(trace[0]["phrases"]), TRACE_LISTE_MAX)

    def test_toujours_exclue_de_verifier_fidelite(self):
        self.assertEqual(verifier_fidelite("Hors extrait, d'après mes connaissances : 42 [1].", {1: "rien"}), [])


if __name__ == "__main__":
    unittest.main()
