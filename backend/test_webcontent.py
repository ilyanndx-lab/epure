#!/usr/bin/env python3
"""Tests pour core.webcontent (phase 4 : contenu réel des pages @web).

Isole `recuperer_contenu` de tout accès réseau réel et de tout chargement
d'embedding réel — les deux dépendances externes du module sont remplacées
aux points d'entrée qu'il expose lui-même à cet effet :

  - `_recuperer_page` : fetch HTML d'une page (réseau)
  - `_extraire_texte_principal` : extraction readability (peut être bypassée
    directement pour contrôler le texte sans fabriquer du HTML réaliste)
  - `_vecteurs_partages` : accès au store vectoriel partagé (core.runtime)

Usage :
    python test_webcontent.py
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — avant tout import de core.*

import numpy as np  # noqa: E402

from core import webcontent  # noqa: E402
from core.embedding_install import EmbeddingIndisponible  # noqa: E402
from core.websearch import ResultatWeb  # noqa: E402


def _resultat(rang: int, url: str, extrait: str = "extrait ddg d'origine") -> ResultatWeb:
    return ResultatWeb(rang=rang, titre=f"Titre {rang}", url=url, extrait=extrait, moteur="ddg-html")


class _FakeVectorStore:
    """Double de `core.vector_store.VectorStore` : cosinus contrôlé par un
    simple marqueur textuel plutôt qu'un vrai modèle — suffisant pour éprouver
    le RECLASSEMENT (quel chunk gagne), pas la qualité sémantique réelle."""

    def __init__(self, motif_pertinent: str = "PERTINENT"):
        self.motif = motif_pertinent
        self.appels: list[list[str]] = []

    def embed_texts(self, textes: list[str]) -> np.ndarray:
        self.appels.append(list(textes))
        return np.array(
            [[1.0, 0.0] if self.motif in t else [0.0, 1.0] for t in textes],
            dtype=np.float32,
        )


class _VectorStoreIndisponible:
    def embed_texts(self, textes: list[str]) -> np.ndarray:
        raise EmbeddingIndisponible({"état": "absent", "detail": "modèle non téléchargé"})


class FetchReussiTest(unittest.TestCase):
    """Une page qui répond → son extrait DDG est remplacé par du contenu réel,
    reclassé par similarité avec la requête."""

    def test_extrait_remplace_par_le_passage_pertinent(self):
        r = _resultat(1, "https://exemple.org/article")
        contenu = "PERTINENT " * webcontent._MOTS_PAR_CHUNK + "AUTRE " * webcontent._MOTS_PAR_CHUNK

        etapes: list[dict] = []
        with mock.patch.object(webcontent, "_recuperer_page", return_value="<html>peu importe</html>"), \
             mock.patch.object(webcontent, "_extraire_texte_principal", return_value=contenu), \
             mock.patch.object(webcontent, "_vecteurs_partages", return_value=_FakeVectorStore()):
            resultats = webcontent.recuperer_contenu([r], "PERTINENT", on_etape=etapes.append)

        self.assertEqual(len(resultats), 1)
        self.assertIn("PERTINENT", resultats[0].extrait)
        self.assertNotEqual(resultats[0].extrait, r.extrait)
        # Budget respecté (marge pour le marqueur de troncature « … »).
        self.assertLessEqual(len(resultats[0].extrait), webcontent._BUDGET_CARACTERES_PAR_PAGE + 1)
        # Champs non modifiés à part l'extrait.
        self.assertEqual(resultats[0].rang, r.rang)
        self.assertEqual(resultats[0].titre, r.titre)
        self.assertEqual(resultats[0].url, r.url)

        pages = [e for e in etapes if e["etape"] == "page_recuperee"]
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["url"], r.url)
        self.assertEqual(pages[0]["statut"], "succès")
        self.assertGreater(pages[0]["passages_retenus"], 0)

    def test_ordre_des_resultats_preserve(self):
        """Le rang d'origine décide de l'ordre du retour, pas l'ordre d'arrivée
        des fetchs (parallèles, donc non déterministe)."""
        resultats_in = [_resultat(1, "https://a.example/"), _resultat(2, "https://b.example/")]
        with mock.patch.object(webcontent, "_recuperer_page", return_value=None):
            resultats = webcontent.recuperer_contenu(resultats_in, "requête", on_etape=None)
        self.assertEqual([r.rang for r in resultats], [1, 2])


class FetchEchoueTest(unittest.TestCase):
    """Dégradation PAR PAGE : l'échec d'une page ne touche ni les autres ni la
    recherche entière."""

    def test_page_en_echec_garde_son_extrait_ddg_les_autres_non_affectees(self):
        r_ok = _resultat(1, "https://ok.example/", extrait="ddg ok")
        r_ko = _resultat(2, "https://ko.example/", extrait="ddg ko")

        def _fetch(url: str):
            return None if url == r_ko.url else "<html>ok</html>"

        contenu = "PERTINENT " * webcontent._MOTS_PAR_CHUNK
        etapes: list[dict] = []
        with mock.patch.object(webcontent, "_recuperer_page", side_effect=_fetch), \
             mock.patch.object(webcontent, "_extraire_texte_principal", return_value=contenu), \
             mock.patch.object(webcontent, "_vecteurs_partages", return_value=_FakeVectorStore()):
            resultats = webcontent.recuperer_contenu([r_ok, r_ko], "PERTINENT", on_etape=etapes.append)

        par_rang = {r.rang: r for r in resultats}
        self.assertEqual(par_rang[2].extrait, "ddg ko", "la page en échec garde son extrait DDG d'origine")
        self.assertNotEqual(par_rang[1].extrait, "ddg ok", "la page réussie est bien enrichie")

        statuts = {e["url"]: e["statut"] for e in etapes if e["etape"] == "page_recuperee"}
        self.assertEqual(statuts[r_ok.url], "succès")
        self.assertEqual(statuts[r_ko.url], "échec")

    def test_texte_extrait_vide_traite_comme_un_echec(self):
        """Une page qui répond mais dont readability ne tire rien (page sans
        article, mur de JS) doit dégrader comme un échec réseau."""
        r = _resultat(1, "https://vide.example/", extrait="ddg d'origine")
        with mock.patch.object(webcontent, "_recuperer_page", return_value="<html></html>"), \
             mock.patch.object(webcontent, "_extraire_texte_principal", return_value=""):
            resultats = webcontent.recuperer_contenu([r], "requête", on_etape=None)
        self.assertEqual(resultats[0].extrait, "ddg d'origine")


class EmbeddingIndisponibleTest(unittest.TestCase):
    """Dégradation GLOBALE : sans embedding, toute l'étape retombe sur les
    extraits DDG — la recherche web ne dépend jamais du RAG."""

    def test_tous_les_extraits_ddg_conserves_sans_exception(self):
        resultats_in = [
            _resultat(1, "https://a.example/", extrait="ddg a"),
            _resultat(2, "https://b.example/", extrait="ddg b"),
        ]
        contenu = "du texte bien réel extrait de la page " * 30
        etapes: list[dict] = []
        with mock.patch.object(webcontent, "_recuperer_page", return_value="<html>ok</html>"), \
             mock.patch.object(webcontent, "_extraire_texte_principal", return_value=contenu), \
             mock.patch.object(webcontent, "_vecteurs_partages", return_value=_VectorStoreIndisponible()):
            resultats = webcontent.recuperer_contenu(resultats_in, "requête", on_etape=etapes.append)

        self.assertEqual([r.extrait for r in resultats], ["ddg a", "ddg b"])
        self.assertTrue(any(e["etape"] == "reclassement_indisponible" for e in etapes))

    def test_resultats_vides_ne_font_rien(self):
        self.assertEqual(webcontent.recuperer_contenu([], "requête"), [])


class BudgetDeTempsTest(unittest.TestCase):
    """Même style que `test_models_lmstudio.test_une_sonde_lmstudio_qui_bloque_est_bornee_a_2s` :
    plusieurs pages lentes EN PARALLÈLE ne doivent pas faire dépasser le
    plafond global, pas la somme des plafonds individuels."""

    def test_plusieurs_pages_lentes_en_parallele_respectent_le_budget_global(self):
        def _fetch_lent(url: str):
            time.sleep(8.0)  # bien au-delà de _TIMEOUT_ETAPE_S (6 s)
            return None

        resultats_in = [_resultat(i, f"https://lente{i}.example/") for i in range(1, 6)]
        debut = time.perf_counter()
        with mock.patch.object(webcontent, "_recuperer_page", side_effect=_fetch_lent):
            resultats = webcontent.recuperer_contenu(resultats_in, "requête", on_etape=None)
        ecoule = time.perf_counter() - debut

        self.assertLess(
            ecoule, webcontent._TIMEOUT_ETAPE_S + 2.0,
            "le budget global n'est pas respecté — les plafonds individuels "
            "se sont additionnés au lieu de tourner en parallèle",
        )
        # Dégradation propre : toutes les pages gardent leur extrait DDG.
        self.assertEqual([r.extrait for r in resultats], [r.extrait for r in resultats_in])


class TraceParPageTest(unittest.TestCase):
    def test_une_etape_par_page_cohérente_avec_le_format_existant(self):
        resultats_in = [_resultat(i, f"https://site{i}.example/") for i in (1, 2, 3)]
        etapes: list[dict] = []
        with mock.patch.object(webcontent, "_recuperer_page", return_value=None):
            webcontent.recuperer_contenu(resultats_in, "requête", on_etape=etapes.append)

        pages = [e for e in etapes if e["etape"] == "page_recuperee"]
        self.assertEqual(len(pages), 3)
        for e in pages:
            self.assertIn("url", e)
            self.assertIn("statut", e)
            self.assertIn("ms", e)
            self.assertIn("passages_retenus", e)

    def test_callback_defaillant_ne_casse_pas_l_appel(self):
        """Même garde-fou que `core.websearch._emettre` : un `on_etape` qui
        lève ne doit jamais faire échouer `recuperer_contenu`."""
        def _casse(_e):
            raise RuntimeError("callback cassé")

        with mock.patch.object(webcontent, "_recuperer_page", return_value=None):
            resultats = webcontent.recuperer_contenu([_resultat(1, "https://x.example/")], "q", on_etape=_casse)
        self.assertEqual(len(resultats), 1)


class LimitePagesEnrichiesTest(unittest.TestCase):
    def test_au_dela_de_max_pages_enrichies_les_suivantes_gardent_leur_ddg(self):
        n = webcontent._MAX_PAGES_ENRICHIES + 2
        resultats_in = [_resultat(i, f"https://p{i}.example/", extrait=f"ddg{i}") for i in range(1, n + 1)]
        etapes: list[dict] = []
        with mock.patch.object(webcontent, "_recuperer_page", return_value=None):
            resultats = webcontent.recuperer_contenu(resultats_in, "requête", on_etape=etapes.append)

        pages = [e for e in etapes if e["etape"] == "page_recuperee"]
        self.assertEqual(len(pages), webcontent._MAX_PAGES_ENRICHIES)
        # Toutes gardent leur extrait DDG (fetch renvoie None), y compris les
        # rangs au-delà de la limite, qui n'ont même pas été tentés.
        self.assertEqual([r.extrait for r in resultats], [f"ddg{i}" for i in range(1, n + 1)])


if __name__ == "__main__":
    unittest.main()
