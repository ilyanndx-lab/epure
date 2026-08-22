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
| `core/docanalysis.py` (« doc_analysis ») | égalité, `$in`, et un AND à deux clés — qui ratait silencieusement, **corrigé depuis** (étape F) | `[]`, `["documents"]`, `["documents","metadatas"]`, `["metadatas"]` | `where` |
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

### Étape D — Retirer chromadb et sa grappe — **faite le 2026-08-13**

**Mesuré avant de retirer quoi que ce soit**, par carte de dépendances inverse sur
l'environnement installé (qui déclare quoi comme dépendance, marqueurs évalués) —
parce que « ces paquets ne servaient qu'à chromadb » était jusque-là une conviction,
pas un fait :

| Paquet | Réclamé par | Sort du paquet ? |
|---|---|---|
| `grpcio` | `chromadb`, `grpcio-status`, `otlp-proto-grpc` | oui |
| `kubernetes` | `chromadb` | oui |
| `opentelemetry-exporter-otlp-proto-grpc` | `chromadb` | oui |
| `googleapis-common-protos` | `google-api-core`, `grpcio-status`, `otlp-proto-grpc` | oui |
| `pypika`, `mmh3` | `chromadb` | oui |
| `opentelemetry-api`/`-sdk` | `chromadb` + la chaîne OTLP | oui |
| **`onnxruntime`** | `chromadb`, **`faster-whisper`, `piper-tts`** | **NON — reste** |

`onnxruntime` est la raison d'être de ce tableau : il était dans la grappe de
chromadb et on aurait pu le retirer avec le reste, mais la voix en dépend
aussi. Une purge « par ressemblance » aurait cassé la synthèse vocale dans le
paquet, sans erreur au build.

Retirés en conséquence :

- `chromadb==1.5.9` de `backend/requirements.txt` ;
- `kubernetes` de `PURGE_SITE_PACKAGES` ;
- **`PURGE_DISTRIBUTIONS` en entier**, avec ses trois fonctions
  (`_purger_distribution`, `_dist_info_pour`, `_normaliser_nom_distribution`) —
  la purge par lecture des `RECORD` de `.dist-info` n'existait que parce que
  `grpcio` pose `grpc/` et l'exporter OTLP un espace de noms partagé ;
- **`SITECUSTOMIZE` et `poser_sitecustomize`** — le stub d'`OTLPSpanExporter` sans
  lequel `import chromadb` cassait une fois ses propres dépendances purgées ;
- dans `test_paquet.py` : `PurgeDistributionsTest` et `SitecustomizeTest`
  (supprimées, leur cible n'existe plus) et trois tests d'`ExigencesTest`. Une
  `PurgeSitePackagesTest` les remplace pour le seul mécanisme qui subsiste.

Le script de paquet **rétrécit** — trois couches de contournement en moins, empilées
au fil d'une soirée où chacune exposait la suivante. C'est la démonstration que le
remplacement traitait la cause et non le symptôme.

**Deux ajouts, pas seulement des retraits** :

- `vector_db` entre dans `EXCLUS_RACINE`. **Le point le plus dangereux de l'étape** :
  le nouveau store contient le TEXTE des fiches et des PDF indexés, pas seulement des
  vecteurs. Remplacer un stockage sans étendre la liste d'exclusion aurait livré au
  destinataire les documents d'Ilyann, dans un fichier au nom neuf que personne ne
  surveillait, sans la moindre erreur au build. `chroma_db` y reste tant que l'ancien
  index existe (C.4) : une entrée d'exclusion sans dossier correspondant ne coûte
  rien, contrairement à une entrée de purge.
- Deux tests neufs : l'un vérifie que le retrait se voit aux deux bouts (plus de
  `chromadb` actif dans `requirements.txt`, plus aucune entrée de purge qui le vise,
  `PURGE_DISTRIBUTIONS`/`SITECUSTOMIZE` absents du module) ; l'autre que
  `vector_db` et `chroma_db` sont bien exclus du paquet.

Répercuté ailleurs : `ci.yml` (le job rapide installe `numpy` au lieu de `chromadb` —
c'est `core/vector_store.py`, importé par `core/rag.py`, qui en dépend, et il
n'importe `sentence_transformers` que dans son `__init__`, donc torch reste hors du
job), `docker-compose.yml` (volume `vector_db/`), `CLAUDE.md` §3.4 et §9.

**Un test a échoué en étant écrit, et le détail vaut d'être noté** : les assertions
de `PurgeSitePackagesTest` étaient placées APRÈS la sortie du bloc
`with TemporaryDirectory()`. Le dossier entier étant alors supprimé, l'assertion
« `pip/` a bien disparu » passait par vacuité et celle sur ce qui devait SURVIVRE
échouait. Un test de suppression écrit après le nettoyage ne teste rien — il ne l'a
signalé ici que parce qu'il vérifiait aussi une survivance.

**Non fait, et volontairement reporté à l'étape E : `tools/contraintes-paquet.txt`.**
Il liste encore `chromadb`, `grpcio`, `kubernetes` et toute la chaîne OTLP. Son
en-tête dit comment le régénérer — construire un paquet, lire `python.gel` dans le
`PAQUET.json` produit — et interdit de l'éditer ligne à ligne, parce que c'est un
`pip freeze` et non une liste de souhaits. Le corriger à la main donnerait un fichier
qui décrit un environnement que personne n'a installé. Sans effet en attendant : un
fichier de contraintes ne fait qu'épingler des versions **si** le paquet est
installé, il n'en installe aucun — plus rien ne tire chromadb, donc plus rien n'y
touche.

### Étape E — Reconstruire et remesurer — **x64 fait le 2026-08-13 ; ARM64 : voir ci-dessous**

#### Le paquet x64 : 132,2 Mo, contre 159,4 Mo

Construit avec `--sans-contraintes` pour régénérer le gel, comme le prescrit l'en-tête
de `tools/contraintes-paquet.txt`. **−27,2 Mo (−17 %)**, et surtout **52 paquets
installés contre 101** : 49 partis, aucun arrivé. La moitié de l'arbre transitif
disparaissait avec une seule dépendance directe.

Purges restantes : `pip` (10,1 Mo) et `setuptools` (5,1 Mo). Plus rien d'autre — les
entrées `kubernetes`, `grpcio`, `opentelemetry-exporter-otlp-proto-grpc` et
`googleapis-common-protos` n'ont pas été « désactivées », elles n'ont plus d'objet.

Vérifié dans l'interpréteur embarqué du paquet, pas seulement au build : `import main`
passe en 13,8 s, monte 71 routes et 4 modules, sert l'interface statique — **sans
torch et sans chromadb chargés**, avec `core.vector_store` importable et numpy 2.5.2
présent. Le gel confirme le tableau de l'étape D par une seconde voie : ni `chromadb`,
ni `grpcio`, ni `kubernetes`, ni `opentelemetry-*`, ni `pypika`, ni `mmh3` — et
`onnxruntime==1.28.0` toujours là, comme prévu, pour la voix.

#### ARM64 : l'attente de cette étape était fausse, et il vaut mieux le savoir ici

Ce document annonçait un essai ARM64 « avec l'attente que ça passe directement
puisque les wheels auront été vérifiées au §0 ». **Cette attente ne tient pas**, et
la vérifier coûtait une requête PyPI par paquet plutôt qu'un déplacement jusqu'à une
machine ARM64. Les 52 paquets du nouveau gel, classés par ce qu'ils publient :

| | Nombre | Verdict ARM64 |
|---|---|---|
| Wheel universelle (`py3-none-any`) | 38 | indifférents à l'architecture |
| Wheel `win_arm64` publiée | 11 | `numpy`, `pandas`, `onnxruntime`, `lxml`, `pillow`, `tokenizers`, `pydantic_core`, `PyYAML`, `av`, `hf-xet`, `jiter` |
| **Sans wheel `win_arm64`** | **3** | **détaillés ci-dessous** |

**Le mur chromadb est bien tombé** : 49 des 52 paquets restants sont propres pour
ARM64, et tout ce que ce chantier a retiré l'était par construction. Mais trois
paquets restent, et ils n'ont rien à voir avec le stockage vectoriel — ils étaient
derrière le mur, masqués par lui :

- **`ctranslate2==4.8.1` — blocage dur.** Aucune wheel `win_arm64` **et aucune sdist
  du tout** : `pip` ne peut pas l'installer sur cette architecture, même avec un
  toolchain complet, faute de source publiée. C'est une dépendance de
  `faster-whisper` (transcription vocale). Rien ne se contourne côté build.
- **`piper-tts==1.4.2`** — extension compilée (`cp39-abi3-win_amd64`), mais une sdist
  existe : constructible en théorie, au prix d'un toolchain C++ sur la machine cible,
  c'est-à-dire exactement le piège que ce chantier voulait quitter.
- **`watchdog==6.0.0`** — cas le plus favorable, à ne pas confondre avec les deux
  autres : ses wheels Windows sont `py3-none-win32/win_amd64/win_ia64`, donc **du
  Python pur simplement étiqueté par plateforme** (l'extension C de watchdog n'existe
  que pour FSEvents, sur macOS). Sur ARM64, `pip` ne fera correspondre aucune wheel
  et tombera sur la sdist, qui devrait s'installer sans rien compiler. À confirmer
  empiriquement, pas à déclarer acquis.

**Correction du 2026-08-13, même jour — cette section a d'abord affirmé le contraire.**
Il y était écrit que « `torch` ne publie aucune wheel `win_arm64` », et donc que la
recherche documentaire serait bloquée à l'usage sur ARM64. **C'est faux, et l'erreur
tient à la source interrogée** : PyPI ne publie effectivement que des wheels
`win_amd64` pour torch, mais torch n'a jamais distribué ses variantes par PyPI seul.
Son index — `https://download.pytorch.org/whl/cpu` — publie bien
`torch-2.13.0+cpu-cp312-cp312-win_arm64.whl`, ainsi que les mêmes pour 2.7 → 2.13 en
cp311/312/313 : 21 wheels `win_arm64` sur les 1137 listées. Conclure « absent de PyPI »
donc « inexistant » était un raccourci — exactement le genre de vérification à moitié
faite que le §0 de ce document a été écrit pour éviter.

Aucune version de torch n'est d'ailleurs **épinglée** nulle part : il n'apparaît ni
dans `backend/requirements.txt` (il arrive en transitif par `sentence-transformers`)
ni dans `tools/contraintes-paquet.txt` (dont il est absent puisque
`sentence-transformers` est exclu de l'installation du paquet). La version concernée
est celle que `pip` résout au premier usage — `torch 2.13.0` aujourd'hui, qui a sa
wheel ARM64 en cp312, le Python embarqué.

Conséquence appliquée : **sur ARM64, le téléchargement à la demande vise l'index
PyTorch et non l'index par défaut**, et AVANT `sentence-transformers`, sans quoi torch
se résout depuis PyPI où il n'y a rien pour cette architecture :

```
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

La consigne est portée par `backend/requirements.txt`, et pas seulement par
`tools/faire_paquet.py` : `tools/` ne part jamais dans le paquet, alors que
`requirements.txt` y est livré (vérifié dans l'archive) — c'est le seul endroit où le
destinataire la lira. Verrouillé par `test_paquet.py`.

**Et le reste de la grappe du premier usage a été vérifié dans la foulée**, pour ne pas
refaire la même moitié de travail : `transformers`, `sympy`, `networkx`, `joblib`,
`Jinja2`, `huggingface-hub`, `sentence-transformers` sont universels ;
`scikit-learn`, `scipy`, `safetensors`, `regex`, `tokenizers`, `pillow` publient des
wheels `win_arm64`. **torch était le seul manquant.** Le RAG — l'objet même de ce
chantier — est donc entièrement viable sur Windows ARM64.

**Conséquence pratique : l'essai ARM64 ne doit pas être tenté « en s'attendant à ce
que ça passe ».** Il échouera, sur `ctranslate2` d'abord — mais le périmètre de l'échec
est désormais cerné, et il est plus étroit qu'annoncé :

- **le stockage vectoriel est réglé** (c'était l'objet du chantier) ;
- **le RAG à l'usage est réglé aussi**, torch compris, via l'index PyTorch ;
- **seule la voix bloque** : `ctranslate2` (transcription) et `piper-tts` (synthèse).

Le prochain chantier n'est donc pas vectoriel, il est vocal — et il pose la même
question qu'au §0 pour chromadb : `faster-whisper`/`piper-tts` sont-ils proportionnés à
ce qu'on leur demande, ou peut-on transcrire et synthétiser autrement ? À noter que la
piste de l'index alternatif, qui a sauvé torch, n'existe pas pour `ctranslate2` : il ne
publie **aucune sdist**, nulle part. La différence avec la soirée d'origine, c'est
qu'on le sait avant de construire, pas en marchant dessus.

#### La voix est déclarée indisponible sur ARM64 — décision du 2026-08-22

Le « prochain chantier vocal » annoncé ci-dessus n'aura pas lieu sous cette forme.
**La voix est déclarée indisponible sur Windows ARM64, et aucune compilation n'est
tentée sur la machine cible.** Ce n'est pas un report : c'est une réponse à la
question que le paragraphe précédent posait.

Pourquoi ce sens-là :

- Le blocage est à l'**installation**, pas à l'usage. `ctranslate2` ne publie ni
  wheel `win_arm64` ni sdist : `pip install -r requirements.txt` échoue avant que
  le backend ait la moindre chance de démarrer. Ce n'est donc pas une capacité
  dégradée qu'on pourrait laisser échouer proprement au clic — c'est un paquet
  qui empêche le paquet entier de s'installer.
- La seule voie restante pour `piper-tts` serait sa sdist, donc **un toolchain
  C++ à installer sur la machine du destinataire**. C'est exactement ce que le §0
  de ce document a refusé pour `chromadb` (« retombe dans le piège que ce chantier
  cherche à éviter ») et pour `sqlite-vec` au §0.1. Refuser là et accepter ici
  aurait été incohérent — et l'incohérence se paierait chez quelqu'un d'autre, sur
  une machine qu'on ne peut pas déboguer.
- `ctranslate2` n'a pas d'issue du tout, même en acceptant de compiler. Une
  décision qui ne sauve qu'une capacité sur deux ne vaut pas son prix.

Ce qui a été fait pour que ce soit une absence propre et non une amputation :

| Couche | Traitement |
|---|---|
| **Paquet** | `tools/faire_paquet.py --arch arm64` retire `faster-whisper` et `piper-tts` des exigences installées (`HORS_PAQUET_PIP_ARM64`). Le x64 n'est pas touché. `--arch` choisit aussi le zip embeddable (`embed-arm64`), et `PAQUET.json` porte `arch` et `voix`. |
| **Backend** | `WhisperEngine`/`PiperEngine` lèvent `VoiceModelUnavailable` quand le **paquet** manque, pas seulement le modèle → 503 lisible. `PiperEngine` vérifie désormais le paquet **avant** de télécharger les 76 Mo du modèle : l'ordre inverse tirait 76 Mo pour un moteur qui ne peut pas se construire. |
| **Statut** | `core/voice.py::capacites_vocales()` détecte la présence des paquets par `importlib.util.find_spec` — jamais par un import, qui chargerait onnxruntime et des bibliothèques natives pour répondre à une question de disque. Exposé par `GET /voice/capabilities`. |
| **Interface** | Micro et lecture à voix haute sont **masqués** quand la capacité est absente (`frontend/src/voix.ts`), comme un fournisseur cloud sans clé. Un contrôle qu'on sait voué au 503 ne s'affiche pas. |

Périmètre inchangé pour tout le reste : sur ARM64, RAG, stockage vectoriel, chat,
modules et Atelier fonctionnent. **Seule la voix manque, et elle se voit manquer.**

#### Ce qui reste ouvert dans l'étape E

- L'essai de construction ARM64 lui-même, sur la machine de sandr ou une autre.
  L'analyse ci-dessus disait qu'il échouerait sur `ctranslate2` ; avec
  `--arch arm64` cette cause-là est retirée de l'installation, donc l'essai
  redevient informatif — il dira ce qui bloque *après* la voix, notamment si
  `watchdog` passe bien par sa sdist. Il demande toujours du matériel ARM64.
- La suite de tests complète passe (328 tests, plus les 15 de
  `integration_vector_store.py`), mais elle tourne en 3.14 sur le poste et en 3.12 en
  CI ; le paquet, lui, embarque 3.12 et n'exécute pas la suite.


### Étape F — Le AND multi-clés, une fois le périmètre clos — **faite le 2026-08-14**

Post-chantier, et c'est la raison d'être de cette étape : tant que `chromadb` était
là, reproduire son rejet des `where` à plusieurs clés était la seule position
tenable (§5 : remplacement de stockage, pas amélioration). `chromadb` retiré à
l'étape D, `core/vector_store.py` est du code d'Épure, et faire échouer une requête
qu'il sait satisfaire n'avait plus de justification — seulement une origine.

**`_valider_where` n'impose plus qu'une chose : un `where` non vide.**
`_matches` combine désormais toutes les clés par ET (égalité ou `$in`, librement
mélangés). Rien à changer dans `get()`/`query()`/`delete()` : les trois partagent
ces deux fonctions, le mécanisme n'était pas dupliqué.

`get()`/`delete()` **sans** `where` gardent leur sémantique propre (tout lire / lever) :
c'est le `where` **vide** qui est refusé, parce qu'il signale un appelant qui croit
filtrer alors qu'il ne filtre rien.

**Le `except Exception: pass` de `core/docanalysis.py` est levé aussi**, et ça compte
autant que le reste : il n'avalait pas seulement le `ValueError` attendu, il avalait
*tout*. L'exception est toujours attrapée — un aperçu manquant ne doit pas faire
échouer le chargement d'un document qui, lui, a réussi — mais elle est journalisée,
et une collection vide de chunk 0 laisse un `warning`. Après correction, une erreur à
cet endroit n'est plus un état connu.

**Preuve empirique, et contre-épreuve.** `test_docanalysis_apercu.py` (9 tests) fait
tourner le vrai `load_document_streaming` et lit l'aperçu de l'événement `done` :
non vide, et issu du chunk 0, pas d'un autre. Le filtre est testé séparément — ET et
non OU, clés suivantes non ignorées, `$in` combinable avec une égalité. Puis la
contre-épreuve, sans laquelle un test de non-régression ne prouve rien : en
réinjectant la règle de chromadb (`len(where) != 1`), le test d'aperçu **échoue avec
`apercu == ''`** — exactement le symptôme d'origine. Deux assertions ont d'ailleurs
dû être renforcées parce qu'elles passaient par vacuité sur une chaîne vide.

Aucun modèle d'embedding n'est chargé : le test double la collection et délègue le
filtrage aux **vraies** fonctions du store. Un `VectorStore` réel construirait
`sentence-transformers` (17 s, torch), que le job rapide de la CI n'installe pas —
c'est ce qui distingue ce `test_` de `integration_vector_store.py`.

`integration_vector_store.py` est mis à jour plutôt que vidé : son test affirmait que
les deux moteurs devaient lever à l'identique. Il documente maintenant la divergence
assumée — chromadb lève, `VectorStore` renvoie le bon chunk — pour qu'on ne la
reprenne pas un jour pour une régression.

---

## §5 — Hors périmètre

**Améliorer la qualité de la recherche documentaire.** C'est un remplacement de
stockage, pas une refonte du RAG. Le comportement de recherche doit rester équivalent,
pas meilleur — une amélioration est un chantier séparé, à ne pas mélanger avec celui-ci.

> **Levé le 2026-08-14, pour un point précis et une fois le remplacement terminé**
> (étape F) : le AND multi-clés de `where`. Ce n'est pas une entorse rétroactive —
> la règle ci-dessus a tenu pendant tout le chantier, et c'est elle qui a fait
> reproduire le bug plutôt que le corriger en chemin. Elle a cessé de s'appliquer
> quand `chromadb` a disparu : il n'y avait plus de « comportement d'origine » à
> préserver, seulement notre propre code qui refusait ce qu'il savait faire.

---

## §6 — Ce qui n'a pas été vérifié

Les points mesurés depuis (§0 le 2026-08-11, étapes B à E le 2026-08-13) sont retirés
d'ici : `sqlite-vec` écarté faute de wheel `win_arm64`, volume réel de 170 chunks,
interface implémentée et testée en non-régression, données migrées et parité confirmée
sur les vraies données, trois moteurs branchés, chromadb et sa grappe retirés, paquet
x64 reconstruit à 132,2 Mo. Ce qui reste ouvert :

- **Le délai d'usage réel avant suppression de `backend/chroma_db/` (étape C.4).**
  C'est le seul point de l'étape C non fermé, et c'est une attente, pas une tâche :
  il demande qu'Épure tourne pour de bon sur le nouveau store — indexation de fiches,
  chargement d'un PDF, sauvegarde et recherche de conversations — sur plusieurs
  sessions. La parité prouve que les deux stockages répondent pareil aux appels qu'on
  a su formuler, pas qu'aucun chemin de code encore inexploré ne casse.
- **La construction ARM64 (étape E), et elle échouera** : `ctranslate2` (via
  `faster-whisper`) n'a ni wheel `win_arm64` ni sdist, donc `pip` ne peut pas
  l'installer, même avec un toolchain. `piper-tts` demanderait une compilation C++.
  `watchdog` devrait passer par sa sdist en Python pur, à confirmer. **Le mur suivant
  est la pile vocale, et elle seule** — mesuré paquet par paquet, pas découvert en
  construisant. `torch` a d'abord été compté dans ce lot par erreur : il n'a pas de
  wheel `win_arm64` sur PyPI mais en a une sur l'index PyTorch, et le RAG est viable
  sur ARM64 (cf. étape E).
- **`integration_vector_store.py` n'est pas câblé dans la CI**, et ne peut plus l'être
  tel quel : il compare au vrai `chromadb`, qui n'est plus une dépendance du projet.
  Depuis l'étape F, il porte en plus la seule divergence assumée entre les deux
  moteurs (le AND multi-clés) — dont la partie qui compte est, elle, couverte par
  `test_docanalysis_apercu.py`, qui tourne en CI sans chromadb ni torch.
  Soit il devient un test manuel assumé (avec `pip install chromadb`), soit il perd son
  côté comparatif pour ne garder que les invariants de `VectorStore` seul. Non tranché.
- **Temps de requête : mesuré (~47 ms/appel `query()` cache chaud, ~92 ms pour
  `upsert(1) + query()` cache froid), mais jamais sous charge réelle prolongée.** La
  borne dominante n'est pas le cosinus (170 produits scalaires sur 384 dimensions, de
  l'ordre du dixième de milliseconde) mais l'inférence du modèle d'embedding sur le
  texte de la requête — un coût qui existait déjà avec chromadb, qui passait par le
  même modèle.
- **La régression de concurrence assumée à l'étape A n'a pas été observée en usage
  réel** : un seul verrou sérialise désormais les trois collections, là où chromadb
  les traitait indépendamment. Sans conséquence mesurable à 170 chunks et un
  utilisateur, mais c'est une limite qui ne se verra qu'en grandissant.
