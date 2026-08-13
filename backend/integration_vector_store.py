#!/usr/bin/env python3
"""Non-régression `core/vector_store.py` contre le vrai `chromadb`, et vérification
qu'un accès concurrent au nouveau store est sérialisé sans corruption.

`docs/remplacement-vectoriel.md`, étape B. Deux choses distinctes, à ne pas confondre :

1. **Non-régression** : les mêmes documents, insérés de la même façon, interrogés par
   les mêmes requêtes, doivent donner les mêmes résultats (mêmes ids, dans le même
   ordre) sur `chromadb.PersistentClient` et sur `VectorStore`. Couvre les TROIS profils
   d'appel mesurés en §0.3 du plan (`fiches`, `doc_analysis`, `history`), pas seulement
   celui de `core/rag.py` — chacun exerce une forme de `where`/`include`/`delete`
   différente (égalité, `$in`, AND multi-clés, delete par `ids`).

2. **Concurrence** : le §1/étape A du plan assume un verrou (`threading.RLock` sur
   `VectorStore`) en échange d'une régression connue (plus d'accès concurrent entre
   collections, contrairement à chromadb). Ce test ne cherche PAS à améliorer ça — il
   vérifie que le verrou fait au moins ce pour quoi il existe : empêcher une corruption
   de données sous écriture concurrente, pas seulement éviter une exception. Deux
   threads qui `upsert`/`query` en boucle sur la même collection, puis un contrôle
   d'intégrité déterministe sur l'état final (pas un « ça n'a pas planté » qui ne prouve
   rien : le module au-dessus, `core/rag.py`, retombe déjà sur ce piège si l'état est
   silencieusement incohérent).

Nommé `integration_` et non `test_` volontairement, comme `integration_modules_mount.py` :
charge un vrai modèle `sentence-transformers` (torch) ET un vrai `chromadb.PersistentClient`
pour la comparaison — lent, et hors de ce que `unittest discover -p 'test_*.py'` doit
tirer à chaque run de CI. Lancé par le job `integration` (manuel, workflow_dispatch).

Usage :
    python integration_vector_store.py
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chromadb  # noqa: E402
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction  # noqa: E402

from core.vector_store import VectorStore  # noqa: E402

_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Phrases choisies pour avoir un vainqueur de similarité sans ambiguïté : deux sujets
# nettement distincts (félins vs. météo), un intrus qui recoupe un peu les deux.
_DOCS = {
    "chat1": "Le chat noir dort sur le canapé du salon toute la journée.",
    "chat2": "Un chaton joue avec une pelote de laine près de la fenêtre.",
    "meteo1": "Il pleut fort ce matin, le ciel est complètement gris.",
    "meteo2": "Un orage violent est annoncé pour la fin de l'après-midi.",
    "mixte": "Le chat regarde la pluie tomber derrière la vitre.",
}
_QUERY_CHAT = "un félin qui se repose"
_QUERY_METEO = "des nuages et de la pluie annoncés"


class _Profil(unittest.TestCase):
    """Base : construit une paire (collection chromadb, collection VectorStore)
    peuplée IDENTIQUEMENT, pour comparer les deux sur la suite d'opérations propre à
    un appelant réel (`fiches`, `doc_analysis` ou `history`).
    """

    def setUp(self):
        self._tmp_chroma = tempfile.mkdtemp(prefix="epure-nonreg-chroma-")
        self._tmp_store = tempfile.mkdtemp(prefix="epure-nonreg-store-")
        ef = SentenceTransformerEmbeddingFunction(model_name=_MODEL)
        client = chromadb.PersistentClient(path=self._tmp_chroma)
        self.chroma_col = client.get_or_create_collection("test_col", embedding_function=ef)
        self.store = VectorStore(self._tmp_store, embedding_model=_MODEL)
        self.store_col = self.store.collection("test_col")

    def tearDown(self):
        shutil.rmtree(self._tmp_chroma, ignore_errors=True)
        shutil.rmtree(self._tmp_store, ignore_errors=True)

    def _upsert_both(self, ids, documents, metadatas):
        self.chroma_col.upsert(ids=ids, documents=documents, metadatas=metadatas)
        self.store_col.upsert(ids=ids, documents=documents, metadatas=metadatas)


class FichesTest(_Profil):
    """Profil `core/rag.py` : delete par `where` égalité, get `$in` (`include=[]`),
    get sans filtre (`include=["metadatas"]`), query avec et sans `where`.
    """

    def setUp(self):
        super().setUp()
        ids = list(_DOCS.keys())
        docs = list(_DOCS.values())
        metadatas = [
            {"source": f"/fiches/{k}.pdf", "chunk": 0, "mtime": 1000.0 + i}
            for i, k in enumerate(ids)
        ]
        self._ids, self._metadatas = ids, metadatas
        self._upsert_both(ids, docs, metadatas)

    def test_count_identique(self):
        self.assertEqual(self.chroma_col.count(), self.store_col.count())

    def test_query_meme_ordre_meme_top1(self):
        for texte in (_QUERY_CHAT, _QUERY_METEO):
            with self.subTest(requete=texte):
                rc = self.chroma_col.query(query_texts=[texte], n_results=3)
                rs = self.store_col.query(query_texts=[texte], n_results=3)
                self.assertEqual(rc["ids"][0], rs["ids"][0],
                                  f"ordre différent pour « {texte} »")

    def test_query_avec_where_in(self):
        sujets = [f"/fiches/{k}.pdf" for k in ("chat1", "chat2", "mixte")]
        where = {"source": {"$in": sujets}}
        rc = self.chroma_col.query(query_texts=[_QUERY_CHAT], n_results=5, where=where)
        rs = self.store_col.query(query_texts=[_QUERY_CHAT], n_results=5, where=where)
        self.assertEqual(set(rc["ids"][0]), set(rs["ids"][0]))
        self.assertEqual(rc["ids"][0], rs["ids"][0])

    def test_get_in_sans_relecture(self):
        """`include=[]` : chromadb renvoie quand même la clé `"documents"` (valeur
        `None`) plutôt que de l'omettre — mais aucun appelant réel ne fait `in`/indexation
        directe sur ces dicts, seulement `.get(clé, défaut)` (vérifié en §0.3 : les
        trois fichiers n'écrivent jamais que `results.get("documents", ...)`). Le test
        compare donc ce que les appelants lisent réellement, pas la forme interne exacte
        du dict — `VectorStore` omet la clé, chromadb la met à `None`, les deux sont
        équivalents pour tout `.get()`.
        """
        sujets = [f"/fiches/{k}.pdf" for k in ("meteo1", "meteo2")]
        where = {"source": {"$in": sujets}}
        gc = self.chroma_col.get(where=where, include=[])
        gs = self.store_col.get(where=where, include=[])
        self.assertEqual(set(gc["ids"]), set(gs["ids"]))
        self.assertFalse(gc.get("documents"))
        self.assertFalse(gs.get("documents"))

    def test_get_sans_filtre_renvoie_tout(self):
        gc = self.chroma_col.get(include=["metadatas"])
        gs = self.store_col.get(include=["metadatas"])
        self.assertEqual(set(gc["ids"]), set(self._ids))
        self.assertEqual(set(gs["ids"]), set(self._ids))
        self.assertEqual(set(gc["ids"]), set(gs["ids"]))

    def test_delete_where_egalite(self):
        self.chroma_col.delete(where={"source": "/fiches/chat1.pdf"})
        self.store_col.delete(where={"source": "/fiches/chat1.pdf"})
        self.assertEqual(self.chroma_col.count(), self.store_col.count())
        self.assertNotIn("chat1", self.chroma_col.get()["ids"])
        self.assertNotIn("chat1", self.store_col.get()["ids"])


class DocAnalysisTest(_Profil):
    """Profil `core/docanalysis.py` : AND multi-clés dans `where`, `distances`
    demandées explicitement, delete par `doc_id` ET par `source` séparément.
    """

    def setUp(self):
        super().setUp()
        ids = [f"docA::{i}" for i in range(3)] + [f"docB::{i}" for i in range(2)]
        docs = [_DOCS["chat1"], _DOCS["chat2"], _DOCS["mixte"], _DOCS["meteo1"], _DOCS["meteo2"]]
        metadatas = (
            [{"doc_id": "docA", "source": "/pdfs/a.pdf", "chunk_index": i,
              "n_pages": 1, "n_chunks": 3} for i in range(3)]
            + [{"doc_id": "docB", "source": "/pdfs/b.pdf", "chunk_index": i,
                "n_pages": 1, "n_chunks": 2} for i in range(2)]
        )
        self._upsert_both(ids, docs, metadatas)

    def test_where_a_deux_cles_leve_dans_les_deux_a_l_identique(self):
        """`core/docanalysis.py::load_document_streaming` appelle bien
        `get(where={"doc_id": doc_id, "chunk_index": 0})` — un AND à deux clés que le
        VRAI chromadb (1.5.9) rejette (`validate_where` : `len(where) != 1`). L'appel
        est déjà entouré d'un `except Exception: pass` dans le code existant : c'est un
        bug préexistant, indépendant de cette migration, qui laisse `apercu` vide
        aujourd'hui. Le non-régression ici n'est PAS « ça doit marcher » — c'est que le
        nouveau store échoue de la MÊME façon que l'ancien, pour que ce bug reste
        exactement aussi présent (ou absent) après la migration qu'avant elle.
        """
        where = {"doc_id": "docA", "chunk_index": 0}
        with self.assertRaises(ValueError):
            self.chroma_col.get(where=where, include=["documents"])
        with self.assertRaises(ValueError):
            self.store_col.get(where=where, include=["documents"])

    def test_query_avec_distances_et_where(self):
        rc = self.chroma_col.query(
            query_texts=[_QUERY_CHAT], n_results=3, where={"doc_id": "docA"},
            include=["documents", "metadatas", "distances"],
        )
        rs = self.store_col.query(
            query_texts=[_QUERY_CHAT], n_results=3, where={"doc_id": "docA"},
            include=["documents", "metadatas", "distances"],
        )
        self.assertEqual(rc["ids"][0], rs["ids"][0])
        self.assertEqual(len(rc["ids"][0]), 3, "docB ne doit pas apparaître (where doc_id=docA)")
        for dc, ds in zip(rc["distances"][0], rs["distances"][0]):
            self.assertAlmostEqual(dc, ds, places=3)
        # Le score que core/docanalysis.py affiche (1.0 - dist) doit rester cohérent
        # entre les deux stockages, puisque c'est exactement cette formule qui est
        # affichée à l'utilisateur.
        for dc, ds in zip(rc["distances"][0], rs["distances"][0]):
            self.assertAlmostEqual(1.0 - dc, 1.0 - ds, places=3)

    def test_delete_par_doc_id_puis_par_source(self):
        self.chroma_col.delete(where={"doc_id": "docB"})
        self.store_col.delete(where={"doc_id": "docB"})
        self.assertEqual(self.chroma_col.count(), self.store_col.count())

        self.chroma_col.delete(where={"source": "/pdfs/a.pdf"})
        self.store_col.delete(where={"source": "/pdfs/a.pdf"})
        self.assertEqual(self.chroma_col.count(), 0)
        self.assertEqual(self.store_col.count(), 0)

    def test_get_loaded_docs_sans_filtre(self):
        gc = self.chroma_col.get(include=["metadatas"])
        gs = self.store_col.get(include=["metadatas"])
        doc_ids_c = {m["doc_id"] for m in gc["metadatas"]}
        doc_ids_s = {m["doc_id"] for m in gs["metadatas"]}
        self.assertEqual(doc_ids_c, {"docA", "docB"})
        self.assertEqual(doc_ids_s, {"docA", "docB"})


class HistoryTest(_Profil):
    """Profil `core/history.py` : jamais de `where`, delete par `ids`, `query` sans
    demander `distances`.
    """

    def setUp(self):
        super().setUp()
        ids = ["conv1", "conv2", "conv3"]
        docs = [_DOCS["chat1"], _DOCS["meteo1"], _DOCS["mixte"]]
        metadatas = [
            {"id": cid, "date": f"2026-08-{10+i}", "titre": f"Conversation {i}", "modèle": "ollama:x"}
            for i, cid in enumerate(ids)
        ]
        self._upsert_both(ids, docs, metadatas)

    def test_query_sans_distances(self):
        """Même remarque que `test_get_in_sans_relecture` : chromadb renvoie
        `"distances": None` plutôt que d'omettre la clé — sans conséquence, aucun
        appelant ne fait autre chose que `.get("distances", ...)`.
        """
        rc = self.chroma_col.query(query_texts=[_QUERY_CHAT], n_results=2,
                                    include=["documents", "metadatas"])
        rs = self.store_col.query(query_texts=[_QUERY_CHAT], n_results=2,
                                   include=["documents", "metadatas"])
        self.assertFalse(rc.get("distances"))
        self.assertFalse(rs.get("distances"))
        self.assertEqual(rc["ids"][0], rs["ids"][0])

    def test_delete_par_ids(self):
        self.chroma_col.delete(ids=["conv2"])
        self.store_col.delete(ids=["conv2"])
        self.assertEqual(self.chroma_col.count(), 2)
        self.assertEqual(self.store_col.count(), 2)
        self.assertNotIn("conv2", self.chroma_col.get()["ids"])
        self.assertNotIn("conv2", self.store_col.get()["ids"])

    def test_delete_sans_filtre_leve_dans_les_deux(self):
        with self.assertRaises(ValueError):
            self.chroma_col.delete()
        with self.assertRaises(ValueError):
            self.store_col.delete()
        # Ni l'un ni l'autre n'a rien supprimé en levant.
        self.assertEqual(self.chroma_col.count(), 3)
        self.assertEqual(self.store_col.count(), 3)


class ConcurrenceTest(unittest.TestCase):
    """Le verrou empêche-t-il une CORRUPTION, pas seulement une exception ?

    Deux threads sur la MÊME collection : l'un ré-upserte en boucle un jeu d'ids fixe
    (donc l'état final attendu est déterministe — connu à l'avance), l'autre interroge
    en boucle pendant ce temps. Le contrôle qui compte n'est pas « ça n'a pas planté »
    — c'est que l'état final soit EXACTEMENT celui attendu : bon nombre de lignes, bons
    ids, vecteurs de la bonne dimension, aucune métadonnée tronquée. Une corruption
    silencieuse (cache à moitié reconstruit lu par l'autre thread, écriture SQLite
    entrelacée) laisserait un symptôme ici même si aucune exception n'a été levée.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="epure-concurrence-")
        self.store = VectorStore(self._tmp, embedding_model=_MODEL)
        self.col = self.store.collection("test_col")
        self._ids = [f"id{i}" for i in range(5)]
        self._docs = [_DOCS["chat1"], _DOCS["chat2"], _DOCS["meteo1"],
                      _DOCS["meteo2"], _DOCS["mixte"]]
        self._metadatas = [{"source": "/x", "chunk": i} for i in range(5)]

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_upsert_et_query_concurrents_ne_corrompent_pas(self):
        erreurs: list[BaseException] = []
        ITER = 200

        def _boucle_upsert():
            try:
                for _ in range(ITER):
                    self.col.upsert(ids=self._ids, documents=self._docs,
                                     metadatas=self._metadatas)
            except BaseException as exc:  # noqa: BLE001 — capturé pour l'assertion finale
                erreurs.append(exc)

        def _boucle_query():
            try:
                for _ in range(ITER):
                    r = self.col.query(query_texts=[_QUERY_CHAT], n_results=3)
                    # Contrôle DANS la boucle, pas seulement à la fin : chaque
                    # résultat intermédiaire doit lui aussi être bien formé, sinon
                    # une corruption transitoire (visible seulement pendant la
                    # course, pas dans l'état final) passerait inaperçue.
                    assert len(r["ids"][0]) <= 3
                    assert len(r["ids"][0]) == len(r["distances"][0])
                    for i in r["ids"][0]:
                        assert i in self._ids
                    g = self.col.get()
                    assert set(g["ids"]).issubset(set(self._ids))
            except BaseException as exc:  # noqa: BLE001
                erreurs.append(exc)

        t1 = threading.Thread(target=_boucle_upsert)
        t2 = threading.Thread(target=_boucle_query)
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)

        self.assertFalse(t1.is_alive(), "le thread d'upsert ne s'est pas terminé (deadlock ?)")
        self.assertFalse(t2.is_alive(), "le thread de query ne s'est pas terminé (deadlock ?)")
        self.assertEqual(erreurs, [], f"erreurs pendant l'exécution concurrente : {erreurs}")

        # État final déterministe : exactement les 5 ids attendus, rien de plus,
        # rien de tronqué.
        self.assertEqual(self.col.count(), 5)
        final = self.col.get(include=["documents", "metadatas"])
        self.assertEqual(set(final["ids"]), set(self._ids))
        for doc in final["documents"]:
            self.assertIn(doc, self._docs, "document corrompu ou substitué")
        for meta in final["metadatas"]:
            self.assertEqual(set(meta.keys()), {"source", "chunk"}, "métadonnée tronquée")

        cache = self.col._ensure_loaded()
        self.assertEqual(cache["embeddings"].shape, (5, self.store._dim),
                          "vecteurs manquants ou de mauvaise dimension après la course")

    def test_upsert_concurrents_sur_ids_differents_pas_de_perte(self):
        """Deux threads qui upsertent chacun LEURS ids (pas les mêmes) : les deux
        jeux doivent survivre entiers — un verrou qui laisserait passer une écriture
        sans la committer avant la suivante perdrait silencieusement des lignes.
        """
        ids_a = [f"a{i}" for i in range(30)]
        ids_b = [f"b{i}" for i in range(30)]
        docs_a = [_DOCS["chat1"]] * 30
        docs_b = [_DOCS["meteo1"]] * 30
        erreurs: list[BaseException] = []

        def _upsert_un_par_un(ids, docs):
            try:
                for rid, doc in zip(ids, docs):
                    self.col.upsert(ids=[rid], documents=[doc], metadatas=[{"source": "/x"}])
            except BaseException as exc:  # noqa: BLE001
                erreurs.append(exc)

        t1 = threading.Thread(target=_upsert_un_par_un, args=(ids_a, docs_a))
        t2 = threading.Thread(target=_upsert_un_par_un, args=(ids_b, docs_b))
        t1.start(); t2.start()
        t1.join(timeout=60); t2.join(timeout=60)

        self.assertEqual(erreurs, [])
        self.assertEqual(self.col.count(), 60, "des lignes ont disparu sous écriture concurrente")
        ids_finaux = set(self.col.get()["ids"])
        self.assertEqual(ids_finaux, set(ids_a) | set(ids_b))


if __name__ == "__main__":
    unittest.main(verbosity=2)
