"""L'aperçu d'un document déjà chargé n'est plus vide — et le ET multi-clés qui le porte.

Ce que ce fichier prouve, et pourquoi il existe :

`core/docanalysis.py::load_document_streaming` récupère l'aperçu d'un document
déjà en cache avec ``get(where={"doc_id": …, "chunk_index": 0})``. Tant que le
stockage était chromadb, cet appel **levait systématiquement** — chromadb rejette
tout `where` de plus d'une clé (`validate_where` : ``len(where) != 1``) — et un
``except Exception: pass`` avalait l'échec : `apercu` restait vide, sans trace, et
la relecture du code ne pouvait pas le révéler. `core/vector_store.py` a d'abord
reproduit ce rejet volontairement (périmètre « remplacement de stockage seulement »),
puis l'a levé une fois chromadb retiré.

D'où deux niveaux de preuve, parce que l'un sans l'autre ne vaut pas grand-chose :

1. **Le filtre lui-même** : plusieurs clés se combinent par ET, pas par OU, et pas
   en ignorant les suivantes.
2. **Le chemin réel jusqu'à l'aperçu** : on fait tourner le vrai
   `load_document_streaming` et on regarde ce qui sort dans l'événement `done`.
   C'est la seule vérification qui aurait attrapé le bug d'origine — celui-ci
   passait tous les tests unitaires du filtre, puisque le filtre n'était pas en
   cause.

Aucun modèle d'embedding n'est chargé : le vrai `VectorStore` en construirait
un, ce qui demande les 90 Mo de poids que la suite ne télécharge jamais
(`_test_env` : dossier vide + `EPURE_EMBEDDING_AUTOINSTALL=0`). C'était
`sentence-transformers` et ses 17 s d'import avant le 2026-08-26 ; le motif du
double n'a pas changé, seule la raison de l'absence. La collection est remplacée par un double qui délègue son filtrage aux
**vraies** fonctions de `core/vector_store` — ce sont elles qu'on teste, pas une
réimplémentation qui pourrait diverger en silence.

Usage :
    python test_docanalysis_apercu.py
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les arbres AVANT tout import de core.*

from core import docanalysis as docanalysis_module  # noqa: E402
from core.docanalysis import DocAnalysisEngine  # noqa: E402
from core.vector_store import _matches, _valider_where  # noqa: E402

_TEXTE_CHUNK_0 = (
    "Chapitre premier — cet extrait doit se retrouver tel quel dans l'aperçu, "
    "c'est tout l'objet du test."
)


class CollectionDouble:
    """Collection en mémoire qui filtre avec les VRAIES fonctions du store.

    Ne réimplémente pas `where` : elle appelle `_valider_where` et `_matches` de
    `core/vector_store.py`. Un test qui referait le filtrage à sa façon prouverait
    seulement que le test sait filtrer.
    """

    def __init__(self, lignes: list[tuple[str, str, dict]]):
        self._lignes = lignes

    def get(self, ids=None, where=None, include=("documents", "metadatas")):
        if where is not None:
            _valider_where(where, "get")
        choisies = [
            (rid, doc, meta) for rid, doc, meta in self._lignes
            if (ids is None or rid in ids) and (where is None or _matches(meta, where))
        ]
        resultat = {"ids": [r[0] for r in choisies]}
        if "documents" in include:
            resultat["documents"] = [r[1] for r in choisies]
        if "metadatas" in include:
            resultat["metadatas"] = [r[2] for r in choisies]
        return resultat


class _FaussePage:
    pass


class _FauxLecteurPdf:
    """`pypdf.PdfReader` réduit à ce que le chemin « déjà en cache » consomme."""

    def __init__(self, _chemin):
        self.pages = [_FaussePage(), _FaussePage(), _FaussePage()]


class FiltreMultiClesTest(unittest.TestCase):
    """Le ET, isolément. `get`/`query`/`delete` partagent ces deux fonctions."""

    METAS = [
        {"doc_id": "docA", "chunk_index": 0},
        {"doc_id": "docA", "chunk_index": 1},
        {"doc_id": "docB", "chunk_index": 0},
    ]

    def test_deux_cles_selectionnent_une_seule_ligne(self):
        trouves = [m for m in self.METAS if _matches(m, {"doc_id": "docA", "chunk_index": 0})]
        self.assertEqual(trouves, [{"doc_id": "docA", "chunk_index": 0}])

    def test_c_est_un_ET_pas_un_OU(self):
        """La moitié qui compte : un OU renverrait 3 lignes sur 3 ici."""
        trouves = [m for m in self.METAS if _matches(m, {"doc_id": "docA", "chunk_index": 9})]
        self.assertEqual(trouves, [])

    def test_les_cles_suivantes_ne_sont_pas_ignorees(self):
        """Une implémentation qui ne lirait que la 1re clé renverrait 2 lignes."""
        trouves = [m for m in self.METAS if _matches(m, {"doc_id": "docA", "chunk_index": 1})]
        self.assertEqual(len(trouves), 1)

    def test_une_seule_cle_marche_toujours(self):
        trouves = [m for m in self.METAS if _matches(m, {"doc_id": "docA"})]
        self.assertEqual(len(trouves), 2)

    def test_in_se_combine_avec_une_egalite(self):
        trouves = [
            m for m in self.METAS
            if _matches(m, {"doc_id": {"$in": ["docA", "docB"]}, "chunk_index": 0})
        ]
        self.assertEqual(len(trouves), 2)

    def test_un_where_vide_est_refuse(self):
        """Seule contrainte qui subsiste : elle protège d'un appelant qui croit
        filtrer alors qu'il ne filtre rien. `get()` sans `where` reste le moyen
        explicite de tout lire.
        """
        with self.assertRaises(ValueError):
            _valider_where({}, "get")


class ApercuTest(unittest.TestCase):
    """Le chemin réel : `load_document_streaming` sur un document déjà en cache."""

    def setUp(self):
        fichier = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        # Contenu sans importance : `pypdf.PdfReader` est doublé.
        fichier.write(b"%PDF-1.4")
        fichier.close()
        self.chemin = fichier.name
        self.addCleanup(lambda: os.path.exists(self.chemin) and os.unlink(self.chemin))

        self.engine = DocAnalysisEngine.__new__(DocAnalysisEngine)
        self.engine._llm = None
        doc_id = self.engine._make_doc_id(self.chemin)
        self.doc_id = doc_id
        self.engine._col = CollectionDouble([
            (f"{doc_id}::0", _TEXTE_CHUNK_0,
             {"doc_id": doc_id, "chunk_index": 0, "n_pages": 3, "n_chunks": 2}),
            (f"{doc_id}::1", "Chapitre second — ne doit PAS servir d'aperçu.",
             {"doc_id": doc_id, "chunk_index": 1, "n_pages": 3, "n_chunks": 2}),
        ])

    def _charger(self) -> dict:
        with mock.patch.object(docanalysis_module.pypdf, "PdfReader", _FauxLecteurPdf):
            evenements = list(self.engine.load_document_streaming(self.chemin))
        done = [e for e in evenements if e.get("type") == "done"]
        self.assertEqual(len(done), 1, f"un seul événement done attendu : {evenements}")
        return done[0]["doc"]

    def test_l_apercu_n_est_plus_vide(self):
        """LE test de non-régression. Il échouait avant la correction, avec un
        `apercu` vide et aucune exception — exactement le symptôme d'origine.
        """
        doc = self._charger()
        self.assertTrue(doc["cached"], "prérequis : on teste bien le chemin « déjà chargé »")
        self.assertTrue(doc["apercu"], "l'aperçu ne doit plus être vide")
        self.assertEqual(doc["apercu"], _TEXTE_CHUNK_0[:300])

    def test_l_apercu_vient_du_chunk_0_et_pas_d_un_autre(self):
        """Sans ça, un `where` réduit à `{"doc_id": …}` passerait le test précédent
        en renvoyant le premier chunk venu — l'ordre de `get()` n'est pas garanti.
        """
        doc = self._charger()
        # Affirmation POSITIVE d'abord : `assertNotIn` seul passerait sur un aperçu
        # vide, donc aussi avant la correction — un test vert par vacuité.
        self.assertTrue(doc["apercu"].startswith("Chapitre premier"))
        self.assertNotIn("Chapitre second", doc["apercu"])

    def test_une_erreur_reelle_est_journalisee_et_non_avalee(self):
        """L'autre moitié de la correction : le `except Exception: pass` masquait
        TOUT. Une panne réelle doit désormais laisser une trace — sans faire
        échouer le chargement, qui, lui, a réussi.
        """
        # Ne casser QUE l'appel d'aperçu. Casser `get` entièrement ferait aussi
        # échouer la vérification de cache juste avant, et le code partirait dans la
        # branche « document non chargé » — on ne testerait plus rien de l'aperçu
        # (constaté : le test échouait sur « Impossible de lire ce PDF »).
        get_reel = self.engine._col.get

        def get_qui_casse_sur_l_apercu(*args, **kwargs):
            if len(kwargs.get("where") or {}) > 1:
                raise RuntimeError("panne simulée du store")
            return get_reel(*args, **kwargs)

        self.engine._col.get = get_qui_casse_sur_l_apercu
        with self.assertLogs("core.docanalysis", level="ERROR") as journal:
            with mock.patch.object(docanalysis_module.pypdf, "PdfReader", _FauxLecteurPdf):
                evenements = list(self.engine.load_document_streaming(self.chemin))

        self.assertTrue(any(r.exc_info for r in journal.records),
                        "la trace de l'exception doit être journalisée")
        # Le document reste chargeable : l'aperçu est cosmétique.
        erreurs = [e for e in evenements if e.get("type") == "error"]
        self.assertEqual(erreurs, [], "une panne d'aperçu ne doit pas faire échouer le chargement")


if __name__ == "__main__":
    unittest.main(verbosity=2)
