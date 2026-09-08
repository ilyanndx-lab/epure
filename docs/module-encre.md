# Module `encre` — prise de notes manuscrites et transcription

Feuille de route. Écrite avant la première ligne de code, parce que la décision
structurante de ce projet n'est pas technique : c'est **à quoi sert la
transcription**. Ce document fixe cette décision et l'ordre qui en découle.

---

## 0. La décision, et pourquoi

**L'encre est le document. La transcription est un index, pas une sortie.**

Tu écris au stylet dans Épure ; c'est l'encre que tu relis. La transcription
tourne en arrière-plan, best-effort, et sert à rendre tes notes manuscrites
**cherchables au milieu de tes fiches** (collection `fiches` du store
vectoriel). La conversion en LaTeX propre est une action ponctuelle et
manuelle, sur une expression que tu sélectionnes — jamais le mode par défaut.

### Pourquoi pas « manuscrit → LaTeX propre »

L'état de l'art sur CROHME (CoMER) plafonne à **59,3 / 59,8 / 62,97 %
d'ExpRate** — expression *entièrement* exacte, sur des expressions isolées,
courtes et propres. Les notes réelles sont pires. Le coût d'un chemin manuscrit
s'écrit :

    coût = t_écriture + t_vérif + (1 − p) · t_correction

Cinq raisons pour lesquelles ce coût ne descend pas :

1. **`t_vérif` ne dépend pas de `p` et ne disparaît jamais.** À p = 0,9 tu
   relis encore chaque expression, faute de savoir dans quels 10 % tu es. Coût
   plancher. Le gain marginal d'un fine-tuning de 0,6 → 0,9 vaut
   ~7,5 s/expression, soit ~9 600 expressions pour amortir 20 h de collecte —
   hors développement. Ce seuil ne sera jamais atteint.
2. **L'erreur silencieuse coûte hors de proportion.** Un LaTeX qui compile et
   qui est faux (indice devenu exposant, borne perdue) ne coûte pas 30 s : il
   coûte une notion apprise de travers. Ça impose la vérification à 100 %, donc
   verrouille (1).
3. **La difficulté croît multiplicativement, la frappe linéairement.** Une
   matrice 3×3 = 9 tirages : à 0,95 par cellule, 0,63 pour l'expression. Le
   modèle est le plus mauvais exactement là où on voudrait gagner du temps.
4. **La confiance du modèle n'est pas exploitable** — mal calibrée, et
   confiante précisément sur les erreurs structurelles. À vérifier en phase 0 :
   c'est le seul levier qui attaquerait le coût plancher.
5. **Le meilleur substitut gagne.** Taper de l'ASCII ambigu et laisser un LLM
   le convertir est quasi 100 % fiable (entrée non ambiguë) et coûte zéro
   développement.

Le cadrage « index » échappe aux cinq : `t_vérif = 0` (on ne relit jamais un
index), une erreur silencieuse coûte au pire une page non retrouvée, le rappel
dépend des symboles et non de la structure, aucune calibration n'est arbitrée,
et rien d'autre ne rend des notes manuscrites cherchables. **La valeur arrive à
p = 0,6, sans une ligne d'entraînement** — le fine-tuning redevient une
optimisation optionnelle au lieu d'être la condition de survie du projet.

Nuance conservée : sur une encre **déjà écrite**, le coût d'écriture est
irrécupérable, donc l'export ponctuel vaut ~20 s contre ~25 s à taper.
Marginalement positif — d'où sa présence, en action manuelle uniquement.

---

## 1. Périmètre et contraintes

- **Module personnel**, hors `modules-catalogue/`. Il n'est pas destiné au
  paquet distribué, donc `transformers`/`optimum` sont autorisés côté
  inférence : la contrainte Smart App Control / ARM64 (§3.4 de CLAUDE.md) ne
  s'applique qu'aux destinataires. Si ce périmètre change un jour, il faudra
  réécrire le décodage en numpy pur, à la manière de `core/wordpiece.py`.
- **Entrée stylet actif** (Yoga). La photo est une entrée secondaire, plus
  tard, et son fossé de domaine (tracé rendu ≠ photo de papier) devra être
  mesuré avant d'être promis.
- **Deux machines.** Inférence sur le Yoga (`onnxruntime`, déjà déclaré),
  entraînement sur l'Acer RTX 4060. **Le contrat entre les deux est un
  fichier**, pas un service : le dataset monte, un `.onnx` + sha256 redescend.
  Épure reste mono-instance et local-first.
- **Aucune nouvelle dépendance globale lourde.** Épure n'a **pas** de mécanisme
  de dépendances par module (un module = `manifest.json` + `router.py` +
  `Component.tsx`, aucun ne déclare de dépendance) : tout atterrit dans le
  `requirements.txt` global, celui qu'on a fait passer de 198,3 à 14,1 Mo.
  L'inférence tient avec `onnxruntime`, `numpy` et `Pillow`, tous déjà
  déclarés.

### Où vit le moteur

`core/hmer.py`, exposé par un `_LazyEngine` dans `core/runtime.py`, exactement
comme `whisper` et `piper`. Ce n'est **pas** une entorse au « cœur générique »
(§1) : reconnaître des maths manuscrites ne présume aucune filière, pas plus
que transcrire de la parole. La règle interdit de présumer *une* filière, pas
d'avoir des capacités. Les poids se téléchargent au premier usage avec
vérification sha256, comme `core/embedding_install.py` et `core/voice.py` — et
**aucun sous-processus**, jamais de `pip` à l'exécution (§8).

### Chemins et données

| chemin | nature | `_test_env` |
|---|---|---|
| `resolve_encre_dir()` — encre + dataset | **données utilisateur, irremplaçables** | détourné **et** dans `REAL_DIRS` |
| `resolve_hmer_dir()` — poids du modèle | cache reconstructible | détourné, **absent** de `REAL_DIRS` |

Même distinction que `memory/` contre `piper_models/` (§3.5). Ajouter les deux
variables à `_test_env` (qui en pose sept aujourd'hui) et `resolve_encre_dir()`
au `.gitignore`.

**Stocker l'encre brute, toujours.** Les tracés `(x, y, t, pression)` sont la
donnée irremplaçable ; le rendu PNG et la transcription sont dérivés et
reproductibles. C'est ce qui permet de changer de modèle plus tard et de tout
retranscrire sans rien perdre — donc le rendu se fait **côté serveur** depuis
les tracés (Pillow), pas seulement côté client au moment du dessin.

---

## 2. Phases

Chaque phase a une valeur autonome. C'est une contrainte, pas un confort : avec
des interruptions de plusieurs semaines, un projet dont la valeur n'arrive
qu'à la fin ne survit pas.

### Phase 0 — Mesure. **Avant toute ligne de module.**

Hors Épure, script isolé, une soirée.

- 40–50 expressions de ton cours, écrites à la main au stylet, exportées en PNG
  (n'importe quelle app de dessin suffit — le canvas n'existe pas encore).
- Trois baselines : `pix2text-mfr` (TrOCR ré-entraîné sur formules, gère le
  manuscrit, MIT, export ONNX disponible), `qwen3-vl:8b` en local, et
  éventuellement une VLM cloud comme borne haute.
- Trois mesures : ExpRate exact ; **taxonomie d'erreurs** (glyphe / structure /
  hallucination) ; **calibration** (corrélation score de séquence ↔ exactitude).

**Portes de sortie :**

- si > 60 % des erreurs sont **structurelles** → le fine-tuning ne rendra pas
  ce qu'il coûte. Phase 4 abandonnée d'avance, et le module ML n'est pas
  construit pour ce projet.
- si la calibration est exploitable → la relecture peut être ciblée, et
  l'export LaTeX ponctuel devient bien plus rentable qu'estimé.

*Valeur si tout s'arrête là : tu sais où tu en es, chiffré, au lieu de le
supposer.*

**Résultat mesuré (corpus réel de 50 expressions, `pix2text_mfr` seul —
`qwen3-vl:8b` non installé sur l'Acer, écarté proprement) :**

- ExpRate normalisé : **24,0 %** (12/50, IC95 Wilson [14,3 %, 37,4 %]).
- Répartition des erreurs (pool normalisé, sur les 38 fausses) : 24 %
  structurelle, 24 % glyphe, 16 % hallucination, **36 % mixte** — la plus
  grosse catégorie est le fourre-tout non diagnostiqué, à garder en tête :
  moins de la moitié des erreurs sont clairement de la matière à fine-tuning.
- Porte 1 (structurelle > 60 %) : **non déclenchée** (24 % ≤ 60 %) → le
  fine-tuning a une prise plausible, sans plus.
- Porte 2 (calibration exploitable) : **non déclenchée, et inversée** — le
  modèle est le plus confiant sur ses erreurs structurelles (log-prob moyenne
  -0,019) et le moins confiant sur ses hallucinations (-0,115). Une relecture
  ciblée par score ne fonctionne pas ; elle laisserait passer en priorité les
  pires erreurs.
- **Bug trouvé et corrigé au passage, à retenir pour la phase 2** : la
  première mesure donnait 0 % avec 66 % d'hallucinations — pas un problème du
  modèle. L'encre n'occupait que 2-10 % du canevas exporté (900×320,
  écriture petite et centrée) ; une fois redimensionnée par le préprocesseur
  du modèle, elle devenait illisible. Fix : recadrage au bounding box du
  contenu non-blanc (+10 % de marge) **avant** l'appel au modèle, maintenant
  intégré dans `adaptateurs/pix2text.py` pour toute image future. **Le
  moteur réel de la phase 2 (`core/hmer.py`) devra faire ce même recadrage**
  sur l'encre reçue — ne pas repartir de zéro sur ce point.

**Décision : on ferme la phase 0, on passe à la phase 1.** Le module ML
(phase 4) reste conditionnel, non construit — alimenté plus tard par la
vraie collecte de la phase 3, pas par ce corpus de test.

### Phase 1 — Canvas et encre. Zéro ML.

C'est le gros du travail, et c'est la partie ennuyeuse.

- Module `encre` : `manifest.json`, `router.py`, `Component.tsx`. Prefix de
  montage `""` → **chaque route écrite préfixée à la main** (`/encre/...`),
  sinon collision silencieuse avec une route core (§3.3).
- Backend : `GET|POST /encre/pages`, `GET|PUT|DELETE /encre/pages/{id}`.
  Stockage via `core/jsonstore.py` (jamais `json.load`/`json.dump` direct),
  `resolve_encre_dir()`.
- Frontend : `perfect-freehand` (MIT, ~4 ko, zéro dépendance) pour le rendu de
  tracé sensible à la pression — la partie la plus pénible à faire soi-même.
  Pointer Events + **`getCoalescedEvents()`** (sans lui on perd la moitié des
  points d'un stylet à 240 Hz), `touch-action: none`, rejet de la paume
  (ignorer `pointerType !== 'pen'` dès qu'un stylet est actif), gomme,
  annuler/refaire, pagination.
- Réseau : tout via `src/api.ts` (`apiFetch`, `API`) — jamais de `fetch()` nu.
- Tests : `backend/test_encre_store.py` (jsonstore, confinement de chemin par
  `resolve()` + `is_relative_to()`), et un `.test.tsx` qui **éprouve la forme
  des réponses** — rejouer un corps d'erreur réel, idiome `liste()`/`dico()` de
  `ModuleBar.tsx`.

*Valeur autonome : une app de prise de notes au stylet dans Épure. Utilisable
sans une ligne de ML.*

### Phase 2 — Transcription → index RAG. **Le cœur du projet.**

- `core/hmer.py` + proxy `_LazyEngine` dans `core/runtime.py`. Import des
  dépendances **dans `__init__`**, jamais en tête de module : la paresse du
  proxy ne couvre que la construction, pas l'import (§3.4).
- Transcription en tâche de fond → **locale, sans discussion** (§3.7). La
  post-correction LLM éventuelle passe par `modele_pour_tache(use_cloud=False)`,
  jamais par `modèle_actif`, jamais `None`. Test sur le modèle de
  `test_taches_locales.py` : pire cas posé d'abord (`modèle_actif` cloud + toutes
  les clés présentes).
- Par page : `transcrite`, `date`, **`modèle + version`** — pour pouvoir
  retranscrire tout l'historique quand le modèle change.
- Indexation dans la collection `fiches` avec un lien retour vers la page
  d'encre.
- Post-correction LLM : **suggestion affichée, jamais substitution silencieuse.**
  Un LLM transforme volontiers une expression juste en expression plausible et
  fausse.

*Valeur : tes notes manuscrites deviennent cherchables au milieu de tes fiches.*

**FAITE le 2026-09-07.** Ce qui a été construit, et les écarts avec ce qui était
prévu ci-dessus :

- `core/hmer.py` + `hmer_engine` (`_LazyEngine`), `EncreEngine.set_transcription`,
  `RAGEngine.index_page_encre`, `POST /encre/pages/{id}/transcrire`, un bouton et
  une zone de lecture dans `Component.tsx`. Tests : `backend/test_hmer.py`,
  extensions de `test_encre_store.py`, `test_rag_sources.py`,
  `test_dependances_declarees.py`, `test_paquet.py`, et sept cas de plus dans
  `Component.test.tsx`.
- **Déclenchement MANUEL, une page à la fois** — pas « en tâche de fond »
  comme l'annonçait le §2 ci-dessus. La latence CPU réelle n'était pas mesurée
  quand la phase a été décidée, donc un anti-rebond aurait été posé à l'aveugle.
  Elle l'est maintenant : **0,4 à 0,5 s par page** une fois le modèle chargé,
  **8,7 s** de construction du moteur (poids déjà présents), 16,7 s d'import
  d'`optimum`/`torch` à chaud. L'automatisation redevient une décision informée ;
  elle n'est pas prise.
- **Pas de post-correction LLM**, conformément au §2 : ce sera un lot séparé, une
  fois l'exactitude réelle vue en usage.
- **`torch` est une dépendance DURE**, contre ce qu'annonçait le §1 (« l'inférence
  tient avec `onnxruntime`, `numpy` et `Pillow`, tous déjà déclarés »). Mesuré,
  pas supposé : `optimum/onnxruntime/modeling_seq2seq.py` fait un `import torch`
  de niveau module, et `pip uninstall torch` casse l'import. Coût réel :
  **890,1 Mo** de `site-packages` dans un venv propre, dont ≈765 Mo de nouveau
  pour Épure (torch 509,0 · transformers 106,8 · sympy 69,3 · onnx 35,1 ·
  networkx 15,0 · tokenizers 7,4 · optimum 2,9 · …). Le §1 reste vrai sur le
  fond — rien de tout ça n'atteint un destinataire — mais par
  `HORS_PAQUET_PIP` et non parce que la pile serait légère.
- **Poids** : `breezedeus/pix2text-mfr`, révision épinglée
  `bea257edb2653f2ae413b084f2ac0e8299d08df0`, 117,7 Mo en huit fichiers,
  téléchargés en 44,7 s et vérifiés par sha256 — l'idiome de `core/voice.py`,
  comme annoncé. Le hub n'est jamais interrogé par `from_pretrained` :
  `core/runtime.py` pose `HF_HUB_OFFLINE=1` dès que le cache Whisper existe, donc
  un chargement par identifiant de dépôt échouerait sur ce poste.
- **Le recadrage de la phase 0 est porté à l'identique** — bounding box du contenu
  non-blanc, +10 % de marge, clippé aux bords. À noter : `machinelearn/` n'existe
  plus sur cette machine, donc il a été réécrit d'après la description de ce
  document et non recopié du fichier d'origine.
- **La source RAG est `encre:<id>`** et non un chemin de fichier synthétique. Elle
  apparaît telle quelle dans le panneau fichiers du chat (icône générique, pas de
  nom de fichier à afficher) ; « retirer » y fonctionne, « ouvrir dans un nouvel
  onglet » est refusé explicitement (404) au lieu de produire un 500.


### Phase 3 — Correction et collecte.

Deux modes dans le même écran :

- **Correction** : une expression transcrite, tu corriges le LaTeX → paire
  (encre, LaTeX) stockée. C'est ici que vit l'export LaTeX ponctuel.
- **Dictée inversée** : l'app tire une expression LaTeX **de tes propres fiches
  via le RAG**, l'affiche rendue, tu la recopies au stylet → vérité terrain
  gratuite, distribution réaliste et **ciblable** sur les symboles que tu
  confonds (profil issu de la phase 0). ~15–25 s la paire contre 60–90 s en
  annotant des notes existantes : c'est ce qui rend la phase 4 atteignable.

Ce n'est **pas** du renforcement — c'est de la collecte supervisée. Écrire des
symboles isolés est cheap mais transfère mal (la difficulté est la segmentation
et la disposition 2D) : utile pour établir le profil de confusions, pas pour
gagner de la précision de bout en bout.

Export : `.jsonl` + PNG rendus. C'est le contrat avec le module ML.

*Valeur : un profil de confusions, et un dataset qui grossit tout seul en
usage normal.*

### Phase 4 — Module ML et fine-tuning. **Conditionnelle.**

N'y aller que si la phase 0 a montré des erreurs majoritairement de glyphe
**et** que le dataset dépasse ~400 paires.

- Module `ml` sur l'Acer uniquement : suivi de runs, configurations, métriques,
  comparaison. Ce projet lui donne son premier vrai client — un module ML
  construit dans le vide produit une abstraction que rien ne contraint.
- Fine-tune de `pix2text-mfr` (VisionEncoderDecoder, `Seq2SeqTrainer`).
- **Split train/val par session d'écriture, jamais aléatoire** : deux copies de
  la même expression écrites le même jour fuiteraient d'un côté à l'autre.
- Export ONNX + sha256 + manifeste de version, déposé côté Yoga.
- **Évaluation contre la baseline gelée de la phase 0. Si ça ne bat pas, on ne
  déploie pas.**

---

## 3. Ce qui n'est pas décidé

- Le fossé de domaine tracé-rendu ↔ photo de papier, si l'entrée photo est
  reprise un jour. À mesurer, pas à supposer.
- La stabilité de l'écriture d'Ilyann. Ne pas la mesurer d'avance : elle se
  révélera comme un plafond précoce du fine-tuning, gratuitement.
- Le format d'échange du dataset au-delà du `.jsonl` (InkML est le standard
  CROHME, mais rien n'oblige à le suivre tant qu'un seul consommateur existe).
