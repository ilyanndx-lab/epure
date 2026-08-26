# Conversations persistées et fichiers par conversation

**Objectif.** Le chat passe d'un contexte global implicite à un modèle par
conversation, comme Claude : plusieurs conversations qui survivent au
redémarrage, chacune avec son historique de messages **et** sa propre liste de
fichiers attachés. Un fichier déjà indexé se ré-attache à une nouvelle
conversation sans être ré-uploadé ; un fichier attaché ici n'apparaît pas
ailleurs.

**Validé le 2026-08-27.** Ce document est la référence du chantier ; l'ordre de
travail du §7 est celui qui est suivi, une étape par commit.

---

## §0 — L'état des lieux, mesuré

Quatre constats, tous vérifiés dans le code avant d'écrire quoi que ce soit. Les
deux premiers sont le point de départ, les deux suivants ont changé la
conception.

### 1. `fichiers_actifs` est global, unique, et relu à chaque message

`core/memory.py` le déclare dans `_CONTEXT_DEFAULT`. Il est **écrasé** à chaque
import (`modules/settings/router.py:355`,
`memory.update_context(fichiers_actifs=indexed_paths, résumé_contexte="")`) et
relu à chaque message du chat (`modules/chat/router.py:378`). Il n'existe aucun
moyen de choisir, par conversation ou par message, lesquels utiliser parmi ceux
déjà chargés.

Surface totale : **2 sites de lecture** (tous deux dans `chat/router.py`),
**4 sites d'écriture** (tous dans `settings/router.py`), **2 sites frontend**
(`ModuleBar.tsx:384` et `:576`). C'est peu, et c'est ce qui rend le retrait
praticable.

### 2. Aucune conversation n'est vivante sur le disque

`history` (`chat/router.py`, le WebSocket) est une liste Python créée à la
connexion et perdue à la déconnexion. `context_session.json` est **réinitialisé
à chaque démarrage** (`core/memory.py`, « Always reset context on startup »).
Rien ne persiste, donc rien ne peut être rouvert.

### 3. Le magasin existe déjà, et il est presque au bon format

`core/history.py` écrit **un JSON par conversation** dans `backend/history/`
(`<uuid>.json`) plus un index `conversations.json`. Ce n'est pas un magasin à
créer : c'est un magasin **écrit une seule fois, à la déconnexion**
(`chat/router.py:585`), lu uniquement par le module Historique.

Mesuré sur le poste : **18 conversations, 125 Ko au total, index de 5 091 o pour
18 entrées** — soit **283 o par entrée d'index** et **~6,7 Ko par conversation**.

Le chantier consiste donc surtout à faire passer `history/` du statut d'archive
post-mortem à celui de magasin vivant. Créer un second stockage à côté serait
exactement la faute de `modules_state.json` (CLAUDE.md §3.3).

### 4. Le frontend persiste déjà les messages — et c'est un bug silencieux

`Component.tsx:222` : `usePersistentState<Message[]>('epure.chat.messages', [])`.

Après un rechargement de page, l'écran affiche la conversation, mais le backend
a reconstruit `history = []` à la connexion WebSocket. **Le modèle ne voit plus
les tours précédents alors que l'utilisateur les a sous les yeux**, et rien ne le
signale. Le modèle par conversation corrige ça au passage : écran et modèle
auront la même source.

### 5. `ModuleBar.tsx` n'est utilisé que par le chat

Un seul site d'import (`modules/chat/Component.tsx:6`), malgré son emplacement
dans `components/`. Sa portée est donc déjà celle du chat, ce qui supprime toute
la question « que devient le panneau 📎 dans les autres modules » : il n'y est
pas.

---

## §1 — Schéma de stockage

**Un fichier JSON par conversation + un index, via `core/jsonstore.py`. Pas de
SQLite.**

### Forme du fichier `history/<uuid>.json`

Les clés existantes ne bougent pas ; cinq s'ajoutent.

```jsonc
{
  "id": "...", "date": "2026-08-26",         // existant, inchangé
  "titre": "...", "modèle": "...", "modules": ["chat"],
  "n_messages": 12,
  "messages": [{"role": "...", "content": "..."}],

  "créée":    "2026-08-26T14:03:11",         // NOUVEAU
  "modifiée": "2026-08-26T14:41:02",         // NOUVEAU
  "fichiers_attachés": ["C:/…/thermo.pdf"],  // NOUVEAU
  "résumé_contexte": "",                     // DÉPLACÉ depuis context_session.json
  "dernière_consolidation": 10               // NOUVEAU (idempotence, cf. §3)
}
```

L'index `conversations.json` garde sa forme, plus `modifiée` et `n_fichiers`.
**Jamais de messages dans l'index.**

### Pourquoi un fichier par conversation

`write_json` réécrit intégralement (sérialisation, `.tmp`, `replace`). Avec un
fichier par conversation, le coût d'un tour est **borné par la taille de cette
conversation** (~6,7 Ko mesuré). Avec un monolithe, il croît avec tout
l'historique — 125 Ko aujourd'hui, réécrits à chaque message.

Deuxième raison : `jsonstore` verrouille **par chemin résolu**. Un fichier par
conversation, et deux conversations ne se sérialisent jamais l'une derrière
l'autre. Un monolithe met tout le chat derrière un unique `RLock`.

Troisième : un fichier illisible perd une conversation, pas toutes.

### Pourquoi pas SQLite

- **Le motif d'écriture ne le demande pas** : un ajout par tour d'assistant,
  mono-utilisateur, mono-process (`--workers > 1` est déjà interdit, cf.
  l'avertissement de `jsonstore`).
- **Le volume ne le demande pas** : à 10 conversations/jour pendant un an,
  ~3 650 fichiers de ~7 Ko = 25 Mo de données utilisateur.
- **Le dépôt a déjà une histoire de stockage** (CLAUDE.md §3.4 : « Aucune base de
  données côté application », et `vector_db/` qui est du SQLite *pour les
  embeddings*). Ajouter du SQL applicatif, c'est un second idiome de persistance,
  un schéma et des migrations, pour un gain nul au volume réel.

### Où `jsonstore` atteint sa limite — et le fil de détente

Le point de coût n'est pas les conversations, c'est **l'index**, réécrit en
entier à chaque insertion : 283 o/entrée, donc 283 Ko à 1 000 conversations,
850 Ko à 3 000.

**Décision : l'index est réécrit à chaque fin de tour**, pour `modifiée` et
`n_messages`. C'est O(N) par message, la seule vraie faiblesse du schéma.
L'alternative — dériver le tri du `mtime` des fichiers et garder `n_messages`
dans l'index — fait exister **deux représentations d'une même notion**, qui
divergent. C'est littéralement la leçon de `modules_state.json`.

**Fil de détente, mesurable :** quand `conversations.json` dépasse ~1 Mo
(≈ 3 500 conversations) ou que l'ouverture de la liste devient perceptible, on
déplace **l'index seul** vers SQLite. Les fichiers de conversation restent en
JSON. C'est une migration d'un fichier, pas du modèle.

### `fsync` — décidé : oui, opt-in

`jsonstore.write_json` ne fait pas de `fsync`, et sa docstring dit pourquoi (« ce
serait payé sur chaque màj de contexte de session, très fréquente »). Tant que
`history/` était écrit une fois en fin de conversation, une coupure de courant ne
coûtait rien ; il devient le registre vivant.

**Décision : paramètre opt-in `write_json(..., fsync=False)`, utilisé
uniquement par l'écriture de conversation.** Le raisonnement retenu : une
conversation perdue est un contenu que l'utilisateur a produit — la perte est
visible et regrettable — là où `context_session.json` est un état technique
éphémère, reconstruit au démarrage. Quelques écritures par minute est un coût
négligeable devant ce qu'on protège. Aucun autre appelant ne le passe.

---

## §2 — `fichiers_actifs` : trois notions, dont deux étaient confondues

| Notion | Source de vérité | Portée | Avant |
|---|---|---|---|
| **Indexé** — le corpus | `vector_db/`, collection `fiches` → `rag.get_indexed_files()` | instance, permanent | ✅ correct |
| **Attaché** — le filtre | `fichiers_attachés` de la conversation | conversation | ❌ `context_session.fichiers_actifs`, global, écrasé, remis à zéro au démarrage |
| **Résumé de contexte** | `résumé_contexte` de la conversation | conversation | ❌ global |

**`fichiers_actifs` disparaît** — il n'est pas remplacé, sa fonction est remise
là où elle a un sens. Ce n'était jamais un corpus : c'était un filtre de session,
dans une application qui n'avait pas de session persistée.

### L'articulation RAG ↔ attachement

- Attacher **n'indexe rien** : c'est l'ajout d'un chemin à une liste, après
  vérification qu'il est dans `rag.get_indexed_files()`. C'est ce qui rend
  gratuit le ré-attachement sans ré-upload.
- Uploader continue d'indexer **globalement** (inchangé) **et** attache en plus
  à la conversation courante.
- Le chat garde ses **trois** modes, déjà présents dans le code
  (`chat/router.py:378-386`) ; seule la provenance de la liste change :

  | mode | déclencheur | appel |
  |---|---|---|
  | aucun attachement | défaut | pas de RAG |
  | attachements | `fichiers_attachés` non vide | `rag.query_filtered(texte, attachés)` — existe déjà, prend déjà une liste de chemins |
  | corpus entier | `rag_override == "all"` (existant) | `rag.query(texte)` |

  « Corpus entier » reste orthogonal à l'attachement : c'est « cherche partout »,
  pas « attache tout ».

### ⚠️ Fichiers désindexés : `présent: bool`, jamais un filtrage silencieux

À la lecture d'une conversation, `fichiers_attachés` est croisé avec
`get_indexed_files()` et renvoyé sous la forme `[{chemin, présent: bool}]`.

**Ne jamais filtrer en silence.** Un fichier attaché qui disparaît de la réponse
sans le dire, c'est le symptôme « indexé à zéro chunk, en silence » (CLAUDE.md
§3.3 bis) servi à l'envers : l'utilisateur voit la réponse changer sans
comprendre pourquoi. Ce comportement précis doit avoir son test.

### `résumé_contexte` sort de `build_system_context` — décidé

`memory.build_system_context` l'injecte aujourd'hui sous `[CONTEXTE ACTIF]`
(`memory.py:289`), or `MemoryEngine` ne connaît pas les conversations.

**Décision : il sort de `build_system_context` et est ajouté à `sys_parts` côté
chat**, où la conversation est connue. `MemoryEngine` redevient ce qu'il dit
être : le profil de l'utilisateur. C'est le bon découpage de responsabilité, et
ça change ce que voit le modèle — d'où une décision explicite plutôt qu'un effet
de bord.

---

## §3 — Endpoints et WebSocket

### Où loger les routes : dans le module **chat**

`history` est `removable: true`. Y mettre le cycle de vie des conversations
voudrait dire que désactiver le module Historique décapite le chat. Donc :

- **`/chat/conversations*`** (module chat) — le cycle de vie.
- **`/history*`** (module historique) — inchangé, devient une **vue** de
  parcours et de recherche sémantique sur le même magasin.

Un seul moteur derrière les deux : `history_engine` injecté depuis
`core.runtime` (CLAUDE.md §3.2). Pas de second magasin. Le chat est monté avec
`prefix: ""`, donc **ces routes s'écrivent préfixées à la main** (§3.3).

### Routes nouvelles

| Route | Rôle |
|---|---|
| `POST /chat/conversations` | crée — rarement appelée, la création est paresseuse (voir plus bas) |
| `GET /chat/conversations` | liste (l'index seul, jamais les messages), `?days=&limit=&offset=` |
| `GET /chat/conversations/{id}` | charge : messages + `fichiers_attachés` enrichis de `présent` |
| `PATCH /chat/conversations/{id}` | renomme — un titre généré doit pouvoir être corrigé |
| `DELETE /chat/conversations/{id}` | supprime (`delete_conversation` existe : fichier + index + vecteur) |
| `PUT /chat/conversations/{id}/fichiers` | remplace l'ensemble attaché, corps `{paths: [...]}` |

**Pourquoi `PUT` de l'ensemble** plutôt que `POST`/`DELETE` par fichier : l'UI est
déjà une liste à cases à cocher (`selectedFiles`, `ModuleBar.tsx:767`). Envoyer
l'ensemble correspond à l'interaction réelle et supprime toute question d'ordre
entre deux requêtes concurrentes. Validation : chaque chemin passe par
`resolve_user_path` **et** doit appartenir à `get_indexed_files()`, sinon 400.

### Routes modifiées et supprimées

- `POST /files/upload` et `POST /files/load` : paramètre optionnel
  `conversation_id`. Les chemins indexés sont attachés à cette conversation au
  lieu d'écraser le contexte global. Sans lui, ils indexent seulement (cas utile :
  alimenter le corpus depuis les Réglages).
- **Supprimées** : `GET /files/active`, `DELETE /files/active`. Un seul
  consommateur, livré dans le même paquet que le backend — pas de décalage de
  version à gérer.
- `GET /context` reste, amputé de deux clés.

### WebSocket : un seul `/ws/chat`, un `conversation_id` par message

Pas de socket par conversation : changer de conversation forcerait une
reconnexion, et la logique de reconnexion (`Component.tsx:265`) est déjà
délicate. La liste `history` en fermeture du handler **disparaît** ; le serveur
charge la conversation depuis le disque à chaque tour (~7 Ko, négligeable devant
l'appel au modèle), ce qui règle le constat §0.4.

- **Entrant** : le message gagne `conversation_id`. Absent → le serveur crée la
  conversation (**création paresseuse** : elle n'existe sur disque qu'au premier
  message, sinon la liste se remplit de coquilles vides) et répond
  `{"type": "conversation", "id": …}`, que le client adopte.
- **Sortant, nouveau** : `{"type": "titre", "id", "titre"}` quand le titre est
  généré, pour que la liste se mette à jour en direct.
- Le reste du protocole (`token`, `reasoning`, `stats`, `pipeline_*`, `done`,
  `error`) est **inchangé** — `test_raisonnement_stream.py` reste valide.

### Trois traitements accrochés à la déconnexion, à ré-accrocher

`WebSocketDisconnect` n'est plus une frontière signifiante
(`chat/router.py:580-589`) :

| Traitement | Avant | Après |
|---|---|---|
| **Sauvegarde** | déconnexion, si ≥ 3 messages | chaque fin de tour, dans la `transaction` du fichier |
| **Titre** (LLM local) | déconnexion | après le **premier** tour d'assistant, en `Thread`, `modele_local_defaut()` (§3.7, déjà respecté) |
| **Consolidation** | déconnexion, si ≥ 10 messages | fin de tour quand `n_messages` franchit un multiple de 10, gardé par `dernière_consolidation` |

⚠️ **`save_conversation` fait aussi un `self._col.upsert`**, donc un calcul
d'embedding sur 8 000 caractères. Le refaire à chaque tour mettrait un embedding
sur le chemin du message, ce que CLAUDE.md §8 interdit explicitement. **La
ré-indexation vectorielle suit la cadence de la consolidation** (tous les
10 messages + à la déconnexion), en thread de fond — jamais par tour.

---

## §4 — Surface frontend

| Fichier | Nature |
|---|---|
| `modules/chat/Component.tsx` | Le gros. `messages` cesse d'être un `usePersistentState` et vient de `GET /chat/conversations/{id}`. Ne restent persistés que `epure.chat.conversationId` et le brouillon `epure.chat.input`. `conversation_id` dans chaque `ws.send`. Nouveaux `data.type` : `conversation`, `titre`. |
| `modules/chat/ConversationList.tsx` | **Nouveau.** Liste, « nouvelle conversation », renommer, supprimer. Rendu **dans le module chat**, pas dans `Sidebar.tsx` — la Sidebar est la navigation entre modules ; y mettre les conversations mélangerait deux axes. |
| `components/ModuleBar.tsx` | Ciblé. `activeFiles`/`selectedFiles` (l. 274-275, 384-386, 528, 547, 576-577) repointent vers `GET/PUT /chat/conversations/{id}/fichiers`. Nouvelle prop `conversationId`. Distingue visuellement **indexé** (corpus, `/rag/files`, inchangé) et **attaché** (coché). |
| `components/ModuleBar.test.tsx` | À étendre : les nouvelles frontières `.json()` éprouvées sur la **forme des réponses d'erreur** (401 avant appairage, 404 conversation supprimée, 503 pile d'embedding), pas seulement le cas nominal. C'est la raison d'être du fichier. |
| `modules/history/Component.tsx` | « Rouvrir dans le chat » → pose `conversationId` et navigue. `onNavigate` existe déjà. |

Pas de nouveau composant partagé, pas de contexte React, pas de prop drilling à
travers `App` — le constat §0.5 l'évite.

---

## §5 — Migration

**Il n'y a presque rien à migrer, et c'est structurel.**

**`context_session.json`** : `MemoryEngine.__init__` fait
`self._write(self._context_path, _CONTEXT_DEFAULT)` inconditionnellement. Les
deux clés supprimées **ne survivent déjà à aucun redémarrage**. Aucun état
utilisateur à préserver : on les retire de `_CONTEXT_DEFAULT`. Ni l'une ni
l'autre n'est dans le `allowed` de `PATCH /context/settings`.

**Les 18 conversations existantes** : déjà au bon format, moins les clés
nouvelles. Lecture tolérante (`.get("fichiers_attachés", [])`), `créée`/`modifiée`
dérivées de `date` + `mtime` quand absentes. **Aucun script de migration, aucune
réécriture à la lecture** — un ancien fichier gagne ses clés la première fois
qu'on y écrit. C'est ce qui rend la migration sûre : rien ne touche au disque
tant que l'utilisateur ne reprend pas la conversation.

**Ce qui serait perdu sans un geste explicite** : `localStorage['epure.chat.messages']`,
c'est-à-dire ce qui est à l'écran au moment de la mise à jour. Au premier
chargement après la mise à jour, si la clé est non vide et qu'aucun
`conversationId` n'est posé, ces messages sont postés comme une nouvelle
conversation (« Conversation reprise »), son id adopté, puis la clé effacée.
C'est le seul point de migration réel du chantier.

---

## §6 — Risques et limites assumées

**Poids dans le paquet : nul.** Aucune dépendance nouvelle.

**Croissance disque** : ~7 Ko par conversation + ~1,5 Ko d'embedding dans la
collection `history`. 1 000 conversations ≈ 8 Mo. Non-sujet.

**Coût algorithmique** : l'index réécrit à chaque tour, O(N). Quantifié au §1
avec son fil de détente. Faiblesse assumée, préférée à deux sources pour l'ordre
de la liste.

### ⚠️ Deux onglets sur la même conversation — limite documentée, non résolue

Les deux WebSockets vivent dans le même process uvicorn, donc
`jsonstore.transaction` sérialise correctement les écritures : **pas de
corruption**. Mais les deux vues **divergent** — l'un ne voit pas les tours de
l'autre, et le dernier à écrire fait foi pour `n_messages`.

**Décision : documenté, pas résolu.** Mono-utilisateur rend le cas rare, et la
solution (diffuser le tour aux autres sockets ouverts sur la même conversation)
est un chantier à part qui ne se justifie pas encore. C'est écrit ici **et** dans
le code pour que ça ne soit pas redécouvert par surprise : si le symptôme
« j'ai perdu des messages en ayant deux fenêtres ouvertes » apparaît un jour,
c'est ici qu'est la réponse, et ce n'est pas un bug à chercher ailleurs.

**Tour concurrent dans une même conversation** (envoi pendant que le précédent
streame) : l'ajout du message utilisateur et celui de la réponse sont **deux
`transaction` distinctes**, et la lecture pour construire le prompt se fait
**avant** le démarrage du flux. Bénin, mais seulement si c'est écrit ainsi.

**Complexité** — ajouté : 1 fonction de chemin, 6 endpoints, ~5 méthodes sur
`HistoryEngine`, 1 composant frontend, 2 recâblages. Retiré : 2 notions globales,
2 endpoints, le bloc de sauvegarde à la déconnexion, la liste `history` en
fermeture, le stockage des messages en `localStorage`. Le chantier **supprime un
concept** (le contexte global) en même temps qu'il en ajoute un (la
conversation).

---

## §7 — Ordre de travail

Une étape par commit, vérifiée avant la suivante. Les étapes 1 à 3 sont
livrables et testables **sans changement visible à l'usage**.

| # | Étape | État |
|---|---|---|
| 1 | Prérequis : `resolve_history_dir()`, `_test_env`, `REAL_DIRS` | ✅ fait le 2026-08-27 (`4a1aa1d`) |
| 2 | `HistoryEngine` : nouvelles méthodes, lecture tolérante des anciens fichiers, `fsync` | à faire |
| 3 | Endpoints `/chat/conversations*` + `PUT …/fichiers` | à faire |
| 4 | WebSocket : `conversation_id`, chargement disque, ré-accrochage titre/consolidation/vecteur | à faire |
| 5 | Retrait de `fichiers_actifs`/`résumé_contexte` + `/files/active` | à faire |
| 6 | Frontend : `ConversationList`, recâblage `Component.tsx` et `ModuleBar.tsx`, tests vitest | à faire |
| 7 | Reprise de `epure.chat.messages` (migration one-shot) | à faire |

### Étape 1 — ce qui a été fait, et pourquoi c'était bloquant

`core/history.py` calculait ses deux chemins en **constantes de module**
(`Path(__file__).parent.parent / "history"`), le motif que CLAUDE.md §3.5
interdit. Ça n'avait jamais mordu parce que le dossier n'était écrit qu'à un
seul moment et qu'aucun test ne l'atteignait : **l'invariant tenait par
accident**. Il cesse de tenir dès que `history/` devient le magasin vivant —
sans cette étape, les tests des étapes 2 à 7 auraient écrit dans les 18 vraies
conversations du poste, et `test_zz_donnees_reelles` ne l'aurait pas vu,
`history/` n'ayant jamais figuré dans `REAL_DIRS`.

`$EPURE_HISTORY_DIR` est un temporaire **VIDE et surveillé** — le régime de
`memory/`, pas celui des caches. Vide pour le **déterminisme** du décompte (une
copie ferait dépendre `list_conversations()` de l'historique du poste : vert ici,
rouge en CI) ; surveillé parce qu'une conversation n'est reconstructible par
rien. L'en-tête de `_test_env` confondait ces deux propriétés — elles y sont
désormais distinguées bloc par bloc.

Le confinement de `_conv_path` est une **ceinture, pas un correctif** : vérifié
avant de l'écrire, aucune traversée n'est atteignable aujourd'hui (un paramètre
de chemin Starlette n'accepte pas de `/`, même percent-encodé — `..%2F..%2Fx`
→ 404 ; et `C:evil` est absorbé en `<history>/evil.json`). Elle prend son sens à
l'étape 3, où un `PUT` écrira sous un identifiant fourni par le client.
