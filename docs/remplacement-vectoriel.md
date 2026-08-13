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
| `core/docanalysis.py` (« doc_analysis ») | égalité, `$in`, et un AND à deux clés qui **rate déjà silencieusement** (§1, étape B) | `[]`, `["documents"]`, `["documents","metadatas"]`, `["metadatas"]` | `where` |
| `core/history.py` (« history ») | — (jamais de `where`) | `["documents","metadatas"]` | **`ids`**, pas `where` |

`core/docanalysis.py:145-153` est le seul des trois à lire `results["distances"]` — pour
calculer un score affiché (`1.0 - distance`). Aucun autre appelant ne le fait aujourd'hui,
mais rien ne doit empêcher de le faire.

**Correction faite en écrivant le test de non-régression de l'étape B, à ne pas reperdre
ici :** cette ligne disait initialement « AND multi-clés » comme une capacité à honorer —
lu dans le code (`core/docanalysis.py:51-52` appelle bien
`get(where={"doc_id": doc_id, "chunk_index": 0})`), jamais exécuté contre le vrai
chromadb. Exécuté : `validate_where` de chromadb 1.5.9 rejette tout `where` de plus d'une
clé (`ValueError: Expected where to have exactly one operator`) — l'appel lève, et
`core/docanalysis.py` l'entoure déjà d'un `except Exception: pass`, donc `apercu` reste
vide en silence. Un bug préexistant, indépendant de ce chantier. `core/vector_store.py`
reproduit exactement ce rejet plutôt que de faire réussir cet appel : le comportement
observable ne doit pas changer, y compris le bug.

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
        """`ids` et `where` sont exclusifs, comme chez chromadb. `where` accepte
        exactement UNE clé — égalité (`{"champ": valeur}`) ou `$in`
        (`{"champ": {"$in": [...]}}`) — comme le vrai chromadb, qui rejette tout `where`
        à plus d'une clé (`validate_where` : `len(where) != 1`). `core/docanalysis.py`
        appelle bien `get(where={"doc_id": x, "chunk_index": 0})`, mais cet appel lève
        déjà chez chromadb aujourd'hui — absorbé par son propre `except Exception: pass`
        (constaté à l'étape B, pas ici : corrigé rétroactivement dans ce paragraphe).
        `include=[]` doit rester utilisable pour ne récupérer QUE les ids (compter sans
        relire les documents, cf. `core/rag.py::_do_query_filtered` et
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

#### Trois points que l'interface ci-dessus laissait implicites

Rien de ce qui suit n'est codé — ce sont des décisions à figer dans l'interface, pas des
détails d'implémentation à reporter à l'étape B. Les trois affectent la signature ou le
contrat des méthodes ci-dessus, pas seulement leur corps.

**1. Verrouillage.** Un seul `sqlite3.Connection`, ouvert une fois par
`VectorStore.__init__`, partagé par toutes les `Collection` (même fichier, une table
chacune). Le module `sqlite3` de la bibliothèque standard n'autorise pas l'usage
concurrent d'UNE connexion depuis plusieurs threads — `check_same_thread=False` lève
seulement la vérification, ça ne rend rien thread-safe. Donc : **un seul
`threading.RLock`, sur `VectorStore`, partagé par toutes les collections** — pas un
verrou par collection, parce que la ressource réellement partagée est la connexion, pas
une table individuelle. `RLock` et non `Lock` : `query()` doit pouvoir réutiliser en
interne la même logique de filtrage que `get()` sans se bloquer elle-même. Le verrou
couvre le corps entier de chaque méthode publique de `Collection` — `count`, `upsert`,
`get`, `query`, `delete` — **lectures comprises**, pas seulement les écritures : sans ça,
une lecture concurrente à une écriture pourrait voir le cache mémoire du point 2 à moitié
reconstruit.

Régression assumée, à écrire dans le docstring de `VectorStore` et pas seulement ici :
chromadb permettait de facto des accès concurrents entre collections indépendantes
(chacune avait son propre chemin dans le moteur). Ici, une écriture sur `doc_analysis`
bloque une lecture sur `fiches` pendant sa durée. Sans conséquence mesurable à 170 chunks
et un seul utilisateur (le verrou est tenu quelques microsecondes), mais c'est une vraie
limite si le volume ou la concurrence grandissent un jour — à ne pas laisser se découvrir
en silence.

**2. Invalidation du cache mémoire — celui du nouveau store, pas le LRU existant de
`core/rag.py`.** Deux caches distincts, à ne pas confondre :

- Le LRU de `core/rag.py` (`self._query_lru`/`self._query_filtered_lru`,
  `functools.lru_cache`) est **au-dessus** du store et déjà invalidé par l'appelant :
  `index_file()` appelle `cache_clear()` après chaque `upsert`/`delete`. Ce chantier ne le
  change pas.
- Le cache mémoire **par collection**, à l'intérieur du nouveau `VectorStore` (vecteurs
  numpy + ids + métadonnées, chargés depuis SQLite) — celui que le docstring de
  `VectorStore` mentionne sans dire quand il est invalidé. C'est celui-ci qui manquait
  une réponse.

Réponse : toute `upsert()` ou `delete()` sur une `Collection` **invalide entièrement**
son cache mémoire (mis à `None`), reconstruit paresseusement à la prochaine lecture
(`count`/`get`/`query`). Pas de mise à jour incrémentale (retirer/ajouter des lignes dans
le tableau numpy en place) : à 170 chunks au total, relire toute la table SQLite d'une
collection après une écriture coûte de l'ordre du milliseconde — une mise à jour
incrémentale économiserait un temps non mesurable au prix d'un bookkeeping id→index qui
peut driver silencieusement avec le temps. L'invalidation se fait sous le même verrou que
l'écriture qui la déclenche (point 1) : aucune fenêtre où un lecteur concurrent verrait un
cache à moitié reconstruit.

**3. `get()`/`delete()` sans filtre (ni `ids` ni `where`).** Vérifié empiriquement sur le
vrai chromadb, pas supposé :

```
col.get()     → renvoie TOUT (ids=['1','2','3'] sur une collection à 3 items)
col.delete()  → ValueError: At least one of ids, where, or where_document must be
                provided in delete.
```

`collection.get()` sans filtre renvoyant tout est déjà exploité aujourd'hui —
`core/rag.py::get_indexed_files` et `core/docanalysis.py::get_loaded_docs` appellent
`get(include=["metadatas"])` sans `ids` ni `where`, précisément pour tout relire.
`collection.delete()` sans filtre lève chez chromadb ; aucun appel existant dans les
trois fichiers n'appelle `delete()` sans argument, donc rien à préserver côté
comportement observé — mais un filet de sécurité à ne pas perdre en le réécrivant :
un appel accidentel ne doit jamais vider une collection entière en silence.

Le remplacement adopte exactement cette asymétrie, parce que c'est déjà le comportement
en vigueur (l'engagement de l'étape A — « aucun changement de comportement » — s'applique
aussi au cas qui n'est exercé par aucun appelant aujourd'hui, pas seulement à ceux qui le
sont) :
- `Collection.get(ids=None, where=None, ...)` : `ids is None and where is None` → renvoie
  tout.
- `Collection.delete(ids=None, where=None)` : `ids is None and where is None` → lève
  `ValueError`, même message que chromadb.

### Étape B — Implémenter avec le backend retenu — **faite le 2026-08-13**

SQLite + numpy, selon l'interface validée à l'étape A (§0, §1) : `core/vector_store.py`
(`VectorStore`, `Collection`). Non-régression vérifiée avec `integration_vector_store.py`
(15 tests) contre le vrai `chromadb`, sur les trois profils d'appel réels — `fiches`,
`doc_analysis`, `history` — pas seulement `fiches` : mêmes documents, mêmes requêtes,
mêmes ids en sortie dans le même ordre, mêmes distances (`assertAlmostEqual`), même
comportement de `get()`/`delete()` sans filtre. Concurrence vérifiée séparément : deux
threads qui `upsert`/`query` en boucle (200 itérations) sur la même collection, contrôle
d'intégrité déterministe de l'état final (pas seulement « aucune exception ») — le verrou
tient.

Une découverte faite en écrivant ce test, à ne pas laisser dans l'angle mort : la ligne
« AND multi-clés » du tableau au §0.3 était fausse — `core/docanalysis.py:51-52` appelle
bien `get(where={"doc_id": doc_id, "chunk_index": 0})`, mais le vrai chromadb (1.5.9)
rejette tout `where` de plus d'une clé (`validate_where`), et cet appel lève déjà
aujourd'hui, absorbé par un `except Exception: pass` préexistant — `apercu` reste vide en
silence, indépendamment de ce chantier. `core/vector_store.py` reproduit ce rejet plutôt
que de le corriger : non-régression veut dire préserver le bug aussi, pas seulement les
fonctionnalités qui marchent. §0.3 et l'interface de l'étape A sont corrigées en
conséquence.

Nommé `integration_` et non `test_` comme `integration_modules_mount.py` : charge un
vrai `chromadb.PersistentClient` ET un vrai modèle `sentence-transformers` pour la
comparaison — lent, hors de `unittest discover -p 'test_*.py'`. Lancé manuellement
(`python integration_vector_store.py`), pas encore câblé dans le job `integration` de la
CI — à faire si ce fichier doit devenir un garde-fou permanent plutôt qu'une vérification
ponctuelle.

**Pas encore fait** : brancher `core/rag.py`, `core/docanalysis.py`, `core/history.py`
sur `VectorStore` — ce document valide l'implémentation, pas son intégration. `chromadb`
reste le stockage réellement utilisé par l'application tant que ce branchement n'est pas
fait (§6).

### Étape C — Migration des données existantes — **faite le 2026-08-13, sauf le délai final**

Quatre temps, dans cet ordre strict, dont **le dernier n'est pas terminable en une
session** (cf. C.4).

#### C.1 — `migrer_vectoriel.py` : 170/170 chunks

Lit `backend/chroma_db/` via le vrai `chromadb.PersistentClient` et réécrit dans
`VectorStore`. Les trois collections passent, aux effectifs exacts du recensement
du §0.4 : `fiches` 122, `doc_analysis` 34, `history` 14.

Deux décisions prises sur mesure plutôt que par défaut :

- **Les documents sont ré-embeddés, pas recopiés.** Recopier les vecteurs déjà
  calculés par chromadb semblait l'option évidente. Mesuré avant de choisir : les
  vecteurs stockés par chromadb ont une norme de **1,000000** (donc déjà
  normalisés — ce qui confirme au passage la formule `1 - cos` du §0.3 par une
  seconde voie) et coïncident avec ce que `VectorStore._embed` recalcule à
  **1,5e-8 près**, du bruit de float32. Les deux options étant équivalentes en
  sortie, le ré-embedding gagne parce qu'il n'utilise que l'API publique validée
  et surtout parce qu'il rend le store **homogène** : ce qu'il contient est ce
  qu'il écrirait lui-même en réindexant. Des vecteurs importés d'un autre moteur
  auraient changé en silence à la première ré-indexation.
- **Toutes les collections trouvées sont migrées, pas les trois attendues.** Une
  liste en dur transformerait une quatrième collection oubliée en perte de
  données silencieuse.

La destination est `resolve_vector_dir()` (`$EPURE_VECTOR_DIR`, défaut
`<backend>/vector_db`), **un dossier neuf** : la source doit rester intacte et
interrogeable, puisque C.2 fait tourner les deux côte à côte. L'ancien chemin
n'était pas surchargeable du tout — `RAGEngine` le calculait en
`dirname(config.yaml)/chroma_db`, donc un test qui aurait construit ce moteur
aurait écrit dans l'index réel.

#### C.2 — `parite_vectorielle.py` : 340 comparaisons, 0 écart

Sur les **vraies** données, pas un jeu de test — c'est ce qui distingue ce script
de `integration_vector_store.py`, qui compare les deux moteurs sur des données
fabriquées. Les requêtes sont extraites du corpus lui-même (morceaux de vrais
chunks, choisis à pas déterministe) plus quatre questions génériques en français :
une requête tirée d'un document indexé a une réponse exacte à distance ~0, et
toute divergence d'ordre saute aux yeux au lieu de se diluer. Vérifié pour chaque
collection, sur le profil d'appel de son propriétaire : `count()`, `get()` total,
contenu id par id, `query()` sans filtre, `$in` et égalité sur `source` (`fiches`),
`doc_id` (`doc_analysis`), `get(ids=…)` (`history`), et le rejet du `where` à deux
clés — **mêmes ids, dans le même ordre, mêmes documents, mêmes métadonnées, mêmes
distances à 1e-5 près, mêmes rejets.**

Deux pièges rencontrés en écrivant ce script, tous deux dans le comparateur et non
dans le moteur — à ne pas reperdre, parce qu'ils fabriquent de faux écarts
crédibles :

- **`get()` ne garantit aucun ordre de lignes, et l'ordre des CLÉS d'un dict
  diffère entre les deux stockages** (chromadb le reconstruit depuis ses colonnes,
  `VectorStore` le relit d'un JSON). Trier des métadonnées sur leur `repr`
  mélange les deux : `{'a': 1, 'b': 2}` et `{'b': 2, 'a': 1}` sont égaux pour
  Python mais donnent des chaînes différentes, donc un tri différent, donc un
  écart signalé sur des données identiques. C'est exactement ce qui s'est produit
  au premier passage — 2 « écarts » sur 340, tous deux imaginaires. D'où
  `_canonique()` : tri par `json.dumps(sort_keys=True)`, comparaison des dicts.
- **La console Windows est en cp1252** : un `→` dans le rapport fait tomber le
  script sur un `UnicodeEncodeError` — dans `migrer_vectoriel.py`, ça se produisait
  APRÈS l'écriture d'une partie des données. Les deux scripts reconfigurent donc
  leurs flux en UTF-8. Sur une plateforme primaire Windows, un script de migration
  ne doit pas pouvoir échouer sur son propre affichage.

**Ce qui n'est délibérément PAS vérifié sur les données réelles : `delete()` sans
filtre.** C'est bien un point de parité (§1, point 3), mais le vérifier consiste à
appeler une suppression non filtrée sur les 170 chunks d'Ilyann. Tout l'intérêt du
test est que l'appel lève — s'il ne levait pas, c'est-à-dire précisément dans le
cas qu'il cherche à détecter, il viderait la collection. Un test dont le mode
d'échec est de détruire les données qu'il protège n'a pas sa place ici : il est
fait sur du jeté par `integration_vector_store.py`.

#### C.3 — Branchement des trois moteurs

`core/rag.py`, `core/docanalysis.py`, `core/history.py` prennent un `VectorStore`
**injecté** ; `core/runtime.py` en construit un seul et le donne aux trois. Fin de
`rag._client`/`rag._ef` : le partage existait déjà, mais par deux attributs privés
d'un moteur qui n'avait pas vocation à être un point de partage — et brancher
`core/rag.py` seul aurait laissé les deux autres sur chromadb sans que rien ne
proteste. Les signatures d'appel n'ont pas bougé (`upsert`, `get`, `query`,
`delete` sont appelés en arguments **nommés** partout), donc aucun corps de méthode
des trois moteurs n'a changé.

**Piège majeur découvert au branchement, à ne surtout pas rejouer : l'import de
`sentence_transformers` doit rester LOCAL à `VectorStore.__init__`.** En tête de
`core/vector_store.py`, il coûte **17,4 s et tire torch — mesuré**. Or
`core/rag.py` importe ce module et `core/runtime.py` importe `core/rag.py` : cet
import se paierait donc à l'import de `core.runtime`, c'est-à-dire au démarrage
d'uvicorn, qui ne répondrait plus à rien — `/health` compris — pendant tout ce
temps. C'est très exactement l'incident que `_LazyEngine` existe pour corriger
(§3.2 de `CLAUDE.md`), réintroduit par la bande : **la paresse du proxy ne protège
que la CONSTRUCTION des moteurs, jamais l'import de leurs dépendances.** chromadb
masquait le problème en n'important `sentence_transformers` qu'à l'instanciation
de sa fonction d'embedding ; rendre l'embedding explicite rend cet import
explicite aussi. Vérifié après correction : `import core.runtime` = 3,4 s, torch
non importé, store non construit.

Vérifications du branchement : les 332 tests de `unittest discover` passent, les 15
de `integration_vector_store.py` aussi, et les trois moteurs répondent sur les
vraies données migrées via leurs API publiques (`get_indexed_files` → 10 fichiers,
`query`/`query_filtered` → contexte non vide, `get_loaded_docs` → 2 documents,
`search` → scores cohérents, `search_history` → conversations pertinentes).

`_test_env.py` détourne désormais `EPURE_VECTOR_DIR` sur un temporaire vide, pour
la première des deux raisons de `MODELS_DIR` (un test qui construirait `RAGEngine`
par accident écrirait dans le vrai index) et non pour la seconde (l'index est
dérivé et reconstructible, donc hors de `REAL_DIRS`).

#### C.4 — Le délai avant suppression — **NON TERMINÉ, et volontairement**

`backend/chroma_db/` est **intact** (24 Mo, rien n'y a été écrit ni supprimé). Une
copie de sûreté a été prise avant le premier accès en lecture.

Ce temps n'est pas une tâche mais une **attente** : il demande qu'Épure tourne
pour de bon sur le nouveau store, sur plusieurs sessions réelles — indexation de
nouvelles fiches, chargement d'un PDF dans Docs, sauvegarde et recherche de
conversations — avant que l'ancien index soit supprimé. Aucune batterie de tests ne
remplace ça : la parité prouve que les deux stockages répondent pareil aux appels
qu'on a su formuler, pas qu'aucun chemin de code encore inexploré ne casse.
Supprimer `chroma_db/` maintenant échangerait une garantie contre 24 Mo.

C'est le seul point de l'étape C qui reste ouvert, et c'est Ilyann qui le ferme.

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

Les points précédents de cette liste ont été mesurés (§0 le 2026-08-11, étape B le
2026-08-13) et sont retirés d'ici — `sqlite-vec` n'a pas de wheel `win_arm64` (écarté),
`numpy` en a une pour la version épinglée, le volume réel est de 170 chunks sur trois
collections, l'interface a été implémentée (`core/vector_store.py`) et testée en
non-régression contre le vrai chromadb sur les trois profils d'appel. Ce qui reste :

- **`core/rag.py`, `core/docanalysis.py`, `core/history.py` n'ont pas encore été
  branchés sur `core/vector_store.py`.** Ce document décrit et teste l'implémentation,
  pas son intégration — `core/runtime.py:129-136` construit toujours
  `DocAnalysisEngine`/`HistoryEngine` avec `rag._client`/`rag._ef` (chromadb), et
  `RAGEngine` construit toujours son propre `chromadb.PersistentClient`. C'est la suite
  de l'étape B, pas encore faite.
- **Temps de requête réel : mesuré, pas juste estimé — et la borne dominante n'est pas
  celle qu'on attendait.** ~47 ms/appel à `query()` sur 170 vecteurs (cache mémoire
  chaud, `all-MiniLM-L6-v2` = 384 dimensions), ~92 ms pour un cycle
  `upsert(1) + query()` cache froid. Le calcul cosinus lui-même (170 produits scalaires
  sur 384 dimensions) est bien de l'ordre du dixième de milliseconde comme prévu — c'est
  l'inférence du modèle d'embedding sur le TEXTE de la requête qui domine le temps total.
  Pas une régression : chromadb passe par exactement le même modèle
  (`SentenceTransformerEmbeddingFunction`) pour embedder ses propres requêtes, donc ce
  coût existe déjà aujourd'hui, indépendamment du stockage.
- **Migration des données réelles (étape C) et retrait de chromadb (étape D)** ne sont
  pas commencés.
