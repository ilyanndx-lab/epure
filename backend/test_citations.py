#!/usr/bin/env python3
"""Tests pour core.citations (second verrou contre les sources inventées).

Conventions du dépôt : script unittest autonome, `_test_env` importé avant
tout `core.*` (même si `core.citations` ne touche à aucune donnée de runtime,
la convention est uniforme sur tout `backend/`).

Écrits AVANT l'implémentation des cas code/maths (consigne de la tâche) :
c'est la principale source de faux positifs, et un faux positif détruit la
confiance dans le badge de citation plus vite qu'un faux négatif.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — convention du dépôt, avant tout core.*

from core.citations import (
    ReferenceCitations,
    construire_reference,
    extraire_rangs_cites,
    extraire_urls,
    valider_citations,
)


class FauxPositifsCodeEtMathsTest(unittest.TestCase):
    """La principale source de faux positifs : Épure sert des maths et du
    code, où `[n]` est une syntaxe d'indexation, pas une citation."""

    def setUp(self):
        self.reference = ReferenceCitations(rangs_valides=frozenset({1, 2, 3}))

    def test_indice_dans_bloc_code_fence_non_signale(self):
        reponse = "Voici le code :\n```python\narr[0] = x[1] + t[1]\n```\nRien d'autre."
        rapport = valider_citations(reponse, self.reference)
        self.assertEqual(rapport.rangs_hors_plage, [])
        self.assertTrue(rapport.aucune_citation_malgre_contexte)  # aucune vraie citation non plus

    def test_indice_inline_backticks_non_signale(self):
        reponse = "La valeur `x[1]` désigne le second élément du tableau."
        rapport = valider_citations(reponse, self.reference)
        self.assertEqual(rapport.rangs_hors_plage, [])

    def test_indice_dans_math_inline_dollar_non_signale(self):
        reponse = "On a $v[1] = 3$ pour ce vecteur, d'après le second terme."
        rapport = valider_citations(reponse, self.reference)
        self.assertEqual(rapport.rangs_hors_plage, [])

    def test_indice_dans_math_display_dollar_non_signale(self):
        reponse = "Résultat :\n$$M[1] = \\begin{pmatrix}1\\end{pmatrix}$$\nFin."
        rapport = valider_citations(reponse, self.reference)
        self.assertEqual(rapport.rangs_hors_plage, [])

    def test_indice_dans_math_display_crochet_non_signale(self):
        reponse = "Résultat :\n\\[M[1] = 3\\]\nFin."
        rapport = valider_citations(reponse, self.reference)
        self.assertEqual(rapport.rangs_hors_plage, [])

    def test_vraie_citation_a_cote_de_code_reste_detectee(self):
        """Le masquage ne doit pas non plus AVALER une vraie citation qui
        suit un bloc de code — sinon on remplace un faux positif par un faux
        négatif, pas un progrès."""
        reponse = "```python\narr[0]\n```\nD'après [1], c'est confirmé."
        rapport = valider_citations(reponse, self.reference)
        self.assertEqual(rapport.rangs_hors_plage, [])
        self.assertEqual(rapport.rangs_cites, [1])
        self.assertFalse(rapport.aucune_citation_malgre_contexte)

    def test_citation_hors_plage_a_cote_de_maths_reste_detectee(self):
        reponse = "Avec $x[1]$ on trouve la valeur, comme le dit [7]."
        rapport = valider_citations(reponse, self.reference)
        self.assertEqual(rapport.rangs_hors_plage, [7])


class RangsHorsPlageTest(unittest.TestCase):
    def test_citation_hors_plage_signalee(self):
        reference = ReferenceCitations(rangs_valides=frozenset({1, 2, 3}))
        rapport = valider_citations("Voir [7] pour les détails.", reference)
        self.assertEqual(rapport.rangs_hors_plage, [7])
        self.assertTrue(rapport.a_des_anomalies())

    def test_citation_dans_la_plage_non_signalee(self):
        reference = ReferenceCitations(rangs_valides=frozenset({1, 2, 3}))
        rapport = valider_citations("Voir [2] pour les détails.", reference)
        self.assertEqual(rapport.rangs_hors_plage, [])
        self.assertFalse(rapport.a_des_anomalies())

    def test_plusieurs_hors_plage_dedupliques_dans_l_ordre(self):
        reference = ReferenceCitations(rangs_valides=frozenset({1}))
        rapport = valider_citations("[9] puis [4], encore [9].", reference)
        self.assertEqual(rapport.rangs_hors_plage, [9, 4])


class UrlsNonReconnuesTest(unittest.TestCase):
    def test_url_inventee_signalee(self):
        reference = ReferenceCitations(urls_valides=frozenset({"https://python.org/"}))
        rapport = valider_citations("Voir https://invente.example/page pour la suite.", reference)
        self.assertEqual(rapport.urls_non_reconnues, ["https://invente.example/page"])
        self.assertTrue(rapport.a_des_anomalies())

    def test_url_reconnue_non_signalee(self):
        reference = ReferenceCitations(urls_valides=frozenset({"https://python.org/"}))
        rapport = valider_citations("Voir https://python.org/ pour la suite.", reference)
        self.assertEqual(rapport.urls_non_reconnues, [])

    def test_url_utilisateur_reprise_dans_la_reponse_non_signalee(self):
        """Une URL collée par l'utilisateur (ou tirée d'un PDF attaché) et
        reprise telle quelle n'est PAS une invention — le faux positif à
        éviter absolument."""
        reference = construire_reference(texte_utilisateur="Regarde ce lien : https://mon-cours.example/notes.pdf")
        rapport = valider_citations("D'après https://mon-cours.example/notes.pdf, ...", reference)
        self.assertEqual(rapport.urls_non_reconnues, [])

    def test_url_source_rag_non_signalee(self):
        """Une URL qui provient d'une source RAG (chunk injecté ce tour)
        n'est pas non plus une invention."""
        reference = construire_reference(urls_rag=["https://mon-cours.example/chapitre3"])
        rapport = valider_citations("Comme dans https://mon-cours.example/chapitre3, ...", reference)
        self.assertEqual(rapport.urls_non_reconnues, [])

    def test_ponctuation_finale_retiree_de_l_url_detectee(self):
        reference = ReferenceCitations(urls_valides=frozenset({"https://python.org/"}))
        rapport = valider_citations("Voir https://python.org/.", reference)
        self.assertEqual(rapport.urls_non_reconnues, [])


class SignalFaibleTest(unittest.TestCase):
    def test_contexte_web_fourni_aucune_citation_signal_faible_pas_anomalie(self):
        reference = ReferenceCitations(rangs_valides=frozenset({1, 2, 3}))
        rapport = valider_citations("Je ne sais pas répondre à cette question.", reference)
        self.assertTrue(rapport.aucune_citation_malgre_contexte)
        self.assertFalse(rapport.a_des_anomalies())  # pas un événement d'erreur
        self.assertTrue(rapport.est_vide())  # rien à AFFICHER côté client

    def test_pas_de_contexte_web_pas_de_signal_faible(self):
        reference = ReferenceCitations()  # rangs_valides vide : pas de contexte web offert
        rapport = valider_citations("Je ne sais pas répondre à cette question.", reference)
        self.assertFalse(rapport.aucune_citation_malgre_contexte)
        self.assertTrue(rapport.est_vide())

    def test_citation_hors_plage_annule_le_signal_faible(self):
        """Un [n] apparaît (même invalide) : ce n'est plus « rien cité »."""
        reference = ReferenceCitations(rangs_valides=frozenset({1, 2, 3}))
        rapport = valider_citations("D'après [9], ...", reference)
        self.assertFalse(rapport.aucune_citation_malgre_contexte)
        self.assertTrue(rapport.a_des_anomalies())


class RangsCitesTest(unittest.TestCase):
    """Sert le bloc Sources (modules/chat/router.py) : uniquement les [n]
    réellement cités, dans l'ordre d'apparition."""

    def test_ordre_d_apparition_deduplique(self):
        reference = {1, 2, 3}
        rangs = extraire_rangs_cites("D'abord [2], puis [1], encore [2].", reference)
        self.assertEqual(rangs, [2, 1])

    def test_rangs_hors_plage_absents_de_rangs_cites(self):
        reference = {1, 2, 3}
        rangs = extraire_rangs_cites("Voir [1] et [9].", reference)
        self.assertEqual(rangs, [1])

    def test_indice_de_code_absent_de_rangs_cites(self):
        reference = {1}
        rangs = extraire_rangs_cites("```python\narr[1]\n```", reference)
        self.assertEqual(rangs, [])

    def test_rapport_porte_les_memes_rangs_cites(self):
        reference = ReferenceCitations(rangs_valides=frozenset({1, 2}))
        rapport = valider_citations("[2] puis [1].", reference)
        self.assertEqual(rapport.rangs_cites, [2, 1])


class ExtraireUrlsTest(unittest.TestCase):
    def test_extrait_plusieurs_urls(self):
        urls = extraire_urls("Voir https://a.example/x et http://b.example/y.")
        self.assertEqual(urls, {"https://a.example/x", "http://b.example/y"})

    def test_aucune_url_texte_vide(self):
        self.assertEqual(extraire_urls(""), set())
        self.assertEqual(extraire_urls("Rien ici."), set())


class ConstruireReferenceTest(unittest.TestCase):
    def test_union_des_trois_provenances(self):
        reference = construire_reference(
            urls_web=["https://web.example/"],
            rangs_web=[1, 2],
            urls_rag=["https://rag.example/"],
            texte_utilisateur="Voir https://user.example/",
        )
        self.assertEqual(reference.rangs_valides, frozenset({1, 2}))
        self.assertEqual(
            reference.urls_valides,
            frozenset({"https://web.example/", "https://rag.example/", "https://user.example/"}),
        )

    def test_defauts_vides(self):
        reference = construire_reference()
        self.assertEqual(reference.rangs_valides, frozenset())
        self.assertEqual(reference.urls_valides, frozenset())


class CasVidesTest(unittest.TestCase):
    def test_reponse_vide(self):
        rapport = valider_citations("", ReferenceCitations(rangs_valides=frozenset({1})))
        self.assertTrue(rapport.est_vide())
        self.assertFalse(rapport.aucune_citation_malgre_contexte)  # pas de tour, pas de signal

    def test_reference_vide_texte_sans_citation(self):
        rapport = valider_citations("Une réponse banale, sans rien de spécial.", ReferenceCitations())
        self.assertTrue(rapport.est_vide())


if __name__ == "__main__":
    unittest.main()
