# Feuille de route — ordre d'exécution et prompts

Séquence des quatre plans de `docs/` plus le chantier d'isolation, qui n'a pas
de plan. Chaque étape porte : le prompt à donner à Claude Code, les précisions
issues de l'analyse croisée (elles ne sont **pas** dans les documents source),
les commits attendus, et la condition d'arrêt.

**Règle générale, valable partout.** Tout nouveau fichier de test s'appelle
`test_*.py`, importe `_test_env` en première ligne, et trie **avant**
`test_zz_donnees_reelles.py`. La découverte automatique s'en charge, il n'y a
rien à ajouter à `ci.yml`. Pousser fait partie du lot : tant que ce n'est pas
poussé, seul le poste de développement a jugé.

---

## Ordre retenu

```
0.  commiter les quatre plans            commande
1.  deux questions préalables            aucun commit
2.  profils                              2 commits
3.  registre — étape A seule             1 commit
4.  démontage option D                   5-6 commits
5.  isolation worker                     plan d'abord, aucun code
6.  registre — étapes B à E              après 5, jamais avant
```

`anonymisation-mesure.md` est hors de cette file : dépôt séparé, aucune
dépendance à Épure. À lancer quand tu veux, réduit à sa seule expérience §5.

**Le point qui commande l'ordre.** `registre-modules.md` §5 écrit : « la
confiance porte sur les personnes. Elle ne porte pas sur ce que leur LLM a
écrit, et c'est la faiblesse assumée de ce plan. » C'est exact, et c'est
pourquoi les étapes B à E attendent l'isolation. Le contrôle proposé à l'étape D
— « le bouton Installer n'apparaît qu'après défilement jusqu'au bas du code » —
mesure un défilement, pas une lecture : **il ne peut pas échouer**, donc il ne
contrôle rien. L'étape A, elle, est irréversible si on l'oublie : elle passe
maintenant.

---

## 0 — Commiter les plans

```
git add docs/profils.md docs/registre-modules.md docs/anonymisation-mesure.md docs/demontage-option-d.md
git commit -m "docs: plans profils, registre, demontage option D, protocole anonymisation"
git push
```

---

## 1 — Deux questions préalables

Aucun code, aucun commit. Les réponses changent le périmètre de l'étape 2.

> Deux questions, pas de code.
>
> **1. Où vit le prompt système du chat ?** Cherche dans `backend/config.yaml`,
> `backend/core/llm.py`, `backend/modules/chat/router.py`, `backend/core/memory.py`
> et partout ailleurs. `docs/profils.md` §4 le met hors périmètre faute de
> l'avoir localisé, alors qu'il motive trois profils sur quatre. Si sa forme se
> prête à être placée dans un profil, dis-le et estime le coût ; sinon explique
> ce qui l'en empêche.
>
> **2. Où doit vivre `profil_actif` ?** `docs/profils.md` §3.3 expose un champ
> `actif: bool` sans dire où l'état est stocké. Rappel de l'étape C : deux
> sources de vérité pour un même état, c'est exactement ce qu'on a supprimé en
> tuant `modules_state.json`. Propose, argumente, ne code pas.
>
> Réponds aux deux, je tranche ensuite.

---

## 2 — Profils

> Lis `docs/profils.md` et implémente-le. Quatre précisions qui ne sont pas dans
> le document :
>
> **a.** `profil_actif` vit dans `instance_config` et nulle part ailleurs. Aucun
> nouveau fichier d'état. Si l'implémentation semble en réclamer un, arrête-toi
> et explique pourquoi — c'est le signe d'un modèle mal posé, pas d'un besoin de
> stockage.
>
> **b.** Appliquer un profil désactive des modules, et **désactiver ne démonte
> pas les routes** : `set_status` retire l'id de `modules_activés`, le routeur
> reste monté jusqu'au redémarrage. Un profil `minimal` laisse donc des routes
> répondre alors que l'interface ne montre plus le module. Ce n'est pas une
> régression — c'est le comportement actuel — mais le document ne le mentionne
> pas. Documente-le, et dis-moi si tu penses qu'il faut le traiter ici ou le
> laisser à l'option D (étape 4).
>
> **c.** Le §4 met le prompt système hors périmètre. Applique la réponse de
> l'étape 1 : s'il est localisable et transposable, mets-le dans le format ;
> sinon garde le hors périmètre en citant la raison exacte.
>
> **d.** Le §5 signale que `backend/config.yaml` et `modules/settings/router.py`
> n'ont pas été lus, et que la forme de `PUT /instance/config` est déduite de
> docstrings. Lis-les avant d'écrire, et signale tout écart entre ce que la
> docstring annonce et ce que le code fait.
>
> Les tests du §3 doivent être réellement falsifiables : `settings` réinjecté,
> id `../evil` refusé, idempotence de l'application d'un profil. Vérifie chacun
> en cassant le code exprès.
>
> Deux commits : `feat(profils): profils d'instance appliques via modules_activés`
> puis `feat(settings): ecran de selection de profil`.
>
> Puis pousse et donne-moi `gh run list --branch <branche> --limit 2`.

---

## 3 — Registre, étape A seule

> Lis `docs/registre-modules.md` **étape A uniquement** — n'entame ni B, ni C,
> ni D, ni E. Motif : les étapes suivantes font entrer du code tiers dans le
> process principal alors que l'isolation worker n'est pas câblée (CLAUDE.md §7).
> L'étape A, elle, est irréversible si on l'oublie : ajouter `manifest_version`
> rétroactivement est impossible une fois des modules dans la nature.
>
> Précisions hors document :
>
> **a.** Tous les manifestes existants sont concernés — le cœur
> (`backend/modules/*`) **et** le catalogue (`modules-catalogue/*`), soit une
> vingtaine de fichiers. N'en oublie aucun ; un manifeste sans version sera
> indistinguable d'un manifeste v1 plus tard.
>
> **b.** Le lecteur doit **refuser** une version inconnue, pas l'ignorer. Un
> refus bruyant vaut mieux qu'une lecture partielle silencieuse — c'est la même
> règle que le garde de version de `test_versions_epinglees.py`.
>
> **c.** Un test dédié : manifeste sans `manifest_version` refusé, version
> future refusée avec un message qui dit quoi faire, version courante acceptée.
>
> Un commit : `feat(modules): manifest_version, socle de compatibilite du registre`.

---

## 4 — Démontage, option D

Cinq à six commits, selon le découpage de `docs/demontage-option-d.md` §3.
Chacun poussé et vert avant le suivant.

> Lis `docs/demontage-option-d.md` et exécute les étapes A, A-bis, B et C.
> **N'entame pas l'étape D** avant de m'avoir rapporté la mesure demandée par le
> §4.
>
> Trois précisions :
>
> **a. L'étape D est mal formulée dans le document.** Elle dit « supprimer
> `_drop_module_routes` et **ses appels** », alors que le §4 établit que
> `_remount` est un de ces appels et qu'il doit « vraisemblablement emprunter le
> chemin sentinelle lui aussi ». L'instruction supprimerait donc un appel que le
> §4 dit devoir être *remplacé*. Corrige le document en même temps : « supprimer
> `_drop_module_routes` ; rebrancher `_remount` sur le chemin sentinelle ».
>
> **b. La mesure du §4 est bloquante.** Réapprouver un module dans l'Atelier
> passe par `_remount`. Le document dit lui-même que ce chemin n'a jamais été
> exercé en fastapi ≥ 0.137 et que c'est une déduction. Mesure-le sur un venv en
> 0.141 : compte les routes avant et après réapprobation, et dis-moi si le
> routeur est remplacé ou empilé. Si la réapprobation reste cassée après
> dépinglage, le dépinglage est prématuré et on s'arrête là.
>
> **c.** Les deux tests de route fantôme sont **réécrits, pas supprimés** : un
> test unitaire vérifiant que la désinstallation écrit la sentinelle, un test
> d'intégration vérifiant le 404 après redémarrage. Le second va dans
> `integration_*.py`, donc dans le job manuel. Le document appelle ça une
> dégradation réelle — écris-la dans le CHANGELOG, pas seulement dans le
> document.
>
> Commits selon le §3 du document, un par étape. Rapporte après C.

Puis, une fois la mesure du §4 rapportée et si elle est concluante :

> Étape D. Applique la formulation corrigée : supprimer `_drop_module_routes`,
> rebrancher `_remount`, lever les épingles de `requirements.txt` **et** de
> `ci.yml` — les deux, `test_versions_epinglees.py` vérifie leur accord. Garde
> ce test, en retirant seulement la borne sur `fastapi.__version__`. Mets à jour
> l'en-tête de `docs/limite-demontage.md` sans supprimer le document.
>
> Commit : `chore(deps): depinglage de fastapi apres suppression du demontage a chaud`.

---

## 5 — Isolation worker

Ce chantier n'a pas de plan. Il en faut un avant tout code, parce qu'il touche
le montage de tous les modules.

> Écris `docs/isolation-cablage.md`, dans le format des autres plans de `docs/` :
> contexte, ce qui existe et est vérifié, les pièges, les étapes avec leur
> vérification, hors périmètre, ce qui n'a pas été vérifié, prompts.
>
> État de départ, à vérifier plutôt qu'à croire : `backend/core/module_worker.py`
> existe et ses 5 tests passent (env réduit à une allowlist, garde d'import
> `core.*`, clé worker exigée, import interdit qui tue le worker). Ce qui manque
> est le câblage : aucune route `/capabilities/*`, aucun proxy `/<id>/*`,
> `spawn_worker` n'est appelé que par les tests, et `module_registry` importe
> encore tous les routers dans le process principal.
>
> Questions auxquelles le plan doit répondre, avec des mesures et pas des
> déductions : le coût de démarrage d'un worker par module ; ce qui se passe
> quand un worker meurt ; comment le proxy relaie le SSE et les WebSockets ;
> si les modules du **cœur** passent aussi par un worker ou restent en process ;
> et ce que devient `_remount` dans ce modèle.
>
> N'écris aucun code. Le plan d'abord, je le relis, on exécute ensuite.

---

## 6 — Registre, étapes B à E

À n'ouvrir qu'après le 5. Quand ce sera le cas, la précision qui devra figurer
dans le prompt :

> L'écran de confiance de l'étape D doit être remplacé par un contrôle capable
> d'échouer. « Le bouton apparaît après défilement » mesure un défilement, pas
> une lecture. Une fois l'isolation en vigueur, le contrôle réel est
> l'isolation elle-même, et l'écran redevient de l'information — dis-le comme tel
> plutôt que de le présenter comme une garantie.

---

## Hors file — Anonymisation

Dépôt séparé, aucune dépendance à Épure.

> Lis `docs/anonymisation-mesure.md` et exécute **uniquement l'expérience §5**,
> la ré-identification adverse. Motif : le document la désigne lui-même comme
> « la moins chère et celle qui peut clore la question ». Si la ré-identification
> réussit, les expériences §4 et §6 sont sans objet.
>
> Une correction de protocole avant de commencer : le §1 pose « seuil de
> décision fixé avant de mesurer », mais le §5 n'en fixe aucun — il dit « si le
> taux est significatif ». Fixe le seuil **maintenant**, écris-le dans le
> document, et adopte la variante 1/K qui le rend interprétable. Sans ça tu
> jugeras le résultat après l'avoir vu, ce que tout le protocole cherche à
> éviter.
>
> Nouveau dossier hors de `epure/`, aucune modification du dépôt Épure.
