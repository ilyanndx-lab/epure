"""Remplace chromadb.PersistentClient — SQLite + numpy, cosinus par force brute.

`docs/remplacement-vectoriel.md`. chromadb est une base vectorielle conçue pour de la
production à grande échelle (client Kubernetes, mode distribué par gRPC, bindings Rust) —
disproportionnée pour chercher dans les documents d'une seule personne, et son cœur Rust
n'a aucune wheel Windows ARM64. `sqlite-vec`, le remplacement le plus évident, n'en a pas
non plus (§0.1 du plan, vérifié sur tout son historique PyPI) — `numpy`, lui, en a une
pour la version déjà épinglée (§0.2). À 170 chunks au total sur les trois collections
réelles (§0.4), une comparaison cosinus par force brute est de l'ordre du milliseconde :
aucune structure d'index n'est justifiée.

Interface et décisions figées avant ce fichier — ce module ne fait qu'implémenter ce qui
a été validé, rien de plus :

- Une seule connexion SQLite (`<path>/vectors.sqlite3`, une table par collection),
  partagée par toutes les `Collection`. `sqlite3` ne rend pas l'usage d'UNE connexion
  concurrent-safe entre threads (``check_same_thread=False`` lève seulement la
  vérification, ça ne protège de rien) : un seul ``threading.RLock`` sur `VectorStore`
  sérialise donc tout accès — lectures et écritures, sur toutes les méthodes de
  `Collection`. RLock parce que `query()` réutilise en interne la même logique de
  filtrage que `get()`.

  Régression assumée par rapport à chromadb : une écriture sur `doc_analysis` bloque une
  lecture sur `fiches` pendant sa durée (chromadb permettait des accès concurrents entre
  collections indépendantes). Sans conséquence mesurable à 170 chunks et un seul
  utilisateur — le verrou est tenu quelques microsecondes — mais c'est une vraie limite
  si le volume ou la concurrence grandissent un jour.

- Le cache mémoire par collection (vecteurs numpy + ids + métadonnées, lus depuis
  SQLite) est entièrement invalidé à chaque `upsert`/`delete`, reconstruit paresseusement
  à la lecture suivante. Pas de mise à jour incrémentale : à ce volume, relire toute la
  table coûte de l'ordre du milliseconde, moins cher que le bookkeeping id→index qu'une
  mise à jour incrémentale demanderait.

- `get()` sans `ids` ni `where` renvoie tout ; `delete()` sans `ids` ni `where` lève
  `ValueError` — vérifié empiriquement sur le vrai chromadb (pas supposé), et c'est
  exactement son comportement.

- L'embedding (`sentence-transformers/all-MiniLM-L6-v2`, normalisé) est calculé
  explicitement par ce module, avant chaque écriture et chaque requête — plus caché
  derrière un `embedding_function=` fourni à la collection, comme chez chromadb.

- Les distances renvoyées sont `1 - similarité_cosinus` (plus petit = plus proche) —
  vérifié que c'est exactement ce que chromadb renvoyait déjà pour ces collections
  (mesuré : `col.query(...)["distances"]` sur les données réelles correspond à
  `1 - cos` recalculé indépendamment, pas à `2*(1-cos)` qu'aurait donné un espace L2 sur
  des vecteurs normalisés). `core/docanalysis.py::search` fait déjà `1.0 - dist` pour
  afficher un score — cette formule n'a de sens que si `dist` est bien `1 - cos`, ce
  qu'elle est, avant comme après ce module.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_table(name: str) -> str:
    """Nom de collection → nom de table SQLite, avec garde-fou.

    Les noms de collection sont aujourd'hui des littéraux du code (« fiches »,
    « doc_analysis », « history »), jamais une entrée utilisateur — mais un nom de table
    ne peut pas être paramétré dans une requête SQL (`?` ne marche que pour des valeurs),
    donc toute interpolation de chaîne mérite une liste blanche explicite plutôt que de
    supposer que l'appelant restera toujours un littéral.
    """
    if not _TABLE_NAME_RE.match(name):
        raise ValueError(f"nom de collection invalide : {name!r}")
    return f"col_{name}"


def _valider_where(where: dict, ou: str) -> None:
    """Le vrai chromadb (1.5.9) rejette tout `where` à plus d'une clé — `validate_where`
    lève avant même de chercher : ``len(where) != 1`` → `ValueError`. Ce n'est PAS un
    choix de ce module : `core/docanalysis.py::load_document_streaming` appelle bien
    ``get(where={"doc_id": doc_id, "chunk_index": 0})`` (une intention de AND que le
    nom de la variable suggère), mais get un `ValueError` de chromadb à l'exécution,
    silencieusement absorbé par son propre `except Exception: pass` — `apercu` reste
    vide. Mesuré en écrivant le test de non-régression (§1, étape B), pas supposé en
    lisant le code : la description initiale de l'interface (§0.3/étape A du plan)
    disait « AND implicite entre plusieurs clés », ce qui était faux. Reproduire ce
    rejet ici, plutôt que de laisser cet appel réussir, est ce qui fait de ce module un
    remplacement fidèle et non une correction silencieuse d'un bug indépendant.
    """
    if len(where) != 1:
        raise ValueError(f"Expected where to have exactly one operator, got {where} in {ou}.")


def _matches(metadata: dict, where: dict) -> bool:
    """Une seule clé (`_valider_where` l'impose) : égalité ou `$in` — les deux seules
    formes réellement exercées par `core/rag.py`/`core/docanalysis.py`/`core/history.py`.

    Filtrage en Python sur les métadonnées déjà en mémoire, pas en SQL : à 170 lignes au
    total par collection, traduire `where` en clauses SQL sur du JSON n'apporterait rien.
    """
    ((champ, condition),) = where.items()
    valeur = metadata.get(champ)
    if isinstance(condition, dict) and "$in" in condition:
        return valeur in condition["$in"]
    return valeur == condition


class VectorStore:
    """Un fichier `<path>/vectors.sqlite3`, une table par collection.

    Charge le modèle d'embedding UNE fois ici, partagé par toutes les collections —
    c'était `embedding_function=`, caché dans la collection chromadb ; c'est maintenant
    cette classe, explicitement.
    """

    def __init__(self, path: str | Path,
                 embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self._dir = Path(path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._dir / "vectors.sqlite3"), check_same_thread=False
        )
        self._model = SentenceTransformer(embedding_model)
        self._dim = self._model.get_embedding_dimension()
        self._collections: dict[str, "Collection"] = {}

    def _embed(self, texts: list[str]) -> np.ndarray:
        """Vecteurs normalisés (norme 1) — la similarité cosinus devient un simple
        produit scalaire, et les valeurs reproduisent celles que chromadb calculait
        (`SentenceTransformerEmbeddingFunction` normalise aussi, cf. docstring module).
        """
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        vecteurs = self._model.encode(
            texts, convert_to_numpy=True, normalize_embeddings=True
        )
        return np.asarray(vecteurs, dtype=np.float32)

    def collection(self, name: str) -> "Collection":
        """Équivalent de `get_or_create_collection` : crée la table SQLite si absente."""
        with self._lock:
            col = self._collections.get(name)
            if col is None:
                col = Collection(self, name)
                self._collections[name] = col
            return col


class Collection:
    """Un handle par collection — jamais construit directement, toujours via
    `VectorStore.collection(name)`.
    """

    def __init__(self, store: VectorStore, name: str):
        self._store = store
        self._name = name
        self._table = _safe_table(name)
        with store._lock:
            store._conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{self._table}" ('
                f"id TEXT PRIMARY KEY, document TEXT, metadata TEXT, embedding BLOB)"
            )
            store._conn.commit()
        self._cache: dict | None = None

    # ── Cache mémoire ────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> dict:
        """Appelé sous le verrou. Reconstruit le cache depuis SQLite si invalidé."""
        if self._cache is not None:
            return self._cache
        cur = self._store._conn.execute(
            f'SELECT id, document, metadata, embedding FROM "{self._table}"'
        )
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict] = []
        vecteurs: list[np.ndarray] = []
        for rid, document, metadata_json, embedding_blob in cur.fetchall():
            ids.append(rid)
            documents.append(document)
            metadatas.append(json.loads(metadata_json) if metadata_json else {})
            vecteurs.append(np.frombuffer(embedding_blob, dtype=np.float32))
        embeddings = (
            np.vstack(vecteurs) if vecteurs else np.zeros((0, self._store._dim), dtype=np.float32)
        )
        self._cache = {
            "ids": ids, "documents": documents, "metadatas": metadatas,
            "embeddings": embeddings,
        }
        return self._cache

    def _invalidate(self) -> None:
        self._cache = None

    # ── API ──────────────────────────────────────────────────────────────────

    def count(self) -> int:
        with self._store._lock:
            return len(self._ensure_loaded()["ids"])

    def upsert(self, ids: list[str], documents: list[str], metadatas: list[dict]) -> None:
        """L'embedding des `documents` est calculé ICI, avant l'écriture — jamais
        délégué à l'appelant, jamais implicite comme l'était `embedding_function=`.
        """
        with self._store._lock:
            embeddings = self._store._embed(documents)
            conn = self._store._conn
            for rid, document, metadata, embedding in zip(ids, documents, metadatas, embeddings):
                conn.execute(
                    f'INSERT INTO "{self._table}" (id, document, metadata, embedding) '
                    f"VALUES (?, ?, ?, ?) "
                    f"ON CONFLICT(id) DO UPDATE SET "
                    f"document=excluded.document, metadata=excluded.metadata, "
                    f"embedding=excluded.embedding",
                    (rid, document, json.dumps(metadata, ensure_ascii=False), embedding.tobytes()),
                )
            conn.commit()
            self._invalidate()

    def get(self, ids: list[str] | None = None, where: dict | None = None,
            include: tuple[str, ...] | list[str] = ("documents", "metadatas")) -> dict:
        """`ids` et `where` sont exclusifs, comme chez chromadb.

        Sans `ids` ni `where` : renvoie tout (§1, point 3 du plan — vérifié sur le vrai
        chromadb, pas supposé : c'est exactement ce que fait `get()` sans filtre
        aujourd'hui, exploité par `core/rag.py::get_indexed_files` et
        `core/docanalysis.py::get_loaded_docs`).
        """
        if ids is not None and where is not None:
            raise ValueError("ids et where sont exclusifs dans get()")
        if where is not None:
            _valider_where(where, "get")
        with self._store._lock:
            cache = self._ensure_loaded()
            if ids is not None:
                voulus = set(ids)
                indices = [i for i, rid in enumerate(cache["ids"]) if rid in voulus]
            elif where is not None:
                indices = [i for i, m in enumerate(cache["metadatas"]) if _matches(m, where)]
            else:
                indices = list(range(len(cache["ids"])))

            resultat: dict = {"ids": [cache["ids"][i] for i in indices]}
            if "documents" in include:
                resultat["documents"] = [cache["documents"][i] for i in indices]
            if "metadatas" in include:
                resultat["metadatas"] = [cache["metadatas"][i] for i in indices]
            return resultat

    def query(self, query_texts: list[str], n_results: int, where: dict | None = None,
              include: tuple[str, ...] | list[str] = ("documents", "metadatas", "distances")) -> dict:
        """Cosinus par force brute contre tous les vecteurs (ou le sous-ensemble filtré
        par `where`) de la collection. `distances` = `1 - similarité_cosinus`, toujours
        renvoyées si demandées dans `include` — cf. docstring module pour la
        vérification empirique que c'est exactement ce que chromadb renvoyait déjà.

        Renvoie des listes DE LISTES, une par entrée de `query_texts` — même forme que
        chromadb, alors même qu'aucun appelant actuel n'y passe plus d'un texte.
        """
        if where is not None:
            _valider_where(where, "query")
        with self._store._lock:
            cache = self._ensure_loaded()
            if where is not None:
                indices = [i for i, m in enumerate(cache["metadatas"]) if _matches(m, where)]
            else:
                indices = list(range(len(cache["ids"])))
            sous_embeddings = cache["embeddings"][indices] if indices else cache["embeddings"][:0]

            q_embeddings = self._store._embed(list(query_texts))

            out_ids: list[list[str]] = []
            out_documents: list[list[str]] = []
            out_metadatas: list[list[dict]] = []
            out_distances: list[list[float]] = []

            for q in q_embeddings:
                if not indices:
                    out_ids.append([])
                    out_documents.append([])
                    out_metadatas.append([])
                    out_distances.append([])
                    continue
                similarites = sous_embeddings @ q
                distances = 1.0 - similarites
                n = max(0, min(n_results, len(indices)))
                ordre_local = np.argsort(distances)[:n]
                choisis = [indices[j] for j in ordre_local]

                out_ids.append([cache["ids"][i] for i in choisis])
                out_documents.append([cache["documents"][i] for i in choisis])
                out_metadatas.append([cache["metadatas"][i] for i in choisis])
                out_distances.append([float(distances[j]) for j in ordre_local])

            resultat: dict = {"ids": out_ids}
            if "documents" in include:
                resultat["documents"] = out_documents
            if "metadatas" in include:
                resultat["metadatas"] = out_metadatas
            if "distances" in include:
                resultat["distances"] = out_distances
            return resultat

    def delete(self, ids: list[str] | None = None, where: dict | None = None) -> None:
        """`ids` et `where` sont exclusifs. Sans aucun des deux : lève `ValueError`,
        même message que chromadb (vérifié empiriquement, pas supposé) — un appel
        accidentel ne doit jamais vider une collection entière en silence.
        """
        if ids is not None and where is not None:
            raise ValueError("ids et where sont exclusifs dans delete()")
        if ids is None and where is None:
            raise ValueError(
                "At least one of ids, where, or where_document must be provided in delete."
            )
        if where is not None:
            _valider_where(where, "delete")
        with self._store._lock:
            cache = self._ensure_loaded()
            if ids is not None:
                voulus = set(ids)
                a_retirer = [rid for rid in cache["ids"] if rid in voulus]
            else:
                a_retirer = [
                    rid for rid, m in zip(cache["ids"], cache["metadatas"]) if _matches(m, where)
                ]
            if not a_retirer:
                return
            conn = self._store._conn
            conn.executemany(
                f'DELETE FROM "{self._table}" WHERE id = ?',
                [(rid,) for rid in a_retirer],
            )
            conn.commit()
            self._invalidate()
