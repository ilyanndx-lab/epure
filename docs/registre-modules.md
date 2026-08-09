# Registre de modules distribué — plan d'exécution

**Contexte.** Le catalogue (`docs/catalogue-modules.md`) est local : les six
modules installables sont dans le dépôt. L'objectif ici est que **d'autres
personnes publient leurs modules** et qu'Épure sache les récupérer, sans serveur,
sans compte, et sans annuler les frontières de sécurité déjà posées.

**Prérequis.** Aucun sur le code. Le prérequis est de **décision** : ce plan est
écrit pour un **cercle restreint (5-10 proches)**, canal de confiance hors bande.
Il ne convient pas à une distribution publique — cf. §5.

**Décisions actées avec Ilyann (2026-08-09) :**

| Question | Décision |
|---|---|
| Audience | Cercle restreint, 5-10 personnes qui se connaissent |
| Signature cryptographique | **Non.** sha256 épinglé + revue humaine du code avant installation |
| Infrastructure | **Aucune.** Un `index.json` dans un dépôt GitHub public, ajouts par PR |
| Isolation worker | **Pas un prérequis** à cette échelle — cf. §5, et à réévaluer au premier partage hors cercle |
| Transport | Archive `tar.gz` d'un tag GitHub, pas de `git` en dépendance |

---

## §0 — Ce qui existe déjà, vérifié en lisant le code

Trois constats tirés de `backend/core/catalogue.py` et
`backend/core/module_registry.py` au 2026-08-09. Ils réduisent beaucoup le
périmètre.

**1. Déposer un dossier à la main dans `modules-catalogue/` fonctionne déjà.**
`list_catalogue()` relit le disque à chaque appel — aucun cache, aucun index :

```python
for sub in sorted(base.iterdir()):
    mf = sub / "manifest.json"
    if not mf.is_file():
        continue
```

Donc l'idée de départ (« je télécharge dans le dossier et le module apparaît »)
n'est pas une fonctionnalité à écrire : **c'est le comportement actuel.** Le
registre n'invente pas un mécanisme d'installation, il automatise le *dépôt*
dans ce dossier et il le **valide**. C'est tout, et c'est beaucoup plus petit
que prévu.

**2. La copie et le confinement sont déjà écrits et éprouvés.** `catalogue.py`
importe de `module_workshop` : `_check_module_id`, `_backup_existing`,
`_frontend_component_path`, `_remount`, `modules_dir`. Sa docstring dit
explicitement pourquoi (« une seconde implémentation de la copie divergerait de
la première »). Le registre doit suivre la même règle : **il n'écrit aucune
copie, il alimente `modules-catalogue/`** et laisse `catalogue.install()` faire
son travail inchangé.

**3. `catalogue.install()` ne valide rien**, et sa docstring assume ce choix :

> Pas de `tsc` : les modules du catalogue sont VERSIONNÉS et vérifiés en CI par
> l'étape « Catalogue — type-check + build après installation »[…]

C'est le point qui bascule avec ce plan. Voir §1.

---

## §1 — Les trois hypothèses que le registre invalide

Ce sont les seules vraies difficultés. Le reste est de la plomberie.

### 1.1 — « Versionné et vérifié en CI » devient faux

La justification de l'absence de validation dans `install()` repose entièrement
sur le fait que le contenu de `modules-catalogue/` est dans le dépôt et passe la
CI. Un module téléchargé n'est ni l'un ni l'autre.

**Règle retenue : on ne touche pas à `install()`.** Sa justification reste vraie
si on la transforme en invariant :

> **Tout ce qui se trouve dans `modules-catalogue/` a été validé.**

La validation se fait donc **à la réception**, avant écriture dans
`modules-catalogue/`, pas à l'installation. Deux bénéfices : `install()` garde
son contrat et sa vitesse ; et un module invalide n'atteint jamais le catalogue,
donc il n'apparaît pas dans la liste avec un piège dedans.

### 1.2 — Le manifeste est auto-déclaré, et `uninstall()` lui fait confiance

`catalogue.uninstall()` lit les protections **depuis le manifeste sur disque** :

```python
if manifeste.get("core_module"):
    raise CatalogueError(f"Module du cœur, non supprimable : {mid}")
if not manifeste.get("removable", False):
    raise CatalogueError(f"Module marqué non supprimable : {mid}")
```

Un module reçu qui déclare `"core_module": true` devient donc
**indésinstallable depuis l'interface**. Aucune malveillance n'est requise : il
suffit qu'un LLM ait recopié le manifeste de `chat` comme modèle — c'est
exactement le genre d'erreur que produit une génération.

**Règle : trois champs sont réécrits à la réception, jamais lus depuis la
source.**

```
core_module : false
removable   : true
origin      : "registre"
```

`origin: "registre"` est distinct de `"catalogue"` et de `"workshop"` : un
module reçu n'est ni du code du dépôt, ni du code jetable généré localement, et
l'UI doit pouvoir le dire.

### 1.3 — `backend.prefix` est un point de montage non contrôlé

`register_routers()` fait `app.include_router(router, prefix=manifest.backend.prefix)`.
Le prefix vient donc du manifeste, c'est-à-dire de l'auteur du module.

Le catalogue actuel est **déjà incohérent** là-dessus : `reviseur` déclare
`"prefix": "/reviseur"` alors que `kholle` et `rangement` déclarent `""`, et
`CONVENTIONS.md` impose `""` avec les routes auto-préfixées. L'incohérence est
sans conséquence tant que le code est le tien ; elle en a dès qu'il ne l'est
plus.

**Règle : `backend.prefix` est forcé à `""` à la réception**, et on s'appuie sur
la règle déjà présente dans le gate AST (« Chaque route DOIT être préfixée par
`/<id>` »). Le contrôle existe, il suffit de ne pas laisser une seconde voie le
contourner.

> **Note pour plus tard, hors périmètre.** L'ordre de montage est celui de
> `discover_manifests()` (alphabétique) et les routes core sont déclarées avant
> dans `main.py`. Le comportement exact en cas de collision de chemin entre deux
> modules **n'a pas été mesuré**. Ça ne bloque pas ce plan (le gate impose le
> préfixe `/<id>`, et deux modules ne peuvent pas avoir le même id), mais si le
> contrat des routes se desserre un jour, il faudra le mesurer.

---

## §2 — Formats

### 2.1 — Manifeste étendu

Deux champs nouveaux, tout le reste inchangé :

```json
{
  "manifest_version": 1,
  "epure_min": "0.4.0",

  "id": "pomodoro",
  "version": "1.2.0",
  "nom": "Pomodoro",
  "icon": "Timer",
  "description": "…",
  "auteur": "prenom",
  "source": "https://github.com/prenom/epure-pomodoro",

  "frontend": { "component": "Component" },
  "backend":  { "prefix": "" },

  "core_module": false,
  "origin": "registre",
  "status": "active",
  "removable": true
}
```

**`manifest_version` est la version du *contrat*, pas celle du module.** Elle
n'est incrémentée que quand `CONVENTIONS.md` change d'une façon qui casse les
modules existants. C'est le seul champ qui doit exister **avant** le premier
partage : une fois des modules dans la nature, on ne peut plus l'ajouter
rétroactivement.

Un manifeste dont la `manifest_version` n'est pas connue de l'instance est
**refusé avec un message explicite**, jamais monté en espérant que ça passe.

`epure_min` est informatif en v1 (affiché, pas bloquant) : il n'existe pas
encore de version d'Épure publiée à comparer.

### 2.2 — L'index

Un fichier unique dans un dépôt GitHub public, par exemple
`ilyann/epure-modules/index.json`. Publier = ouvrir une PR qui ajoute une entrée.

```json
{
  "index_version": 1,
  "modules": [
    {
      "id": "pomodoro",
      "nom": "Pomodoro",
      "auteur": "prenom",
      "description": "Minuteur de travail avec statistiques",
      "source": "https://github.com/prenom/epure-pomodoro",
      "ref": "v1.2.0",
      "sha256": "3f2a…",
      "manifest_version": 1
    }
  ]
}
```

Pas de serveur, pas de compte, pas d'API à maintenir. La revue de la PR **est**
la modération, et c'est le seul niveau de modération que 5-10 personnes
justifient.

### 2.3 — Le paquet

Archive `tar.gz` du tag, récupérée sur `codeload.github.com` — pas de `git`
requis sur le poste. Les trois fichiers **à la racine** de l'archive (après
suppression du dossier de tête que GitHub ajoute) :

```
manifest.json
router.py
Component.tsx
```

Rien d'autre n'est extrait. Un `README.md` dans l'archive est simplement ignoré,
un quatrième fichier `.py` aussi — le contrat « exactement 3 fichiers » de
CLAUDE.md §3.3 tient parce qu'on n'en copie que trois, pas parce qu'on fait
confiance à l'archive.

---

## §3 — Étapes

### Étape A — `manifest_version`, seule

*À faire même si le registre ne se fait jamais.* C'est le seul geste dont
l'oubli est irréversible.

- Ajouter `"manifest_version": 1` aux manifestes du cœur, de `hello` et des six
  du catalogue.
- `core/module_registry.py` : constante `MANIFEST_VERSIONS_SUPPORTÉES = {1}`.
  Un manifeste sans le champ est traité comme `1` (rétrocompatibilité des
  modules générés existants) ; un manifeste avec une version inconnue est
  **ignoré au montage**, avec un `logger.warning` qui nomme le module et la
  version attendue.
- Test : un manifeste `"manifest_version": 99` n'est pas monté et le message le
  dit.

Commit : `feat(modules): version de contrat dans les manifestes`

### Étape B — Réception d'un paquet, **hors ligne**

Tout ce qui est difficile est ici, et rien n'a besoin du réseau. C'est ce qui
rend l'étape testable en CI sans mock HTTP.

`backend/core/registre.py`, une fonction :

```python
def recevoir(archive: Path) -> dict:
    """Valide une archive de module et l'installe dans modules-catalogue/."""
```

Séquence, dans cet ordre :

| # | Contrôle | Refus si |
|---|---|---|
| 1 | Taille de l'archive | > 5 Mo |
| 2 | Extraction dans un temporaire | un membre dont le chemin résolu sort du temporaire (*zip-slip*) ; un lien symbolique ; un membre non-fichier |
| 3 | Présence des 3 fichiers | l'un manque |
| 4 | Lecture du manifeste (`utf-8-sig`, via `jsonstore`) | illisible |
| 5 | `_check_module_id(manifest["id"])` | id douteux, et l'id de l'archive doit être **celui du manifeste** |
| 6 | `manifest_version` ∈ supportées | sinon |
| 7 | Collision | l'id existe déjà dans `modules-catalogue/` (une mise à jour est une autre opération, cf. §5) |
| 8 | `module_validate.validate_router_py(router.py)` | rapport non ok |
| 9 | Réécriture des champs de confiance | — (`core_module: false`, `removable: true`, `origin: "registre"`, `backend.prefix: ""`) |
| 10 | Copie des 3 fichiers vers `modules-catalogue/<id>/` | — |

Le `tsc` sur `Component.tsx` n'est **pas** fait ici : il coûte plusieurs
secondes, il exige `npx`, et le lot 6 du durcissement (`docs/catalogue-modules.md`
étape E) le rend déjà bloquant à l'approbation Atelier. À trancher : soit on
l'ajoute ici en best-effort, soit on assume qu'un `Component.tsx` cassé fait
échouer le build frontend de façon visible. **Recommandation : best-effort avec
avertissement affiché**, pas bloquant — un échec d'outillage ne doit pas
empêcher d'installer un module dont le backend est sain.

Tests obligatoires, pas optionnels — c'est un point d'entrée qui prend un
fichier venant de l'extérieur :

- archive contenant `../../../evil.py` → refus, rien n'est écrit hors du temporaire
- archive contenant un lien symbolique → refus
- `id` valant `../chat` → `SecurityError`
- manifeste `"core_module": true` → accepté mais **réécrit à `false`**, vérifié sur le fichier posé
- manifeste `"backend": {"prefix": "/"}` → réécrit à `""`
- `router.py` contenant `import subprocess` → refus
- `manifest_version: 99` → refus
- id déjà présent dans le catalogue → refus

Commit : `feat(registre): réception et validation d'un paquet de module`

### Étape C — Le téléchargement

`POST /settings/registre/install` dans le routeur `settings`, corps
`{source, ref, sha256}`.

- **Allowlist d'hôtes** : `github.com`, `codeload.github.com`. Sans elle,
  l'endpoint est un SSRF déclenchable par quiconque a le token — il ferait
  émettre au backend une requête vers n'importe quelle URL, y compris
  `http://localhost:11434` ou une IP du LAN. L'allowlist est le contrôle, pas la
  bonne volonté de l'appelant.
- **Le sha256 est vérifié sur les octets téléchargés, avant toute extraction.**
  Décompresser puis vérifier, c'est avoir déjà exécuté le code de décompression
  sur des octets non vérifiés.
- Timeout court (30 s), taille max appliquée pendant le téléchargement et pas
  après (un `Content-Length` menteur ne doit pas remplir le disque).
- Puis `registre.recevoir(archive)`. L'endpoint ne valide rien lui-même.

`GET /settings/registre` récupère et met en cache l'`index.json`. Cache
obligatoire : même piège que `engines_status` (13,6 s → caché, commit résolu
dans `module_workshop.py`) — ne pas refaire un appel réseau à chaque ouverture
de l'écran Réglages.

Commit : `feat(registre): installation depuis une URL`

### Étape D — L'écran de confiance

Réglages › Registre. La liste vient de l'index. Et surtout, **avant
installation** :

- le `router.py` affiché **intégralement**, avec son nombre de lignes ;
- l'auteur, la source, le tag, le sha256 ;
- une phrase qui ne ment pas :

> Ce module a été écrit par *prenom* et n'a été relu par personne. Une fois
> installé, ses *N* lignes de Python s'exécutent dans Épure avec vos clés API et
> l'accès à vos fichiers.

Le bouton « Installer » n'apparaît qu'après défilement jusqu'au bas du code.
Ce n'est pas de la cosmétique : tant que l'isolation worker n'est pas en vigueur
(CLAUDE.md §7), **la revue humaine est le seul contrôle réel**. Un bouton en un
clic supprimerait le dernier.

Commit : `feat(settings): écran registre avec revue du code avant installation`

### Étape E — Publier

Sans elle, le registre reste vide — c'est la moitié qu'on oublie.

Atelier › Publier, ou `GET /workshop/{id}/package` :

- produit `<id>-<version>.tar.gz` avec les trois fichiers à la racine ;
- affiche le sha256 ;
- affiche le bloc JSON prêt à coller dans une PR sur l'index.

Commit : `feat(atelier): empaqueter un module pour publication`

---

## §4 — Ordre et point de stabilité

A → B → C → D → E. **Ne pas entamer C avant que B soit vert** : le jour où le
réseau entre dans la boucle, un échec devient ambigu (paquet invalide ? DNS ?
tag inexistant ?). B seule, hors ligne, se débogue.

A est indépendante et peut partir tout de suite.

---

## §5 — Hors périmètre v1, explicitement

À ne pas déduire de ce document :

- **Mise à jour d'un module installé.** Un module reste sur la version reçue.
  Mettre à jour suppose de comparer les versions, de sauvegarder, de gérer un
  module modifié localement depuis — c'est un chantier propre. En v1 : désinstaller
  puis réinstaller.
- **Dépendances entre modules.** Un module ne peut pas en exiger un autre.
- **Signature cryptographique.** Écartée pour un cercle restreint : le canal de
  confiance est déjà hors bande (tu connais les gens, tu as leur GitHub). Elle
  redevient nécessaire dès que quelqu'un installe un module d'un inconnu.
- **Isolation.** Un module du registre s'exécute avec `os.environ` (clés API),
  l'accès au token d'instance et l'objet `app`, comme tes propres modules
  générés (CLAUDE.md §7). **C'est acceptable ici uniquement parce que le cercle
  est restreint** — la confiance porte sur les personnes. Elle ne porte pas sur
  ce que leur LLM a écrit, et c'est la faiblesse assumée de ce plan. Le jour où
  un module circule hors du cercle, l'isolation redevient bloquante.
- **Docker.** L'installation écrit dans `frontend/src/modules/generated/`, donc
  elle suppose le serveur de dev. Limite héritée du catalogue
  (`docs/catalogue-modules.md` §0), pas aggravée par le registre.

---

## §6 — Ce qui n'a pas été vérifié

- **`module_validate.validate_router_py` est supposé appelable hors du flux
  Atelier** (signature, valeur de retour, effets de bord). Non vérifié :
  `module_validate.py` n'a pas été lu. À confirmer en premier à l'étape B — si
  la fonction dépend d'un contexte de staging, l'étape change de forme.
- **`modules/settings/router.py` n'a pas été lu.** Le style des endpoints
  existants, la gestion d'erreur et les modèles de réponse sont supposés.
- **`module_workshop.py` (78 Ko) n'a pas été lu.** Les helpers importés par
  `catalogue.py` sont connus par leur usage dans `catalogue.py`, pas par leur
  implémentation.
- **Résidu constaté, non expliqué** : `backend/modules/code/`, `docs/`,
  `flashcards/`, `kholle/`, `rangement/`, `reviseur/` ne contiennent plus que
  `__pycache__/` — plus de `manifest.json` ni de `router.py`. Soit ces modules
  ont été désinstallés et `uninstall()` laisse le dossier `__pycache__` derrière
  lui, soit autre chose. À élucider avant l'étape B : un dossier résiduel change
  le résultat du contrôle « id déjà présent ».

---

## §7 — Prompts pour Claude Code

### Étape A

> Lis `docs/registre-modules.md`, étape A, et `CLAUDE.md` §3.3. Ajoute
> `"manifest_version": 1` à tous les manifestes de `backend/modules/*/` et de
> `modules-catalogue/*/`. Dans `core/module_registry.py`, ajoute
> `MANIFEST_VERSIONS_SUPPORTÉES = {1}` : un manifeste sans le champ vaut 1
> (rétrocompatibilité des modules générés déjà installés), un manifeste avec une
> version hors de l'ensemble n'est pas monté par `register_routers` et produit un
> `logger.warning` nommant le module et la version attendue. N'ajoute **aucun**
> stockage d'état : la version se lit sur le manifeste, comme l'état « installé ».
> Un test qui vérifie qu'un manifeste `"manifest_version": 99` n'est pas monté.
> Commit : `feat(modules): version de contrat dans les manifestes`.

### Étape B

> Lis `docs/registre-modules.md` §0, §1 et étape B. Écris
> `backend/core/registre.py` avec `recevoir(archive: Path) -> dict`, qui valide
> une archive `tar.gz` de module et la pose dans `modules-catalogue/<id>/`.
> **Aucun réseau dans ce fichier** — il prend un chemin local, c'est ce qui le
> rend testable en CI.
>
> Commence par vérifier que `core/module_validate.validate_router_py` est
> appelable hors du flux Atelier ; si elle exige un contexte de staging, dis-le
> avant d'écrire le reste plutôt que de contourner.
>
> Réutilise `_check_module_id` de `module_workshop` et `core/jsonstore` pour lire
> le manifeste. N'écris **aucune** logique de copie vers `backend/modules/` ni
> vers `frontend/` : `core/catalogue.py:install()` s'en charge et ne doit pas
> être modifié — son invariant devient « tout ce qui est dans
> `modules-catalogue/` a été validé ».
>
> Les champs `core_module`, `removable`, `origin` et `backend.prefix` sont
> **réécrits**, jamais lus depuis l'archive : le §1.2 du document explique
> pourquoi (un manifeste déclarant `core_module: true` rendrait le module
> indésinstallable par `catalogue.uninstall()`).
>
> Écris `backend/test_registre.py` couvrant les huit cas listés dans le document,
> zip-slip et lien symbolique compris. Élucide au passage pourquoi
> `backend/modules/code/`, `docs/`, `flashcards/`, `kholle/`, `rangement/`,
> `reviseur/` ne contiennent plus que `__pycache__/` — si `uninstall()` laisse ce
> dossier, corrige-le dans un commit séparé.
> Commit : `feat(registre): réception et validation d'un paquet de module`.

### Étape C

> Étape C. `POST /settings/registre/install` et `GET /settings/registre` dans
> `backend/modules/settings/router.py`. Allowlist d'hôtes stricte
> (`github.com`, `codeload.github.com`) — sans elle l'endpoint est un SSRF
> déclenchable par quiconque a le token. Le sha256 est vérifié **sur les octets
> téléchargés, avant extraction**. Taille max appliquée pendant le
> téléchargement, pas après. `GET /settings/registre` met l'index en cache, sur
> le modèle du cache d'`engines_status` dans `module_workshop.py`.
> L'endpoint ne valide rien lui-même : il délègue à `registre.recevoir`.
> Tests avec HTTP mocké, sur le modèle de `test_web_search.py`.
> Commit : `feat(registre): installation depuis une URL`.

### Étapes D et E

> Étape D. Écran Réglages › Registre. Avant toute installation, le `router.py`
> est affiché intégralement et le bouton « Installer » n'apparaît qu'après
> défilement jusqu'au bas du code. Affiche l'auteur, la source, le tag, le
> sha256, le nombre de lignes, et la phrase du §3 étape D telle quelle. Tant que
> l'isolation worker n'est pas en vigueur (CLAUDE.md §7), cette revue est le seul
> contrôle réel — ne la rends pas contournable en un clic.
> Commit : `feat(settings): écran registre avec revue du code avant installation`.
>
> Étape E. `GET /workshop/{id}/package` : produit `<id>-<version>.tar.gz` avec
> les trois fichiers à la racine, affiche le sha256 et le bloc JSON prêt à coller
> dans une PR sur l'index. Commit :
> `feat(atelier): empaqueter un module pour publication`.
