# Épure — Persistance et migration de la pile d'embedding, détail

> Extrait de `CLAUDE.md` (§3.4), déplacé ici le 2026-09-20 pour alléger
> le noyau chargé à chaque session. Contenu inchangé, seul l'emplacement a
> changé — voir `CLAUDE.md` pour le renvoi et la règle résumée.

---

### 3.4 Persistance

Aucune base de données côté application. Deux stockages :

- **Fichiers JSON** sous `backend/memory/` et `backend/history/`, via
  **`core/jsonstore.py` — IMPÉRATIF : jamais de `json.load`/`json.dump` direct.**
  Lecture en `utf-8-sig` (un BOM posé par PowerShell 5.1 rendait la mémoire de
  session invisible, puis le fichier était écrasé), écriture en `utf-8` sans BOM.
- **`core/vector_store.py`** (SQLite + numpy, cosinus par force brute) sous
  `backend/vector_db/` — `resolve_vector_dir()`, `$EPURE_VECTOR_DIR` — pour les
  trois collections vectorielles : `fiches` (RAG), `doc_analysis`, `history`.
  **IMPÉRATIF : un seul store, construit par `core/runtime.py` et INJECTÉ aux
  trois moteurs.** Ne pas en instancier un second, ni retourner aux attributs
  privés `rag._client`/`rag._ef` qui portaient ce partage avant : c'est ce qui
  rendait possible de brancher `core/rag.py` sur un nouveau stockage en laissant
  `core/docanalysis.py` et `core/history.py` sur l'ancien sans que rien ne le
  signale.

  **IMPÉRATIF : `onnxruntime` s'importe DANS `MoteurEmbedding.__init__`, jamais
  en tête de `core/embedding.py`.** La règle vient de `sentence_transformers`, qui
  coûtait 17,4 s et chargeait torch au seul import du module — comme
  `core/vector_store.py` importe la chaîne d'embedding et que `core/runtime.py`
  importe `core/vector_store.py`, un import en tête de fichier se payait au
  démarrage d'uvicorn. Le coût est tombé à **0,37 s** avec ONNX Runtime, et la
  règle ne change pas de nature pour autant : **la paresse du proxy ne couvre que
  la CONSTRUCTION des moteurs, jamais l'import de leurs dépendances.**

  **LA PILE A CHANGÉ LE 2026-08-26** — `sentence-transformers` (donc torch,
  transformers, scikit-learn, scipy) est remplacé par **`onnxruntime` +
  `core/wordpiece.py`**, sur le MÊME modèle `all-MiniLM-L6-v2`, dont le dépôt
  HuggingFace publie déjà l'export ONNX fp32.

  Ce qui a forcé la sortie n'est pas le poids mais un blocage dur, mesuré deux
  fois à huit minutes d'écart sur la machine ARM64 d'un destinataire : **Smart App
  Control y bloque durablement `sklearn/utils/_isfinite`**, que
  `sentence-transformers` importe sans condition à son chargement — pour un
  `cos_sim` que `core/vector_store.py` n'appelle jamais, puisqu'il calcule son
  cosinus en numpy. `pip install` réussissait, l'import plantait. Et
  `scikit-learn` est une dépendance **inconditionnelle de toutes** les versions de
  `sentence-transformers` (vérifié de la 2.7.0 à la 6.0.0) : changer de version
  n'était pas une issue.

  **Les vecteurs sont les mêmes, et c'est mesuré sur l'index réel** : les
  180 chunks déjà stockés dans `vector_db/` — calculés par
  `sentence-transformers` — se recalculent au **cosinus 1.000000** (écart absolu
  maximal 2,1e-07) avec le nouveau moteur. **Aucune réindexation.**

  Trois points à ne pas défaire :

  - **`onnxruntime` est déclaré en DIRECT dans `requirements.txt`.** Il arrivait
    par `faster-whisper` et `piper-tts`, tous deux retirés des paquets ARM64
    (`HORS_PAQUET_PIP_ARM64`) : sans déclaration, la pile d'embedding aurait
    dépendu de paquets vocaux absents sur l'architecture même qui a motivé le
    chantier, et le poste de dev — où la voix est installée — n'aurait rien pu
    voir. C'est mot pour mot l'incident `websockets`/`uvicorn[standard]` (§8).
    Verrouillé par `test_dependances_declarees.py`.
  - **Le tokeniseur est en Python pur**, et pas `tokenizers`. Son `.pyd` n'est pas
    signé, c'est-à-dire la catégorie exacte de binaire que Smart App Control
    bloque — et le blocage se décide **par fichier**, sur réputation : les `.pyd`
    de numpy, non signés eux aussi, passaient sur la machine ARM64 ; celui de
    scikit-learn non. On ne peut donc pas *raisonner* qu'un binaire non signé
    passera. **Et la réputation dépend du FICHIER, donc de la version** : le
    2026-09-07, sur CE poste, une wheel `numpy==2.5.3` fraîchement téléchargée
    dans un venv neuf a été bloquée (`ImportError: DLL load failed while
    importing _umath_linalg : une stratégie de contrôle d'application a bloqué ce
    fichier`) alors que la `2.5.2` installée de longue date fonctionne. Même
    paquet, même éditeur, verdict inverse — cf. la ligne SAC du §8. Les trois
    binaires d'`onnxruntime`, eux, sont signés `CN=Microsoft Corporation` —
    vérifié sur la machine cible. Parité du tokeniseur prouvée identifiant par
    identifiant sur 200 échantillons (`test_wordpiece.py`, table figée : la CI la
    tient sans installer `tokenizers`).
  - **`core/embedding_install.py` a changé de nature, pas de rôle.** Il
    n'installe plus de paquets — il télécharge les **90 Mo de poids** du modèle
    (`urllib` + sha256, `.part` puis renommage atomique), exactement comme
    `core/voice.py` fait des 76 Mo de Piper. Le contrat HTTP est inchangé :
    `GET /rag/capabilities`, `POST /rag/install`, 503 porteur d'un état,
    `EPURE_EMBEDDING_AUTOINSTALL=0` pour couper. `TAILLE_ESTIMEE_MO` est
    désormais **dérivée** des tailles déclarées (91) au lieu d'être écrite à la
    main (elle disait 2000 pour 198 Mo de wheels réelles).

  Poids mesuré du changement : **198,3 Mo de wheels → 14,1 Mo**, et
  **850,7 Mo retirés du disque** pour ~41,7 Mo ajoutés. Le contournement
  `torch --index-url download.pytorch.org` construit pour ARM64 disparaît avec
  torch.

  chromadb a été retiré le 2026-08-13 (`docs/remplacement-vectoriel.md`) : aucune
  wheel Windows ARM64, et une grappe — `grpcio`, `kubernetes`, `opentelemetry-*` —
  qu'il déclarait en dépendances directes, donc impossible à écarter tant qu'il
  restait. **Son extra, lui, avait été oublié** : `uvicorn[standard]` emportait la
  seule implémentation WebSocket de l'arbre, et tout `/ws/*` est mort dans un
  paquet livré dix jours plus tard — cf. §8 et la séquelle en fin d'étape D du
  document. Une carte de dépendances inverse doit inclure les extras.

  `backend/chroma_db/` peut encore exister sur le disque : c'est l'ancien index,
  gardé le temps d'un usage réel du nouveau (étape C.4), et non une seconde base
  vivante.

