#!/usr/bin/env python3
"""Tests pour la forme structurée de `RAGEngine.query`/`query_filtered`
(phase 3.7) : chaque chunk garde sa `source`, ce qui manquait pour peupler
correctement `urls_rag` dans `modules/chat/router.py`.

Le bug corrigé : `_do_query`/`_do_query_filtered` interrogeaient
`self._col.query(...)`, qui rend `documents` ET `metadatas` (source, mtime,
`indexé_le`), mais aplatissaient tout de suite en une seule chaîne — le
fichier d'origine de chaque chunk était perdu. `modules/chat/router.py` ne
pouvait alors peupler `urls_rag` (l'ensemble de référence de
`core.citations` pour les URLs venant du RAG) qu'avec des CHEMINS de
fichiers, jamais des URLs `http(s)` — un chemin ne matche jamais le motif de
`core.citations.extraire_urls`. `urls_rag` était donc un NO-OP : toute URL
réellement présente dans un document attaché et correctement citée par le
modèle était signalée à tort comme inventée.

Un vrai `RAGEngine`/`VectorStore` est construit ici, avec un moteur
d'embedding FACTICE (`_MoteurFactice`, sac de mots haché) : construire le
vrai `MoteurEmbedding` téléchargerait 90 Mo et n'apporterait rien à ce que ce
fichier éprouve (le filtrage/la structure des résultats, pas la qualité
sémantique du modèle réel). Même idiome que `VectorStore.__init__` le permet
explicitement (`moteur` injectable, cf. sa docstring).

Usage :
    python test_rag_sources.py
"""

import hashlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — avant tout core.*

import numpy as np  # noqa: E402

from core.citations import ReferenceCitations, construire_reference, extraire_urls, valider_citations  # noqa: E402
from core.rag import RAGEngine  # noqa: E402
from core.vector_store import VectorStore  # noqa: E402

_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


class _MoteurFactice:
    """Sac de mots haché sur 64 dimensions, normalisé — pas de sémantique
    fine, juste assez pour que deux textes partageant un mot-clé DISTINCTIF
    (pas les mots communs du français) soient les plus proches l'un de
    l'autre. Suffisant pour éprouver le FILTRAGE et la STRUCTURE des
    résultats, pas la qualité du modèle réel — hors de portée de ce fichier.
    """

    dimension = 64

    def encoder(self, textes: list[str]) -> np.ndarray:
        vecs = np.zeros((len(textes), self.dimension), dtype=np.float32)
        for i, texte in enumerate(textes):
            for mot in re.findall(r"\w+", texte.lower()):
                idx = int(hashlib.md5(mot.encode()).hexdigest(), 16) % self.dimension
                vecs[i, idx] += 1.0
            norme = np.linalg.norm(vecs[i])
            if norme > 0:
                vecs[i] /= norme
        return vecs.astype(np.float32)


class _RagReelTest(unittest.TestCase):
    """Base : un `RAGEngine` réel (VectorStore + moteur factice), deux
    fichiers indexés au contenu distinct — l'un porte une URL réelle."""

    def setUp(self):
        self._dossier_index = tempfile.mkdtemp(prefix="epure-test-rag-vecteurs-")
        self.addCleanup(self._rmtree, self._dossier_index)
        self._dossier_fichiers = tempfile.mkdtemp(prefix="epure-test-rag-fichiers-")
        self.addCleanup(self._rmtree, self._dossier_fichiers)

        store = VectorStore(self._dossier_index, moteur=_MoteurFactice())
        self.rag = RAGEngine(config_path=_CONFIG, store=store)

        self.fichier_chats = os.path.join(self._dossier_fichiers, "chats.txt")
        Path(self.fichier_chats).write_text(
            "URLCHAT Cours sur les félins. Voir https://exemple.org pour plus de détails.",
            encoding="utf-8",
        )
        self.fichier_chiens = os.path.join(self._dossier_fichiers, "chiens.txt")
        Path(self.fichier_chiens).write_text(
            "URLCHIEN Cours sur les canidés. Voir https://autre.example/page pour la suite.",
            encoding="utf-8",
        )
        self.rag.index_file(self.fichier_chats)
        self.rag.index_file(self.fichier_chiens)

    @staticmethod
    def _rmtree(p):
        import shutil
        shutil.rmtree(p, ignore_errors=True)


class QueryAvecSourcesTest(_RagReelTest):
    """`query_avec_sources`/`query_filtered_avec_sources` : chaque chunk
    porte sa source, sous la clé déjà utilisée par `get_indexed_files()`."""

    def test_chunk_porte_sa_source(self):
        chunks = self.rag.query_avec_sources("URLCHAT", n_results=1)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["source"], self.fichier_chats)
        self.assertIn("https://exemple.org", chunks[0]["texte"])

    def test_query_filtered_avec_sources_respecte_le_filtre(self):
        chunks = self.rag.query_filtered_avec_sources(
            "URLCHAT", paths=[self.fichier_chats], n_results=3,
        )
        self.assertTrue(chunks)
        self.assertTrue(all(c["source"] == self.fichier_chats for c in chunks))

    def test_query_reste_une_chaine_formatee(self):
        """`query()`/`query_filtered()` gardent leur contrat existant (str) —
        rien d'autre dans le dépôt ne doit changer de comportement."""
        texte = self.rag.query("URLCHAT", n_results=1)
        self.assertIsInstance(texte, str)
        self.assertIn("https://exemple.org", texte)

    def test_les_deux_chunks_remontent_avec_leurs_sources_respectives(self):
        chunks = self.rag.query_avec_sources("cours felins canides", n_results=2)
        sources = {c["source"] for c in chunks}
        self.assertEqual(sources, {self.fichier_chats, self.fichier_chiens})


class BugUrlsRagCorrigeTest(_RagReelTest):
    """Le bug lui-même, rejoué au niveau de la construction de l'ensemble de
    référence — exactement ce que fait désormais `modules/chat/router.py` :
    ``urls_rag = union(extraire_urls(c["texte"]) for c in chunks_struct)``.

    Ces tests auraient ÉCHOUÉ avant ce correctif : `urls_rag` y était peuplé
    avec des chemins de fichiers (ou `get_indexed_files()`), jamais reconnus
    par `extraire_urls`/le motif `https?://` de `core.citations` — l'URL
    citée aurait été signalée à tort comme inventée dans les deux cas
    ci-dessous.
    """

    def _urls_rag_comme_le_router(self, chunks: list[dict]) -> set[str]:
        urls = set()
        for c in chunks:
            urls |= extraire_urls(c["texte"])
        return urls

    def test_url_reelle_dans_un_chunk_injecte_reconnue(self):
        """RÉGRESSION DU NO-OP : une URL présente dans le chunk RAG
        effectivement injecté ce tour, et reprise telle quelle par la
        réponse, n'est plus signalée comme inventée."""
        chunks = self.rag.query_filtered_avec_sources(
            "URLCHAT", paths=[self.fichier_chats, self.fichier_chiens], n_results=1,
        )
        urls_rag = self._urls_rag_comme_le_router(chunks)
        reference = construire_reference(urls_rag=urls_rag)

        rapport = valider_citations(
            "D'après le cours, voir https://exemple.org pour plus de détails.",
            reference,
        )
        self.assertEqual(rapport.urls_non_reconnues, [])
        self.assertFalse(rapport.a_des_anomalies())

    def test_url_d_un_chunk_non_retourne_ce_tour_reste_signalee(self):
        """Resserrement dans l'autre sens : une URL d'un AUTRE document RAG
        indexé, dont aucun chunk n'a été remonté cette fois-ci, n'entre pas
        dans l'ensemble de référence — elle reste une anomalie si citée."""
        chunks = self.rag.query_filtered_avec_sources(
            "URLCHAT", paths=[self.fichier_chats, self.fichier_chiens], n_results=1,
        )
        # Seul le chunk "chats" est remonté (n_results=1, requête ciblée) :
        # le chunk "chiens" — et son URL — n'y figure pas.
        self.assertTrue(all(c["source"] == self.fichier_chats for c in chunks))

        urls_rag = self._urls_rag_comme_le_router(chunks)
        reference = construire_reference(urls_rag=urls_rag)

        rapport = valider_citations(
            "D'après le cours, voir https://autre.example/page pour la suite.",
            reference,
        )
        self.assertEqual(rapport.urls_non_reconnues, ["https://autre.example/page"])
        self.assertTrue(rapport.a_des_anomalies())

    def test_aucun_chunk_retourne_ensemble_vide(self):
        """Chunks vides (pas de fichier attaché correspondant, ou corpus
        vide) → aucune URL de référence, comportement inchangé."""
        urls_rag = self._urls_rag_comme_le_router([])
        self.assertEqual(urls_rag, set())


if __name__ == "__main__":
    unittest.main()
