#!/usr/bin/env python3
"""Budget de la lecture directe d'URL, proportionnel au contexte CHARGÉ.

Ce que la lecture d'URL faisait avant ce chantier : trois passages de ~200
mots au plus, remis dans l'ordre de la page, puis tronqués à 800 caractères
(le budget de la phase 4, pensé pour cinq extraits @web à la fois). Une page
longue dont la réponse se trouve vers la fin perdait donc la réponse à la
coupe, même quand le reclassement l'avait trouvée : la limite était
documentée telle quelle dans `test_lecture_url.test_reclassement_par_la_question`.

Ce fichier verrouille la règle qui la remplace :

  * budget total = 25 % du contexte chargé × 3,2 caractères par token,
    plafonné à 16 000, avec un plancher de 4 000 quand le contexte est
    inconnu ; partagé également entre les pages lues ;
  * sélection par similarité avec le message sans ses URLs, sur des passages
    ENTIERS (jamais coupés au milieu), remis dans l'ordre de la page ;
  * repli sur l'ordre de la page, dit dans la trace (`selection`), quand il
    n'y a pas de question ou pas d'embedding ;
  * la phase 4 (@web) garde ses 800 caractères par page.

Même isolation réseau que `test_lecture_url.py` (dont les doubles sont
repris, pas recopiés).

Usage :
    python test_budget_lecture_url.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — avant tout import de core.*

from core import webcontent  # noqa: E402
from test_lecture_url import (  # noqa: E402
    _article, _dns_public, _FakeVectorStore, _FausseReponse, _VectorStoreIndisponible,
)


def _page_longue(pertinent_a=None, nb_paragraphes=60):
    html = _article(nb_paragraphes=nb_paragraphes, mot="banal")
    if pertinent_a is not None:
        html = html.replace(f"Paragraphe {pertinent_a} de l'article",
                            f"Paragraphe {pertinent_a} PERTINENT de l'article")
    return html


def _chunks_de(html):
    return webcontent._decouper_en_chunks(webcontent._extraire_texte_principal(html))


class _Base(unittest.TestCase):
    def setUp(self):
        for cible, attribut, valeur in (
            (webcontent.socket, "getaddrinfo", _dns_public),
            (webcontent, "_vecteurs_partages", lambda: _FakeVectorStore()),
        ):
            patcher = mock.patch.object(cible, attribut, valeur)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _lire(self, pages, question, contexte_charge=None, source_contexte=None):
        """`pages` : {url: html}. Rend (lectures, étapes `lecture_url`)."""
        def ouvrir(req, timeout):
            return _FausseReponse(pages[req.full_url], url_finale=req.full_url)

        etapes = []
        with mock.patch.object(webcontent, "_ouvrir", side_effect=ouvrir):
            lectures = webcontent.lire_urls(
                list(pages), question, on_etape=etapes.append,
                contexte_charge=contexte_charge, source_contexte=source_contexte,
            )
        return lectures, [e for e in etapes if e["etape"] == "lecture_url"]


class BudgetTest(unittest.TestCase):
    """La formule seule, sans page."""

    def test_vingt_cinq_pour_cent_du_contexte_a_trois_caracteres_deux(self):
        # 8 192 × 0,25 × 3,2 = 6 553,6 → 6 553 (ratio mesuré, cf. webcontent)
        self.assertEqual(webcontent.budget_lecture(8192), 6553)
        self.assertEqual(webcontent.budget_lecture(4096), 3276)

    def test_plafond(self):
        self.assertEqual(webcontent.budget_lecture(262144), 16000)

    def test_plafond_atteint_a_vingt_mille_tokens(self):
        """16 000 / (0,25 × 3,2) = 20 000 : en dessous, le plafond n'est pas
        atteint — à 8 192 tokens chargés, le budget reste à 6 553."""
        self.assertEqual(webcontent.budget_lecture(20000), 16000)
        self.assertEqual(webcontent.budget_lecture(19999), 15999)

    def test_plancher_quand_le_contexte_est_inconnu(self):
        self.assertEqual(webcontent.budget_lecture(None), 4000)

    def test_phase_4_inchangee(self):
        """Les résultats @web gardent leur budget : 5 extraits × 800 caractères.
        Ce chantier ne touche que la lecture d'une URL donnée explicitement."""
        self.assertEqual(webcontent._BUDGET_CARACTERES_PAR_PAGE, 800)


class FinDePageTest(_Base):
    def test_info_en_fin_de_page_longue_retenue(self):
        """Le cas qui a motivé le chantier : la réponse au paragraphe 57 sur 60.
        Avant, le passage était bien classé premier, puis perdu à la coupe des
        800 caractères parce que remis après deux passages du début."""
        html = _page_longue(pertinent_a=57)
        lectures, etapes = self._lire({"https://site.example/long": html},
                                      "Que dit le passage PERTINENT ?", contexte_charge=8192)
        extrait = lectures[0].resultat.extrait
        self.assertIn("PERTINENT", extrait)
        self.assertLessEqual(len(extrait), 6553)
        # Le budget sert : plus que les trois passages de la phase 4.
        self.assertGreater(lectures[0].passages, 3)
        self.assertEqual(etapes[0]["selection"], "similarite")

    def test_passages_entiers_et_dans_l_ordre_de_la_page(self):
        html = _page_longue(pertinent_a=57)
        lectures, _ = self._lire({"https://site.example/long": html},
                                 "PERTINENT ?", contexte_charge=8192)
        chunks = _chunks_de(html)
        position = {c: i for i, c in enumerate(chunks)}
        morceaux = lectures[0].resultat.extrait.split(" […] ")
        indices = []
        for morceau in morceaux:
            # Chaque morceau est une suite de passages ENTIERS consécutifs :
            # aucune coupe au milieu d'un passage, ni « … » de troncature.
            self.assertFalse(morceau.endswith("…"))
            debut = [c for c in chunks if morceau.startswith(c)]
            fin = [c for c in chunks if morceau.endswith(c)]
            self.assertTrue(debut and fin, f"morceau coupé : {morceau[:60]}…{morceau[-60:]}")
            indices.append(position[debut[0]])
        self.assertEqual(indices, sorted(indices))


class OrdreDePageTest(_Base):
    def test_message_vide_ordre_de_la_page(self):
        html = _page_longue()
        lectures, etapes = self._lire({"https://site.example/long": html}, "",
                                      contexte_charge=8192)
        extrait = lectures[0].resultat.extrait
        # Début de page : le premier passage, titre <h1> compris.
        self.assertTrue(extrait.startswith(_chunks_de(html)[0]))
        self.assertGreater(len(extrait), 800)
        self.assertLessEqual(len(extrait), 6553)
        self.assertEqual(etapes[0]["selection"], "ordre_page")

    def test_embedding_indisponible_ordre_de_la_page(self):
        html = _page_longue(pertinent_a=57)
        with mock.patch.object(webcontent, "_vecteurs_partages",
                               lambda: _VectorStoreIndisponible()):
            lectures, etapes = self._lire({"https://site.example/long": html},
                                          "PERTINENT ?", contexte_charge=8192)
        extrait = lectures[0].resultat.extrait
        # Début de page : le premier passage, titre <h1> compris.
        self.assertTrue(extrait.startswith(_chunks_de(html)[0]))
        self.assertNotIn("PERTINENT", extrait)
        self.assertEqual(etapes[0]["selection"], "ordre_page")


class PartageEtTraceTest(_Base):
    def test_budget_partage_entre_deux_urls(self):
        pages = {
            "https://a.example/long": _page_longue(),
            "https://b.example/long": _page_longue(),
        }
        lectures, etapes = self._lire(pages, "question", contexte_charge=8192)
        self.assertEqual([lec.statut for lec in lectures], ["succès", "succès"])
        for lec in lectures:
            self.assertLessEqual(len(lec.resultat.extrait), 6553 // 2)
            self.assertGreater(len(lec.resultat.extrait), 800)
        for etape in etapes:
            self.assertEqual(etape["budget_caracteres"], 6553)
            self.assertEqual(etape["budget_page"], 3276)

    def test_trace_contexte_et_source(self):
        lectures, etapes = self._lire({"https://site.example/long": _page_longue()},
                                      "question", contexte_charge=8192,
                                      source_contexte="lmstudio /api/v1/models")
        self.assertEqual(etapes[0]["contexte_charge"], 8192)
        self.assertEqual(etapes[0]["budget_caracteres"], 6553)
        self.assertEqual(etapes[0]["source_contexte"], "lmstudio /api/v1/models")
        self.assertEqual(etapes[0]["caracteres"], len(lectures[0].resultat.extrait))

    def test_plancher_quand_le_contexte_est_inconnu(self):
        lectures, etapes = self._lire({"https://site.example/long": _page_longue()},
                                      "question")
        extrait = lectures[0].resultat.extrait
        self.assertLessEqual(len(extrait), 4000)
        self.assertGreater(len(extrait), 2500)
        self.assertIsNone(etapes[0]["contexte_charge"])
        self.assertIsNone(etapes[0]["source_contexte"])
        self.assertEqual(etapes[0]["budget_caracteres"], 4000)

    def test_budget_plus_petit_qu_un_passage_coupe_sur_un_mot(self):
        """Contexte minuscule (1 024 tokens → 819 caractères pour deux pages,
        409 chacune) : aucun passage entier ne tient. Plutôt que de rendre la
        page « sans passage exploitable », le meilleur passage est coupé sur
        une frontière de MOT, marqué « … »."""
        pages = {
            "https://a.example/long": _page_longue(pertinent_a=57),
            "https://b.example/long": _page_longue(),
        }
        lectures, _ = self._lire(pages, "PERTINENT ?", contexte_charge=1024)
        extrait = lectures[0].resultat.extrait
        self.assertEqual(lectures[0].statut, "succès")
        self.assertLessEqual(len(extrait), 409)
        self.assertTrue(extrait.endswith("…"))
        meilleur = next(c for c in _chunks_de(pages["https://a.example/long"]) if "PERTINENT" in c)
        coupe = extrait[:-1]
        self.assertTrue(meilleur.startswith(coupe))
        # Frontière de mot : la coupe tombe juste avant une espace du passage.
        self.assertEqual(meilleur[len(coupe)], " ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
