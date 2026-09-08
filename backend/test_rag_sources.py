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
from core.rag import RAGEngine, est_source_virtuelle, source_encre  # noqa: E402
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


class IndexPageEncreTest(_RagReelTest):
    """`index_page_encre` : indexer une page manuscrite dans la collection `fiches`.

    Méthode DÉDIÉE plutôt qu'un `index_file` sur un chemin fabriqué (cf. sa
    docstring), et c'est ce que le premier test vérifie : la source n'est pas un
    chemin de fichier, elle est reconnaissable comme telle, et elle traverse tout
    ce que le reste du dépôt fait déjà des sources.

    Les trois autres portent sur ce que reprendre d'`index_file` était
    obligatoire, et dont l'oubli ne casse RIEN visiblement — c'est précisément
    pourquoi ils existent : une retranscription qui s'ajoute au lieu de
    remplacer, et un cache qui continue de servir l'ancien texte.
    """

    def test_la_cle_de_source_a_une_seule_ecriture(self):
        """`source_encre` est une fonction et non une f-string recopiee ailleurs.

        Le routeur du module `encre` s'en sert pour desindexer une page
        supprimee : deux facons d'ecrire la meme cle finiraient par diverger d'un
        tiret, et la page resterait indexee pour toujours sous une cle que plus
        rien ne nomme.
        """
        self.assertEqual("encre:a1b2", source_encre("a1b2"))

    def test_la_source_n_est_pas_un_chemin_et_le_dit(self):
        self.rag.index_page_encre("a1b2", r"\alpha + \beta", "Cours de méca")
        self.assertIn("encre:a1b2", self.rag.get_indexed_files())
        self.assertTrue(est_source_virtuelle("encre:a1b2"))
        # Contre-épreuve : les vrais fichiers, eux, ne sont pas virtuels — sans
        # elle, un `est_source_virtuelle` qui rendrait toujours `True` passerait.
        self.assertFalse(est_source_virtuelle(self.fichier_chats))

    def test_le_texte_transcrit_est_cherchable(self):
        """Le but de tout le lot : les notes manuscrites au milieu des fiches."""
        self.rag.index_page_encre("a1b2", "URLENCRE transcription manuscrite", "T")
        chunks = self.rag.query_avec_sources("URLENCRE", n_results=1)
        self.assertEqual("encre:a1b2", chunks[0]["source"])

    def test_le_titre_est_dans_le_document_pas_en_metadonnee(self):
        """La recherche est VECTORIELLE : seul ce qui est dans le document est
        cherchable. « Cours de méca » rangé à côté ne se retrouverait jamais."""
        self.rag.index_page_encre("a1b2", "x", "THERMOCHIMIE")
        chunks = self.rag.query_avec_sources("THERMOCHIMIE", n_results=1)
        self.assertIn("THERMOCHIMIE", chunks[0]["texte"])

    def test_retranscrire_remplace_au_lieu_d_ajouter(self):
        """Sans le `delete(where=...)` d'abord, la page apparaîtrait DEUX fois
        dans les résultats, avec deux textes différents dont l'ancien."""
        self.rag.index_page_encre("a1b2", "PREMIERE version", "T")
        self.rag.index_page_encre("a1b2", "SECONDE version", "T")
        details = {f["chemin"]: f for f in self.rag.describe_indexed_files()}
        self.assertEqual(1, details["encre:a1b2"]["chunks"])
        # Recherche FILTRÉE sur la seule source qui nous intéresse, et non une
        # requête globale : le moteur d'embedding de ce fichier est un sac de
        # mots haché (`_MoteurFactice`), donc son classement entre trois
        # documents ne prouve rien. Ce qu'on veut savoir est ce que le store
        # CONTIENT pour cette source, pas ce qu'un modèle jouet en pense.
        chunks = self.rag.query_filtered_avec_sources(
            "version", paths=["encre:a1b2"], n_results=5)
        textes = " ".join(c["texte"] for c in chunks)
        self.assertIn("SECONDE", textes)
        self.assertNotIn("PREMIERE", textes)

    def test_le_cache_de_requete_est_invalide(self):
        """LE test que l'oubli rend faux en silence : `query()` est mémoïsé
        (`_query_lru`). Sans invalidation, il continuerait de servir l'ancienne
        transcription d'une page qu'on vient de reprendre — donc de répondre sur
        un contenu que l'utilisateur croit corrigé.

        La requête est lancée AVANT la seconde indexation, pour peupler le cache :
        sans cet appel, le test passerait avec ou sans invalidation.
        """
        self.rag.index_page_encre("a1b2", "MOTAVANT", "T")
        self.assertIn("MOTAVANT", self.rag.query("MOTAVANT", n_results=1))
        self.rag.index_page_encre("a1b2", "MOTAPRES", "T")
        self.assertNotIn("MOTAVANT", self.rag.query("MOTAVANT", n_results=1))

    def test_un_texte_vide_desindexe_la_page(self):
        """Pas une erreur : le cas normal d'une transcription qui n'a rien lu.
        Laisser l'ancienne en place serait pire — la page répondrait sur un
        contenu qu'elle n'a plus."""
        self.rag.index_page_encre("a1b2", "quelque chose", "T")
        self.assertIn("encre:a1b2", self.rag.get_indexed_files())
        self.assertEqual(0, self.rag.index_page_encre("a1b2", "", ""))
        self.assertNotIn("encre:a1b2", self.rag.get_indexed_files())

    def test_le_retrait_du_corpus_marche_comme_pour_un_fichier(self):
        """`remove_source` n'a rien à savoir de la nature de la source — c'est ce
        qui rend le bouton « retirer » du panneau fichiers utilisable ici."""
        self.rag.index_page_encre("a1b2", "quelque chose", "T")
        self.assertEqual(1, self.rag.remove_source("encre:a1b2"))
        self.assertNotIn("encre:a1b2", self.rag.get_indexed_files())

    def test_le_decoupage_est_celui_des_fichiers(self):
        """Le MÊME découpage qu'`index_file` sur le MÊME texte, pas seulement
        « un découpage ».

        Comparé au chemin fichier et non à `_decouper` lui-même : `assertEqual(
        len(self.rag._decouper(t)), chunks)` serait vrai de n'importe quel
        découpage, y compris cassé — il ne dirait que « `index_page_encre`
        appelle `_decouper` ». Ce qu'on veut savoir est autre chose : deux
        découpages pour une même collection dériveraient, et un chunk d'encre
        deux fois plus long qu'un chunk de fiche fausserait la comparaison de
        similarité sur laquelle repose toute la recherche.

        Le texte est identique des deux côtés à l'en-tête près — d'où le titre
        vide passé à `index_page_encre`, qui n'ajoute alors rien devant.
        """
        long_texte = "abcde " * 2000
        fichier = os.path.join(self._dossier_fichiers, "long.txt")
        Path(fichier).write_text(long_texte, encoding="utf-8")
        self.rag.index_file(fichier)
        self.rag.index_page_encre("a1b2", long_texte, "")
        details = {f["chemin"]: f for f in self.rag.describe_indexed_files()}
        # Plusieurs chunks des deux côtés, sinon l'égalité serait vraie par
        # trivialité (1 == 1) et ne dirait rien du pas de recouvrement.
        self.assertGreater(details[fichier]["chunks"], 1)
        self.assertEqual(details[fichier]["chunks"], details["encre:a1b2"]["chunks"])

    def test_le_mtime_dit_qu_il_n_y_a_pas_de_fichier_derriere(self):
        """0.0 et non omis : `_indexed_mtimes` construit sa table sur la PRÉSENCE
        de la clé. Une source sans `mtime` en serait absente — correct
        aujourd'hui, et correct par accident. Aucun `os.path.getmtime` ne rend
        jamais cette valeur, donc le scan incrémental ne peut pas la confondre
        avec un fichier inchangé.
        """
        self.rag.index_page_encre("a1b2", "x", "")
        details = {f["chemin"]: f for f in self.rag.describe_indexed_files()}
        self.assertEqual(0.0, details["encre:a1b2"]["mtime"])
        self.assertTrue(details["encre:a1b2"]["indexé_le"])


if __name__ == "__main__":
    unittest.main()
