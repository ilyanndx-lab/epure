# Épure — Contexte historique derrière des règles du noyau

> Ces paragraphes ont été retirés de `CLAUDE.md` le 2026-09-20 pour alléger le
> noyau. La règle elle-même (le IMPÉRATIF) reste dans `CLAUDE.md` ; ce qui suit
> est le pourquoi complet, non résumé, non édulcoré. Un renvoi `docs/claude/
> contexte-historique.md §N` dans le noyau pointe ici.

---

## §1 — pourquoi « le cœur est générique » est un IMPÉRATIF

Le contraire s'était installé sans que rien ne le signale : profil élève né en
« PTSI2 », `watch_folders` sur `Maths / Physique-Chimie / SI`, tri de PDF
n'acceptant que ces trois matières, et trois prompts de `core/` parlant d'un
« étudiant en prépa ». Quiconque installait Épure héritait de la filière de
son auteur. `backend/test_coeur_generique.py` tient la frontière — il liste
ses tolérances, qui sont des explications historiques, jamais du comportement.

---

## §3.2 — pourquoi `_LazyEngine` reste nécessaire

Raison historique : `RAGEngine` importait torch + sentence-transformers
(~30 s à chaud, 2 min à froid) et bloquait uvicorn au point que `/health` ne
répondait pas. La pile légère (§3.4) a ramené ce coût à moins d'une seconde,
mais la paresse reste **nécessaire** : construire ce moteur peut déclencher le
téléchargement de 90 Mo, qu'on ne veut pas au démarrage.

---

## §3.3 — la mesure derrière la suppression de `modules_state.json`

Il portait un second `status` par module, en doublon de `modules_activés`.
Deux fichiers pour une notion divergent mécaniquement — c'est ce qui a été
mesuré avant migration : 9 des 11 entrées de `modules_state.json` pointaient
des modules effacés, 4 des 12 entrées de `modules_activés` aussi, et
`reviseur` était installé et monté tout en étant absent de la barre.

Règles complètes à respecter, au-delà du strict nécessaire gardé dans le
noyau :

- `core/module_registry.py:active_ids()` est la **seule** lecture d'état.
  Liste vide → tous les modules installés (défaut d'installation neuve, ordre
  `discover_manifests`, donc alphabétique et déterministe).
- `set_status(id, "active"|"disabled")` ajoute/retire dans la liste. Signature
  et endpoint `PUT /modules/{id}/status` conservés.
- Le champ `status` de `GET /modules` reste `"active"|"disabled"` : il est
  **dérivé** de l'appartenance à la liste. Le frontend en dépend
  (`src/modules.ts`, `ModuleManifest.status`) — ne pas le renommer.
- `settings` ne peut pas être désactivé : refusé par `set_status`, et réinjecté
  à l'écriture (`core/instance.py:_garder_settings`) comme à la lecture. La
  liste pilote le montage : la lui faire perdre débranche l'écran qui sert à
  la réparer.

---

## §6 — le mécanisme exact de la fuite du token dans les logs uvicorn

La ligne fuyante était écrite par **uvicorn**, qui journalise le chemin avec
sa query : `"WebSocket /ws/chat?token=…" [accepted]`. `test_logs_secrets.py`
affirme aussi que `main` installe le filtre `core/logs.py` (sinon le module
resterait parfait et jamais appelé).
