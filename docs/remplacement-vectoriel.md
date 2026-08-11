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

## §0 — Ce qui doit être vérifié avant de choisir, pas supposé

- **`sqlite-vec` a-t-il une wheel `win_arm64` ?** C'est le candidat naturel : extension
  SQLite pour la recherche vectorielle, et `sqlite3` est déjà dans la bibliothèque
  standard de tout Python, sur toute architecture. Vérifier ses wheels publiées avant de
  s'engager dessus — même discipline que pour `grpcio` et `chromadb` ce soir.

- **`numpy` a-t-il une wheel `win_arm64` ?** Alternative plus simple encore : stocker les
  embeddings dans un fichier et faire une recherche par similarité cosinus en pur
  Python/numpy. Adapté à l'échelle réelle (documents d'une personne, pas des millions de
  vecteurs). `numpy` est plus ancien et plus largement porté que `chromadb` — probable
  qu'il ait une wheel ARM64, mais à vérifier, pas à supposer.

- **L'API exacte que `core/rag.py` utilise aujourd'hui.** Lister précisément les appels
  à `PersistentClient` : `get_or_create_collection`, `add`, `query`, `count`, `get`,
  `update`, `delete`, `list_collections`, la réouverture après redémarrage. C'est le
  contrat que le remplacement doit honorer, ni plus ni moins.

- **Le volume réel de documents** dans le `chroma_db/` actuel d'Ilyann (nombre de
  chunks). Ça dit si une recherche par force brute (comparer contre tout) reste rapide,
  ou si une structure d'index approximatif reste nécessaire.

---

## §1 — Étapes

### Étape A — Isoler l'interface

Créer une interface fine (`core/vector_store.py` ou équivalent) qui expose exactement ce
dont Épure a besoin. `core/rag.py` et tout autre appelant passent par cette interface,
jamais directement par l'implémentation. Aucun changement de comportement à cette étape
— seulement un point de bascule propre pour la suite.

### Étape B — Implémenter avec le backend retenu

Selon le résultat du §0. Un test de non-régression : les mêmes requêtes sur les mêmes
documents renvoient les mêmes résultats (ou un ordre équivalent) qu'avec `chromadb`
aujourd'hui.

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

- Disponibilité réelle des wheels `win_arm64` pour `sqlite-vec` et `numpy`.
- Le volume de documents réel à protéger dans la migration.
- Le temps de requête en recherche par force brute à l'échelle réelle, si cette option
  est retenue plutôt qu'un index.
