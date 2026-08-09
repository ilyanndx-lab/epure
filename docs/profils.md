# Profils d'instance — plan d'exécution

**Contexte.** Épure est câblée prépa scientifique : dossiers surveillés
`Maths/`, `Physique-Chimie/`, `SI/`, modules `kholle` et `reviseur`, vocabulaire
de kholle et de fiches. Le catalogue rend les *modules* pluggables ; le reste ne
l'est pas. Un proche qui installe Épure hérite du réglage d'Ilyann et doit tout
refaire à la main.

**Question posée : preset de configuration, ou verticales distinctes ?**

---

## §0 — La question est tranchée : preset

Méthode : lister les profils cibles réels et regarder ce qui diffère
*réellement*, pas ce qui paraît différent.

| Profil | Ce qui change par rapport à celui d'Ilyann |
|---|---|
| Ami en PCSI / PSI / BCPST | `fiches.watch_folders`, prompt système (programme), vocabulaire |
| Ami en école d'ingé ou en fac | idem + `modules_activés` différent (ni `kholle`, ni `reviseur`) |
| Lycéen | idem, vocabulaire plus simple |
| **Ilyann au travail (Cloudiway)** | `fiches.racine` sur des documents pro, pas de `reviseur`, politique de modèles différente |

Le profil le plus éloigné est le dernier, et **même lui ne retire aucun module
du cœur** : `chat`, `history`, `settings`, `admin` restent identiques, le moteur
RAG est le même, simplement pointé ailleurs.

Un refactor en verticales ne se justifierait que si un profil exigeait de
retirer ou de remplacer un module du cœur. Aucun ne le demande. **Verdict :
preset. La question est fermée**, et elle doit le rester — la rouvrir sans qu'un
profil concret exige un cœur différent serait un refactor sans besoin.

---

## §1 — Ce que le code permet déjà, vérifié

Lecture de `backend/core/instance.py` au 2026-08-09. Trois constats qui rendent
ce chantier beaucoup plus petit qu'il n'en a l'air.

**1. Presque tout ce qu'un profil doit changer est déjà dans
`instance_config`** — et pas dans `config.yaml`, qui « ne garde que les réglages
techniques : whisper, chunk_size… » (docstring du module) :

```
nom_affiché · modules_activés · providers.actif · providers.local
fiches.racine · fiches.watch_folders · thème · atelier.*
```

**2. `InstanceConfig.update(partial)` fait déjà exactement le bon geste** :
merge partiel, fusion récursive des sous-dictionnaires, écriture sous
`jsonstore.transaction()`, et refus des clés immuables (`instance_id`, `auth`).

> **Conséquence : un profil est littéralement un patch partiel stocké.**
> L'appliquer, c'est `instance_config.update(patch)`. Il n'y a pas de moteur à
> écrire.

**3. Changer les dossiers surveillés ne demande pas de redémarrage.**
`fiches_watch_paths()` relit `instance_config.get()` à chaque appel — rien n'est
figé à l'import. (À nuancer : le *contenu* déjà indexé dans ChromaDB, lui, ne
bouge pas. Changer de profil ne réindexe pas. Voir §4.)

**Un piège de nommage, à ne pas rater** : `instance_config` contient déjà une
clé `preset_défaut`, qui désigne les presets de `core/orchestrator.py` —
autre chose. **Ne jamais appeler ces objets « presets ».** Le mot du domaine est
**profil**.

---

## §2 — Le piège structurel : ne pas créer un second état

C'est le seul vrai risque de ce chantier, et il a déjà été payé une fois dans ce
dépôt (`modules_state.json`, CLAUDE.md §3.3).

Un profil est **un modèle, pas un état**. La tentation est d'écrire
`profil_actif: "prepa"` puis de faire lire ce champ par le code pour décider
quoi que ce soit. Ce serait exactement le doublon supprimé : deux sources pour
la même notion, qui divergent mécaniquement dès qu'on modifie un réglage à la
main après avoir appliqué un profil.

**Règle IMPÉRATIVE :**

| | |
|---|---|
| Appliquer un profil | une **action** : un `instance_config.update()` unique et ordonné |
| `profil_actif` | **purement informatif** — affiché dans l'UI, jamais lu pour décider d'un comportement |
| Source de vérité | `instance_config`, comme aujourd'hui. Inchangée. |

Corollaire : après application, l'utilisateur peut modifier n'importe quel
réglage. `profil_actif` devient alors un souvenir (« parti de *prepa* »), pas une
description. C'est voulu, et l'UI doit le dire — un libellé
« profil *prepa*, modifié » plutôt que de prétendre que la config *est* le
profil.

---

## §3 — Format et application

### 3.1 — Un profil

`backend/profils/<nom>.json`, versionnés dans le dépôt :

```json
{
  "profil_version": 1,
  "nom": "prepa",
  "libellé": "Prépa scientifique (MPSI/MP)",
  "description": "Fiches par matière, kholles, révisions espacées.",

  "modules": ["chat", "kholle", "flashcards", "reviseur", "docs",
              "history", "admin", "settings"],

  "config": {
    "nom_affiché": "Épure",
    "fiches": { "watch_folders": ["Maths", "Physique-Chimie", "SI"] },
    "thème": "dark"
  }
}
```

- `modules` est **ordonné** : c'est l'ordre de la barre.
- `config` est un patch partiel, passé tel quel à `instance_config.update()`.
- `fiches.racine` n'est **pas** dans les profils versionnés : c'est un chemin
  propre à la machine. Un profil qui en pose un serait faux chez tout le monde
  sauf son auteur.

### 3.2 — Appliquer

```python
def appliquer(nom: str) -> dict:
    """Écrit le profil dans instance_config. Retourne un rapport."""
```

Séquence :

1. Lire le profil (`jsonstore`), vérifier `profil_version`.
2. Partitionner `modules` : **installés** vs **absents**.
3. Écrire **en une seule fois** :
   `instance_config.update({**profil["config"], "modules_activés": [installés, dans l'ordre du profil]})`
4. Retourner la liste des **absents** pour que l'UI les propose à l'installation.

Trois points sur cette séquence :

- **Une seule écriture, pas N appels à `set_status`.** `set_status` ajoute
  toujours **en fin de liste** (docstring explicite). Appliquer un profil par N
  appels successifs donnerait l'ordre d'appel, pas l'ordre du profil, et ferait
  N transactions là où une suffit. Écrire la liste entière est légitime :
  `PUT /instance/config` le fait déjà pour le réordonnancement au
  glisser-déposer, et `_garder_settings()` réinjecte `settings` au point
  d'écriture quoi qu'il arrive.
- **Les modules absents ne sont jamais installés automatiquement.** Installer,
  c'est exécuter du code tiers ; ça reste une décision explicite. Le profil les
  *propose*.
- **Filtrer les absents avant d'écrire**, plutôt que de laisser `active_ids()`
  les filtrer à la lecture. Le filtre de lecture existe et couvrirait le cas,
  mais écrire des ids fantômes dans la config recrée précisément ce que la
  migration (b) passe son temps à purger.

### 3.3 — Endpoints

| Route | Contrat |
|---|---|
| `GET /settings/profils` | liste des profils, chacun avec `actif: bool` et le décompte de modules manquants |
| `POST /settings/profils/{nom}/appliquer` | applique, retourne `{modules_manquants: [...]}` |

`{nom}` vient du client : `_ID_RE` puis confinement par `resolve()` +
`is_relative_to()`, comme partout ailleurs (CLAUDE.md §3.5). Un nom de profil
est un nom de fichier.

### 3.4 — Les quatre profils de départ

`prepa` (l'actuel, extrait tel quel de la config d'Ilyann), `etudiant`,
`pro`, `minimal` (cœur seul — utile pour vérifier qu'un cœur nu démarre, ce que
rien ne teste aujourd'hui).

---

## §4 — Hors périmètre v1

- **Le vocabulaire.** Remplacer « kholle » par autre chose suppose une couche de
  libellés que rien ne porte aujourd'hui : il faudrait toucher chaque composant.
  Un profil v1 change la configuration, pas les textes de l'interface. C'est la
  limite la plus visible de ce plan — autant la dire.
- **Le prompt système.** Il devrait être dans un profil, mais **je n'ai pas
  localisé où il vit** (`config.yaml` ? `core/llm.py` ? `modules/chat/router.py` ?).
  À déterminer avant de l'ajouter au format — pas à deviner.
- **La réindexation RAG.** Changer `watch_folders` change ce qui sera indexé
  ensuite, pas ce qui l'est déjà dans ChromaDB. Un profil ne purge rien. Il faut
  soit le documenter, soit ajouter une action de réindexation explicite — pas
  l'enchaîner en silence, une réindexation coûte plusieurs minutes.
- **Profils par utilisateur.** Un seul profil actif par instance. Aller plus loin
  casserait l'invariant « mono-utilisateur, pas de rôles » de CLAUDE.md §1 et
  ouvrirait la question de l'isolation des données — un autre chantier, bien plus
  grand.

---

## §5 — Ce qui n'a pas été vérifié

- `backend/config.yaml` n'a pas été lu. Le partage exact entre `config.yaml` et
  `instance_config` est pris de la docstring de `core/instance.py`, pas constaté.
- `modules/settings/router.py` n'a pas été lu : l'existence et la forme de
  `PUT /instance/config` sont déduites des docstrings de `core/instance.py`
  (`_garder_settings` mentionne « `update()` accepte une liste entière venant du
  client »), pas vues.
- Aucune mesure du coût d'un changement de `watch_folders` sur un index ChromaDB
  déjà peuplé.

---

## §6 — Prompts pour Claude Code

### Le cœur du chantier

> Lis `docs/profils.md` en entier, puis `CLAUDE.md` §3.3 et la docstring de
> `core/instance.py`.
>
> Écris `backend/core/profils.py` : `lister()` et `appliquer(nom)`. Un profil est
> un patch partiel stocké dans `backend/profils/<nom>.json`, appliqué par un
> **unique** `instance_config.update()`.
>
> Trois contraintes non négociables, chacune expliquée dans le document :
>
> 1. **Aucun second état.** `profil_actif` est informatif et n'est lu par aucune
>    décision de comportement. La source de vérité reste `instance_config`. Si tu
>    te surprends à écrire un `if profil_actif == …`, arrête-toi : c'est
>    `modules_state.json` qui revient (CLAUDE.md §3.3).
> 2. **Une seule écriture**, pas N appels à `set_status` — qui ajoute en fin de
>    liste et perdrait l'ordre du profil.
> 3. **Les modules absents ne sont jamais installés automatiquement.** Ils sont
>    retournés à l'appelant, filtrés avant écriture.
>
> Ne mets pas `fiches.racine` dans les profils versionnés : c'est un chemin
> propre à la machine.
>
> Attention au nommage : `instance_config` contient déjà `preset_défaut`, qui
> désigne les presets de `core/orchestrator.py`. N'emploie jamais « preset » pour
> ces objets.
>
> Écris les quatre profils de départ (`prepa` extrait de la config actuelle,
> `etudiant`, `pro`, `minimal`) et `backend/test_profils.py` : application
> idempotente, ordre de la barre respecté, modules absents non installés et bien
> remontés, `settings` toujours présent après application même si le profil
> l'omet, nom de profil `../evil` refusé.
> Commit : `feat(profils): profils d'instance appliqués en une écriture`.

### L'UI

> Réglages › Profils. Une carte par profil : libellé, description, décompte de
> modules manquants, bouton « Appliquer ». Après application, affiche les modules
> manquants avec un lien vers le catalogue — **ne les installe pas**.
>
> Le libellé du profil actif doit dire la vérité : « profil *prepa* » tant que la
> config correspond, « profil *prepa*, modifié » dès qu'un réglage a bougé. Le
> profil est un point de départ, pas une description de l'état courant.
> Commit : `feat(settings): écran profils`.
