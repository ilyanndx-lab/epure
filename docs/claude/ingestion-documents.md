# Épure — Ingestion de documents et vision (import + chat), journal détaillé

> Extrait de `CLAUDE.md` (§3.3 bis et §3.3 ter), déplacé ici le 2026-09-20 pour alléger
> le noyau chargé à chaque session. Contenu inchangé, seul l'emplacement a
> changé — voir `CLAUDE.md` pour le renvoi et la règle résumée.

---

### 3.3 bis Ingestion des documents — **deux chemins, pas un**

C'est la confusion la plus facile à faire, et elle mène à croire un format
supporté là où il ne l'est pas :

| chemin | code | formats | ce qu'il produit |
|---|---|---|---|
| **RAG / fiches** | `RAGEngine._extract_text_from_path` | les 12 de `SUPPORTED_EXTENSIONS` | des chunks de texte pour la recherche |
| **module Docs** | `docanalysis.load_document_streaming` | **PDF seulement** | un document paginé (`n_pages`, aperçu, résumé) |

Le second appelle `pypdf.PdfReader` sans condition **parce qu'il compte des
pages** : l'étendre n'est pas ajouter une branche, c'est décider ce que « page »
veut dire pour un classeur. Son `accept` côté frontend annonçait dix types pour
n'en accepter qu'un ; ramené à `.pdf` le 2026-08-24.

**IMPÉRATIF — une seule liste d'extensions.** `SUPPORTED_EXTENSIONS`
(`core/rag.py`) est la source ; `modules/settings/router.py:_SUPPORTED_EXT`
l'importe, le message du 400 de l'upload en est dérivé, et le frontend en tient
un miroir unique (`EXTENSIONS_ACCEPTEES` dans `ModuleBar.tsx`, d'où sortent
`accept` **et** le filtre de `uploadFiles`). Il y en avait trois côté backend et
deux côté frontend : l'oubli le plus probable produit le pire symptôme — un
fichier accepté que le moteur ne sait pas lire s'indexe **à zéro chunk, en
silence**.

`.pptx`/`.xlsx` ajoutés le 2026-08-24 (python-pptx, openpyxl : `py3-none-any`,
aucune extension compilée, +6,6 Mo, zéro transitif nouveau). `.docx` était déjà
lu — ce qui manquait était le contenu de ses **tableaux**, que `doc.paragraphs`
n'inclut pas. Pas de conversion externe : ni LibreOffice, ni Office, ni binaire
appelé. Et **pas** de `.doc`/`.ppt`/`.xls` — aucune des trois bibliothèques ne lit
l'OOXML pré-2007, les accepter donnerait une erreur à l'ouverture au lieu d'un
refus à l'upload.

Convention des extracteurs : paquet **absent** → avertissement + chaîne vide
(dégradation, le paquet livré peut l'avoir perdu) ; fichier **illisible** →
l'exception remonte, comme `.pdf` depuis toujours. Ne pas confondre les deux :
l'un est une installation incomplète, l'autre un mauvais fichier.

**Les images (`.png`/`.jpg`/`.jpeg`/`.webp`) ont, depuis le 2026-09-01, un
troisième comportement — et lui non plus n'est pas uniforme entre les deux
usages de `_extract_text_from_path` :**

- **`index_file`** (l'indexation RAG) bascule vers `RAGEngine._texte_image`,
  qui appelle un modèle vision (`LLMEngine.describe_image`, choisi par
  `core.models.premier_modele_vision_disponible()` — FLM d'abord, sinon
  l'Ollama de `config.yaml:vision.ollama_model`, défaut `moondream`) pour
  produire une description ET transcrire le texte visible, remplaçant le
  placeholder muet d'avant. **`index_file` rend ce texte** (`Optional[str]`,
  `None` si rien n'a été indexé) — voir le piège ci-dessous, c'est précisément
  ce que son absence a cassé une première fois.
- **`read_file_text`/`read_pdf_text`** (lecture ad hoc d'un fichier — `/skills/
  résumé`, l'aperçu d'upload de Réglages) restent sur `_extract_text_from_path`
  et son placeholder statique. **C'est un choix de périmètre assumé, pas un
  oubli** : seule l'indexation appelle un modèle vision.

**IMPÉRATIF — ne jamais réextraire un fichier déjà passé par `index_file` :
réutiliser sa valeur de retour.** Payé une fois : `_stream_load_sse`
(`modules/settings/router.py`) appelait `rag.index_file(path)` PUIS
`RAGEngine.read_file_text(path)` séparément pour construire le résumé affiché
à l'import — un second appel qui passe par `_extract_text_from_path`,
**statique**, donc qui ne voit jamais `_texte_image` ni le modèle vision. Le
résumé d'une image importée disait donc systématiquement « je n'ai pas accès à
l'image », alors que l'index, juste au-dessus dans la même boucle, avait la
vraie description. Corrigé en faisant rendre à `index_file` le texte qu'il a
réellement indexé ; `_stream_load_sse` le réutilise (`text or ""` pour le cas
`None`, sans changer le comportement d'attachement d'avant). Pour les formats
non-image, le bug ne changeait pas le RÉSULTAT (même texte des deux côtés) mais
payait une relecture/reparsing en double à chaque import — éliminé par le même
correctif. Verrouillé par `test_vision_images.py`.

Dégradation à trois niveaux, même esprit que les extracteurs ci-dessus mais un
cran de plus : aucun `llm` injecté (scripts, tests légers), aucun modèle
vision disponible, ou l'appel échoue (timeout, réponse vide) → le placeholder,
jamais une exception. Verrouillé par `test_vision_images.py`.

**IMPÉRATIF — les TROIS cas dégradés SANS exception sont logués, pas seulement
le `except`.** Angle mort trouvé en usage réel : `describe_image` peut réussir
(pas de timeout, pas d'erreur) tout en rendant une chaîne vide — observé sur
`flm:qwen3vl-it:4b`, sans qu'aucune trace n'indique pourquoi. `_texte_image`
logue désormais ce cas, celui d'aucun modèle vision disponible, et celui
d'aucun `llm` injecté du tout (`self._llm is None`) — les trois étaient
silencieux (`logger.warning`, pas `exception` : ce n'est pas une erreur).

**La branche `self._llm is None` a été la dernière trouvée, et c'est elle qui
a fini par expliquer un cas réel** : les deux premiers logs ajoutés ne se
déclenchaient JAMAIS chez un utilisateur, sur plusieurs fichiers — par
élimination, c'est forcément celle-ci qui tournait. En production il n'y a
QU'UN SEUL site de construction (`core/runtime.py:157`,
`RAGEngine(store=vector_store, llm=llm)`), donc si ce log apparaît : soit
`core/runtime.py` sur le disque est resté sur une version d'avant ce
paramètre, soit le process tourne depuis avant la mise à jour — `_LazyEngine`
construit le moteur **une seule fois** et le garde pour toute la durée du
process, donc un `git pull` seul ne suffit pas, il faut redémarrer.

`describe_image` va plus loin et logue le diagnostic BRUT quand le contenu est
vide, pour la prochaine occurrence :

- Ollama : `done_reason` et `eval_count` de la réponse ;
- openai (flm) : `finish_reason`, `refusal` (champ du schéma OpenAI pour un
  refus de contenu — jamais observé sur flm, mais gratuit à logger),
  `model_extra` (un champ non modélisé par le SDK, comme `reasoning_content`
  sur le chemin streaming, s'y retrouverait) et `usage`.

**`finish_reason`/`done_reason` NE DISTINGUE PAS ce cas d'un succès — mesuré,
pas supposé.** Sur `moondream` (avant que `_VISION_PROMPT` soit raccourci,
prompt trop long) : `done_reason='stop'`, `eval_count=1`, réponse en 0,08 s,
contenu vide. Le modèle a émis l'EOS comme PREMIER token — `"stop"` couvre
donc aussi bien un succès qu'un contenu vide. **Le vrai discriminant est le
nombre de tokens produits** (`eval_count`/`usage.completion_tokens` proche de
0-1), pas `finish_reason`. C'est pour ça que les deux sont loggués ensemble.

**IMPÉRATIF — la correspondance avec un modèle Ollama installé tolère
l'absence de tag, jamais une égalité stricte.** Bug confirmé en production,
distinct de celui ci-dessus : `config.yaml:vision.ollama_model` porte
`moondream` SANS tag, mais `get_ollama_installed()` restitue les noms tels
qu'Ollama les expose via `/api/tags` — AVEC tag, `moondream:latest`. Une
égalité stricte (`ollama_model in ollama_installed`) ne les faisait donc
jamais coïncider : `premier_modele_vision_disponible()` rendait `None` même
Ollama joignable et le modèle installé, et le seul log visible était « aucun
modèle vision disponible » — indiscernable d'une vraie absence d'installation.
`core.models._match_ollama_model()` corrige ça : égalité stricte d'abord, puis
repli sur le nom de BASE (partie avant `:`) des deux côtés. Rend le nom
RÉELLEMENT installé (avec son tag), pas la valeur brute de `config.yaml` :
c'est ce nom qui doit partir dans `describe_image`, pas celui de la config —
reste correct si le tag installé change un jour. Verrouillé par
`MatchOllamaModelTest` et deux cas dans `PremierModeleVisionDisponibleTest`
(`test_vision_images.py`).

**Non résolu à ce jour, spécifiquement sur `flm:qwen3vl-it:4b`** :
reproduction tentée sans succès le 2026-09-01 (image blanche/noire/bruitée/
RGBA/panoramique 4000×200, `.webp`, `.jpeg`, `think=True` forcé — tout est
rendu avec un contenu correct et un nombre de tokens normal). La cause précise
chez flm reste ouverte ; ces logs, avec le nombre de tokens qu'ils portent,
sont ce qui la révélera à la prochaine occurrence réelle.

**Coût mesuré, à garder en tête** : `index_file` est appelé par fichier depuis
le flux de chargement du chat (`_stream_load_sse`) — attacher une image à une
conversation bloque donc ce flux le temps de l'appel vision. Mesuré sur ce
poste : ~2 s pour Ollama/`moondream` une fois le modèle chargé (25 s au premier
appel après le pull). Pour `flm:qwen3vl-it:4b`, **6 à 19 s sur la MÊME image**
rejouée trois fois (12,0 s / 6,2 s / 18,8 s) — la variance vient du run-to-run
sur le NPU, pas de la complexité de l'image ; 26 s mesuré au tout premier appel
après chargement du modèle. Ne pas lire une relation « image simple = rapide,
image chargée = lent » dans ces chiffres, il n'y en a pas.

**`_stream_load_sse` répond à ce coût par deux ajouts, sans toucher à
l'indexation elle-même** (`backend/modules/settings/router.py`) : un événement
SSE `{"type": "progress", "fichier", "index", "total"}` avant chaque fichier de
la boucle (numéroté sur `paths`, la liste brute — un fichier ignoré compte quand
même dans ce que voit l'utilisateur), et un paramètre `generate_summary: bool =
True` (`LoadFilesRequest`, `POST /files/upload`) qui saute le résumé
automatique sans jamais toucher à l'indexation/l'attachement. Coché par défaut
dans `ModuleBar.tsx` — décoché, c'est pour plusieurs gros fichiers sur un poste
sans FLM, où l'indexation séquentielle (repli Ollama ci-dessus) est déjà longue
en soi. **`set_resume_contexte` n'est appelé que si `generate_summary` est
vrai** : sauter le résumé ne doit jamais écraser un résumé déjà présent sur la
conversation par une chaîne vide — ce que faisait, et continue de faire
volontairement, le cas `generate_summary=True` avec `text_parts` vide.

**IMPÉRATIF — `describe_image` a son propre timeout (`_VISION_TIMEOUT_S`,
60 s dans `core/llm.py`), jamais `model.timeout_s`.** Cette méthode tourne en
synchrone dans le chargement d'un fichier, pas dans une conversation active :
elle doit échouer vite et retomber sur le placeholder, pas bloquer jusqu'aux
défauts globaux des clients (600 s SDK openai, 300 s en lecture pour
`ollama_client`, le client PARTAGÉ du chat). Deux mécanismes différents, parce
qu'aucun des deux SDK ne se pose pareil :

- **Ollama** : `Client.chat()` n'a pas de paramètre `timeout` par appel — un
  second client, `_vision_ollama_client`, est construit une fois avec ce
  timeout court, distinct de `ollama_client`.
- **openai (flm)** : `create(timeout=...)` existe, mais **la retry policy par
  défaut (2 essais) MULTIPLIE l'attente sur un timeout au lieu de la borner** —
  mesuré : un `timeout=0.5` seul relève à 5,4 s avant de lever, contre 1,9 s
  avec `max_retries=0`. `describe_image` pose donc les deux ensemble via
  `.with_options(timeout=..., max_retries=0)`. Sans `max_retries=0`, le
  timeout affiché ne borne rien — le pire cas réel serait ~3x plus long.

**Qualité mesurée, pas supposée — `moondream` transcrit mais décrit mal.** Sur
un texte simple (« THALES 42 » seul), les deux providers transcrivent
correctement. Sur une image plus proche d'un cours réel (triangle annoté +
formule « AB/AC = AM/AN = 3/5 ») : `flm:qwen3vl-it:4b` transcrit le titre ET
la formule mot pour mot, en français ; `moondream` décrit la forme du triangle
mais **ne transcrit pas la formule** (« a list of numbers and letters ») et
répond en anglais à un prompt français. `moondream` reste le repli retenu
(seul modèle vision Ollama vérifié, se pull et tourne vite) mais son résultat
sur du texte structuré est plus faible que celui de `flm` — à garder en tête
avant de compter sur la transcription Ollama pour des formules.

**`_VISION_PROMPT` (`core/llm.py`) est délibérément COURT.** Mesuré sur
`moondream` : une formulation plus longue, énumérant titres/légendes/formules/
annotations entre parenthèses, fait dégénérer ce modèle — réponse VIDE
(`eval_count: 1`) ou boucle de répétition (1265 tokens de charabia pour la même
image). La forme courte est robuste sur les deux providers câblés ; ne pas
l'étoffer sans rejouer la mesure sur `moondream`.

### 3.3 ter Analyse d'image dans le CHAT — un troisième chemin, pas le même

Ne pas confondre avec le §3.3 bis ci-dessus : celui-là décrit ce qui se passe
à l'**import** d'une image. Ce qui suit se passe dans un **tour de chat**, et
les deux coexistent volontairement.

| chemin | quand | prompt | où va le résultat |
|---|---|---|---|
| **import** (`RAGEngine._texte_image`) | à l'indexation du fichier | générique (`prompt_vision()` sans question) | chunk du RAG + `résumé_contexte` |
| **chat** (`core/vision_chat.py`) | au premier tour qui a une image attachée sans analyse | **la question de l'utilisateur** | `analyses_image[clé du chemin]` de la conversation |

Ce qui a forcé le second, mesuré et non supposé : la légende générique **ne
permet pas de répondre** à une vraie question sur un énoncé dense — elle décrit
une figure et des symboles. Et le chat ne voyait jamais l'image, seulement ce
résumé recopié dans `[CONTEXTE ACTIF]`.

Cinq points qui se déduisent mal :

- **`describe_image(path, model, question=None)` — sans question, le
  comportement d'avant, à l'octet.** C'est ce que vérifie `test_vision_images.py`,
  qui n'a pas été modifié d'une ligne : le jour où il faut le toucher pour
  faire passer un changement de ce chemin, c'est l'import qui a bougé.
- **Le déclenchement est une règle, pas une heuristique** : image attachée +
  pas encore d'analyse = on analyse. Aucune détection de « cette question
  a-t-elle besoin de l'image ? ». La **réanalyse**, elle, n'est jamais
  automatique : seul `@image` (`vision_override`) la force.
- **Le cache est par FICHIER** (`analyses_image`, dict clé `cle_chemin`), pas
  par conversation. C'est exactement ce que `résumé_contexte` ne sait pas
  faire, et la raison de ne pas s'en servir ici. Écrit par
  `HistoryEngine.set_analyse_image`, **une transaction par image**, distincte
  des deux du tour — une transaction ouverte pendant un appel vision tiendrait
  le verrou 26 s. Corollaire : la copie de `conv` que détient l'appelant est
  périmée juste après, il doit assembler son contexte avec la valeur en main.
- **Une image analysée sort de la requête documentaire du tour**, et ses chunks
  sont filtrés (`vision_chat.chunk_redondant`). Sans ça le prompt porte les
  DEUX descriptions de la même image — l'analyse ciblée et la légende d'import
  remontée par le RAG, la seconde étant la plus longue et la moins utile. Le
  filtre post-hoc existe *en plus* de l'exclusion, parce que `@cours`
  (`rag_override == "all"`) n'a aucune liste de fichiers à filtrer.
- **Trois bornes, pour `n_ctx: 4096`** (`core/vision_chat.py`) : par analyse
  conservée (2 000 caractères, tronqué **à l'écriture** pour que le disque et
  le prompt disent la même chose), par tour (2 images, borne de LATENCE — le
  reste est reporté au tour suivant, pas perdu), et par contexte injecté
  (4 000 caractères, l'omission étant **écrite dans le bloc**).

Le modèle vient de `core.models.modele_vision_pour(modèle_actif)` : le modèle
actif s'il **déclare** la vision (`vision is True`, jamais un `None` ni le nom
du modèle — cf. `decrire_capacites`), sinon le choix de l'import, sinon
seulement un repli **cloud** (`_VISION_CLOUD`), qui est la seule entorse
assumée au §3.7 et n'est atteint que sur une absence locale constatée. Les deux
modèles de cette table **n'ont pas été mesurés** — ils empruntent le chemin
`image_url` base64 déjà mesuré sur `flm`, et `gemini` en est exclu parce qu'il
n'est pas dans `_OPENAI_COMPAT` (`describe_image` lèverait au lieu de dégrader).

**IMPÉRATIF — cet appel entre dans `usage_tracker`, comme le reste du tour.**
Trou trouvé en relecture, avant merge : le tour de chat compte ses tokens
cloud depuis la sentinelle `__stats__` de `stream()`, mais `describe_image`
n'en émettait aucun — un appel **payant** avait donc lieu dans un tour de chat
sans figurer au quota. Un quota qui sous-compte est pire qu'un quota absent :
il donne confiance dans un chiffre faux. D'où le paramètre de sortie
`describe_image(..., stats=None)`, rempli sur place (`prompt_eval_count`/
`eval_count` côté Ollama, `usage.prompt_tokens`/`completion_tokens` côté
openai — deux vocabulaires traduits une fois, pas chez chaque appelant), ignoré
quand l'appelant n'en fournit pas, donc **invisible pour le chemin d'import**.
Compté sur SUCCÈS seulement : un échec peut être pré-vol (clé absente →
`ValueError` avant tout HTTP), et sur-compter serait tout aussi faux. Aucune
branche « est-ce local ? » dans le routeur — `QuotaTracker.track` écarte
lui-même les providers locaux.

**Latence : `run_in_executor`, et l'attente rendue VISIBLE.** L'appel reste
synchrone et borné à 60 s ; il part dans l'exécuteur comme les appels RAG et
mémoire du même tour, sinon la boucle d'événements du backend entier est bloquée
6 à 26 s (plus un token nulle part, et le précédent est connu : un flux muet
finit coupé côté client). Le TOUR attend quand même, et c'est le bon choix —
l'analyse doit être dans le prompt du message qui l'a demandée. D'où
l'événement `vision_analyse` (`en_cours` / `terminée` / `échec`), et un `échec`
qui **survit au `done`** côté frontend : il dit que la réponse qu'on vient de
lire a été construite sans l'image.

**Périmètre explicitement laissé dehors** : LM Studio (`describe_image` le
servirait par sa branche `_OPENAI_COMPAT`, mais son format vision n'a pas été
mesuré et `modele_vision_pour` ne le propose jamais).

**Collage (Ctrl+V) dans le composer du chat.** `onPaste` sur le `<Textarea>`
(`frontend/src/modules/chat/Component.tsx`) vise le même point d'entrée que le
glisser-déposer : `uploadFiles` de `ModuleBar.tsx`, exposé au composer via la
prop `uploadFilesRef` (un ref, pas un state — `uploadFiles` change de
référence à chaque rendu où `conversationId`/`generateSummary` changent).
`preventDefault()` n'est appelé qu'APRÈS avoir vérifié qu'au moins un fichier
collé a une extension de `EXTENSIONS_ACCEPTEES` (désormais exportée) — un
collage de texte ou d'un type non supporté suit son cours normal. Un fichier
sans extension reconnue (screenshot au nom générique ou vide) est renommé
depuis son type MIME avant ce filtre. L'appel passe
`{ generateSummary: false }` : coller vite ne doit pas déclencher le résumé
automatique (case cochée par défaut dans le panneau 📎) — seul l'attachement +
l'indexation RAG/vision est voulu. L'image suit ensuite exactement le chemin
§3.3 ter (`vision_chat`, au tour de chat suivant).
