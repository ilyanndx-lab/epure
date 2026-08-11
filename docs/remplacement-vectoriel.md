# Remplacer chromadb — la coquille ne doit pas dépendre d'une base vectorielle de production

**Objectif.** Le RAG documentaire garde exactement son comportement actuel (ajouter,
chercher, supprimer, lister des collections de documents) sur un stockage qui a des
wheels sur toutes les architectures visées — y compris Windows ARM64 — sans rien
compiler, sans shim, sans liste d'exclusions à maintenir.

**Pourquoi maintenant, et pourquoi ce n'est pas un contournement de plus.** La soirée a
montré une escalade, pas une série d'accidents isolés : `grpcio` (dépendance transitive
inutile), puis `kubernetes` (idem), puis `google-generativeai` (fournisseur cloud
optionnel), puis le cœur Rust de `chromadb` lui-même, qui n'a **aucune** wheel Windows
ARM64 et exige `maturin` + un toolchain Rust pour se construire depuis la source. Chaque
couche retirée a exposé la suivante parce que `chromadb` est une base vectorielle conçue
pour de la production à grande échelle (client Kubernetes, mode distribué par gRPC,
bindings Rust) — disproportionnée pour chercher dans les documents d'une seule personne.
Continuer à la patcher, c'est traiter le symptôme sans fin. La retirer traite la cause.

**Ce que ça ne change pas.** Le comportement pour l'utilisateur (chercher dans ses
documents depuis le chat) reste identique. Ça touche la couche de stockage, pas la
fonctionnalité.

---

## §0 — Ce qui doit être vérifié avant de choisir, pas supposé — **fait le 2026-08-11**

### 1. `sqlite-vec` — **aucune wheel `win_arm64`, dans aucune version**

Vérifié sur tout l'historique publié sur PyPI, de `0.0.1a3` à `0.1.9` (version courante).
À chaque version : `win_amd64`, `macosx_x86_64`, `macosx_arm64`, et depuis `0.1.5`
`manylinux_aarch64` (Linux ARM). **Jamais de `win_arm64`.** Ce n'est pas un oubli récent
à rattraper au prochain release — c'est absent depuis la première version alpha.

**Le candidat présenté comme naturel ci-dessus ne résout pas le problème qu'il est censé
résoudre.** L'utiliser demanderait de compiler l'extension depuis les sources sur la
machine ARM64 — retombe exactement dans le piège que ce chantier cherche à éviter.
Écarté.

### 2. `numpy` — **wheel `win_arm64` présente, couvre la version déjà épinglée**

`numpy==2.5.2` (la version dans `backend/requirements.txt` et
`tools/contraintes-paquet.txt`) publie `numpy-2.5.2-cp312-cp312-win_arm64.whl`. Confirmé
aussi sur toutes les versions 2.4.x/2.5.x récentes — le support `win_arm64` est là depuis
plusieurs releases, pas un ajout fragile de la dernière version. Rien à recompiler.

### 3. L'API exacte utilisée aujourd'hui — **`core/rag.py` n'est pas le seul appelant**

```python
self._client = chromadb.PersistentClient(path=db_path)
self._ef = SentenceTransformerEmbeddingFunction(model_name="sentence-transformers/all-MiniLM-L6-v2")
self._col = self._client.get_or_create_collection("fiches", embedding_function=self._ef)

self._col.delete(where={"source": str(path)})
self._col.upsert(documents=[...], ids=[...], metadatas=[{"source", "chunk", "mtime"}, ...])
self._col.count()
self._col.query(query_texts=[text], n_results=n)
self._col.query(query_texts=[text], n_results=n, where={"source": {"$in": paths}})
self._col.get(where={"source": {"$in": paths}}, include=[])       # compter des ids sans relire
self._col.get(include=["metadatas"])                                # tout relire (2 usages)
```

Trois écarts avec ce que ce paragraphe supposait avant vérification :
- **`add` n'est jamais appelé — c'est `upsert` partout**, précédé d'un `delete(where=...)`
  pour éviter les doublons de ré-indexation. `update()` n'existe nulle part : `upsert`
  fait les deux jobs.
- **`list_collections()` n'apparaît jamais.** La « réouverture après redémarrage » n'est
  pas un appel explicite : `RAGEngine.__init__` retourne au démarrage suivant (nouvelle
  instance via `_LazyEngine`), rappelle `PersistentClient(path=db_path)` sur le même
  chemin, et `get_or_create_collection` retrouve la collection déjà sur disque.
- **L'embedding n'est jamais fait à la main.** `documents=`/`query_texts=` sont du texte
  brut ; `SentenceTransformerEmbeddingFunction`, attaché à la collection, est appelé par
  chromadb en interne. Le remplacement doit répliquer cet appel lui-même — ce n'est pas
  gratuit avec du stockage brut.

**Découverte qui change le périmètre du reste du plan : `core/rag.py` n'est pas le seul
consommateur du client chromadb.** `core/runtime.py:129-136` :

```python
docanalysis = _LazyEngine(lambda: DocAnalysisEngine(chroma_client=rag._client, embedding_function=rag._ef, llm=llm), ...)
history_engine = _LazyEngine(lambda: HistoryEngine(llm, rag._client, rag._ef), ...)
```

`DocAnalysisEngine` (`core/docanalysis.py`) et `HistoryEngine` (`core/history.py`)
réutilisent directement `rag._client`/`rag._ef` — attributs privés de `RAGEngine` — pour
gérer deux autres collections sur le **même** `PersistentClient`. Leur API, mesurée dans
le code, est un peu plus large que celle de `core/rag.py` :

| | `where` utilisés | `include` demandés | `delete` par |
|---|---|---|---|
| `core/rag.py` (« fiches ») | égalité, `$in` | `[]`, `["metadatas"]` | `where` |
| `core/docanalysis.py` (« doc_analysis ») | égalité, **AND multi-clés** (`{"doc_id":…, "chunk_index":0}`) | `[]`, `["documents"]`, `["documents","metadatas"]`, `["metadatas"]` | `where` |
| `core/history.py` (« history ») | — (jamais de `where`) | `["documents","metadatas"]` | **`ids`**, pas `where` |

`core/docanalysis.py:145-153` est le seul des trois à lire `results["distances"]` — pour
calculer un score affiché (`1.0 - distance`). Aucun autre appelant ne le fait aujourd'hui,
mais rien ne doit empêcher de le faire.

Aucune section de ce document ne mentionnait `core/docanalysis.py` ni `core/history.py`
avant cette vérification. Une abstraction qui ne couvrirait que `core/rag.py` laisserait
deux clients chromadb (l'ancien pour les deux autres, le nouveau pour le RAG) coexister
silencieusement après la migration.

### 4. Volume réel de `chroma_db/`

24 Mo sur disque (`chroma.sqlite3` = 9,6 Mo + 3 dossiers de segments HNSW/métadonnées).
Mesuré en lecture seule via `PersistentClient`, une collection par appelant identifié
ci-dessus :

| Collection | Chunks | Propriétaire |
|---|---|---|
| `fiches` | 122 | `core/rag.py` |
| `doc_analysis` | 34 | `core/docanalysis.py` |
| `history` | 14 | `core/history.py` |
| **Total** | **170** | |

À cette échelle, une comparaison cosinus par force brute (contre tous les vecteurs d'une
collection) est triviale en temps de calcul — aucune structure d'index approximatif
n'est justifiée, dans aucune des trois collections.

---

## §1 — Étapes

### Étape A — Isoler l'interface — **portée corrigée par §0.3**

Une seule abstraction (`core/vector_store.py`), pas une par appelant : `core/rag.py`,
`core/docanalysis.py` **et** `core/history.py` (`HistoryEngine`) passent tous les trois
par elle, jamais directement par chromadb ni par son remplaçant. Les trois se
partageaient déjà un seul `PersistentClient` (via `rag._client`/`rag._ef`,
`core/runtime.py:129-136`) — la nouvelle abstraction porte cette même relation
explicitement, au lieu de la laisser passer par deux attributs privés d'un objet qui n'a
pas vocation à être un point de partage.

**Backend retenu, d'après §0.1/§0.2/§0.4 : SQLite + numpy, cosinus par force brute.**
`sqlite-vec` est écarté (aucune wheel `win_arm64`, §0.1) ; `numpy` a la sienne pour la
version déjà épinglée (§0.2) ; 170 chunks au total sur les trois collections (§0.4) ne
justifient aucune structure d'index — comparer une requête contre 170 vecteurs en numpy
est de l'ordre du milliseconde, pas un goulot à optimiser prématurément.

**L'appel à `SentenceTransformerEmbeddingFunction` devient explicite dans
l'abstraction.** Aujourd'hui il est cousu à la collection chromadb et invoqué en
interne, invisible depuis `core/rag.py`/`core/docanalysis.py`/`core/history.py` (§0.3).
Le remplacement l'assume ouvertement : le modèle est chargé une fois par
`VectorStore.__init__`, partagé entre les collections, et appelé par cette classe elle-même
avant chaque écriture et chaque requête — pas caché derrière une interface qui imite
`embedding_function=`.

**`query()` doit renvoyer les distances**, toujours quand demandées dans `include` — pas
seulement pour `core/docanalysis.py`, le seul des trois à les lire aujourd'hui (§0.3). Un
appelant qui n'en a pas besoin ne les demande pas ; l'abstraction ne doit pas décider à
sa place qu'elles sont superflues pour tout le monde parce qu'elles le sont pour deux
appelants sur trois.

Interface proposée, **à valider avant tout code** — signatures et docstrings
uniquement, aucune logique d'implémentation :

```python
class VectorStore:
    """Remplace chromadb.PersistentClient — SQLite + numpy, cosinus par force brute.

    Un fichier `<path>/vectors.sqlite3`, une table par collection. Les vecteurs sont
    des BLOB numpy (float32) ; à 170 chunks au total (§0.4), les tenir en mémoire par
    collection après lecture ne pose aucun problème et évite de re-décoder les BLOB à
    chaque requête.
    """

    def __init__(self, path: str | Path,
                 embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"):
        """Charge le modèle d'embedding UNE fois ici, partagé par toutes les
        collections — c'est l'appel qui était caché dans `embedding_function=` côté
        chromadb, maintenant fait explicitement par cette classe.
        """

    def collection(self, name: str) -> "Collection":
        """Équivalent de `get_or_create_collection` : crée la table SQLite si absente,
        renvoie un handle liant le nom à cette table et au modèle partagé.
        """


class Collection:
    """Un handle par collection (`fiches`, `doc_analysis`, `history` aujourd'hui) —
    jamais construit directement, toujours via `VectorStore.collection(name)`.
    """

    def count(self) -> int:
        """Nombre total d'items. Utilisé par les trois appelants pour éviter un
        `n_results` supérieur au nombre d'items disponibles (chromadb lève sinon).
        """

    def upsert(self, ids: list[str], documents: list[str], metadatas: list[dict]) -> None:
        """Insère ou remplace par id. L'embedding des `documents` est calculé ICI
        (modèle partagé du `VectorStore`), avant l'écriture — jamais délégué à
        l'appelant, jamais implicite comme l'était `embedding_function=`.
        """

    def get(self, ids: list[str] | None = None, where: dict | None = None,
            include: list[str] = ("documents", "metadatas")) -> dict:
        """`ids` et `where` sont exclusifs, comme chez chromadb. `where` doit couvrir
        les trois formes mesurées en §0.3 : égalité (`{"champ": valeur}`), `$in`
        (`{"champ": {"$in": [...]}}`), et AND implicite entre plusieurs clés
        (`{"doc_id": x, "chunk_index": 0}` — `core/docanalysis.py`). `include=[]` doit
        rester utilisable pour ne récupérer QUE les ids (compter sans relire les
        documents, cf. `core/rag.py::_do_query_filtered` et
        `core/docanalysis.py::load_document_streaming`).
        Renvoie un dict à listes PLATES (`{"ids": [...], "documents": [...], ...}`) —
        même forme que chromadb pour `get()`, pour ne rien réécrire côté appelants
        au-delà du client lui-même.
        """

    def query(self, query_texts: list[str], n_results: int, where: dict | None = None,
              include: list[str] = ("documents", "metadatas", "distances")) -> dict:
        """Embed chaque texte de `query_texts` (modèle partagé), calcule la similarité
        cosinus contre tous les vecteurs de la collection (ou le sous-ensemble filtré
        par `where`), trie, renvoie les `n_results` meilleurs. `distances` renvoyées
        dès qu'elles apparaissent dans `include` — pas seulement pour
        `core/docanalysis.py`, le seul appelant qui les lit aujourd'hui.
        Renvoie un dict à listes DE LISTES, une par entrée de `query_texts`
        (`{"documents": [[...]], "distances": [[...]], ...}`) — même forme que
        chromadb pour `query()`, alors même qu'aujourd'hui `query_texts` n'a jamais
        qu'un seul élément dans les trois appelants (ils font tous `[0]` sur le
        résultat).
        """

    def delete(self, ids: list[str] | None = None, where: dict | None = None) -> None:
        """`ids` et `where` sont exclusifs, comme pour `get()`. Les deux formes
        existent déjà séparément : `core/history.py` supprime par `ids`
        (`col.delete(ids=[conv_id])`), `core/rag.py` et `core/docanalysis.py` par
        `where` (`col.delete(where={"source": path})`,
        `col.delete(where={"doc_id": doc_id})`).
        """
```

Aucun changement de comportement à cette étape : les trois appelants continuent de voir
les mêmes formes de retour qu'aujourd'hui, seul le client change. Rien de tout ceci n'est
codé — c'est l'interface à valider avant l'étape B.

### Étape B — Implémenter avec le backend retenu

SQLite + numpy, selon l'interface validée à l'étape A (§0, §1). Un test de
non-régression : les mêmes requêtes sur les mêmes documents renvoient les mêmes
résultats (ou un ordre équivalent) qu'avec `chromadb` aujourd'hui, pour les trois
collections — pas seulement `fiches`.

### Étape C — Migration des données existantes

Un script qui lit le `chroma_db/` actuel via l'ancien client et réécrit dans le nouveau
format. Vérification obligatoire avant de supprimer quoi que ce soit : comparer les
résultats d'une même requête sur les deux stockages, sur les vraies données d'Ilyann.
Ne jamais supprimer l'ancien `chroma_db/` avant cette comparaison.

### Étape D — Retirer chromadb et sa grappe

`chromadb`, `grpcio`, `opentelemetry-exporter-otlp-proto-grpc`, et tout le mécanisme
construit ce soir pour les contourner (`sitecustomize.py`, `PURGE_DISTRIBUTIONS`,
les exclusions ciblées dans `faire_paquet.py`). Si le nouveau backend n'a besoin d'aucun
de ces contournements, les faire disparaître simplifie le script au lieu de l'alourdir.
Les tests de `test_paquet.py` qui ciblaient spécifiquement ce mécanisme
(`ExigencesTest`, `PurgeDistributionsTest`, `SitecustomizeTest`) sont à retirer ou
réécrire pour la nouvelle réalité — pas à laisser comme code mort.

### Étape E — Reconstruire et remesurer

Nouveau poids du paquet x64, suite de tests complète, puis **seulement à ce stade** un
nouvel essai de construction ARM64 — sur la machine de sandr ou une autre, avec
l'attente que ça passe directement puisque les wheels auront été vérifiées au §0, pas
découvertes en marchant dessus.

---

## §5 — Hors périmètre

**Améliorer la qualité de la recherche documentaire.** C'est un remplacement de
stockage, pas une refonte du RAG. Le comportement de recherche doit rester équivalent,
pas meilleur — une amélioration est un chantier séparé, à ne pas mélanger avec celui-ci.

---

## §6 — Ce qui n'a pas été vérifié

Les deux premiers points de cette liste ont été mesurés le 2026-08-11 (§0) et sont
retirés d'ici — `sqlite-vec` n'a pas de wheel `win_arm64` (écarté), `numpy` en a une pour
la version épinglée, le volume réel est de 170 chunks sur trois collections. Ce qui
reste :

- **Le temps de requête réel en recherche par force brute.** « De l'ordre du
  milliseconde » (§1, étape A) est une estimation à 170 vecteurs de petite dimension
  (`all-MiniLM-L6-v2` : 384 dimensions), pas un chiffre mesuré sur ce dépôt. À vérifier à
  l'étape B, une fois l'implémentation écrite — pas avant, ça n'aurait rien à mesurer.
- **L'interface proposée en étape A n'est pas encore validée par Ilyann.** Rien après ce
  point ne doit être codé tant que ce n'est pas fait.
