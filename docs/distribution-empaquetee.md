# Distribution empaquetée — le proche n'installe plus d'outils de développement

**Objectif.** Un proche télécharge une archive, la dézippe, lance un installeur minimal,
double-clique un raccourci. Aucun git, aucun Node, aucun Python visible. Il active ou
désactive dans ses Réglages uniquement les modules qu'Ilyann lui a envoyés — jamais un
catalogue commun, jamais les modules faits pour quelqu'un d'autre.

**Ce qui change de fond.** Jusqu'ici, l'installeur (`docs/installeur.md`) posait
l'environnement de développement complet chez le proche, parce qu'il était censé créer
ses propres modules via l'Atelier. Ce n'est plus le cas : **Ilyann crée les modules,
le proche installe et utilise.** La conséquence directe : l'exécutable packagé, écarté
dans `docs/installeur.md` §5 au motif que « l'Atelier a besoin d'un environnement Python
éditable », perd sa principale raison d'être écarté. Il en reste une seule : la taille.
Une taille de téléchargement n'est pas un blocage, c'est un inconvénient à annoncer.

**Ce que ça ne résout pas.** Le mur rencontré le 2026-08-10 (`chroma-hnswlib`, `grpcio` :
aucune wheel `win_arm64`) ne disparaît pas, il se déplace. Construire le paquet pour une
architecture donnée demande de le construire *sur* cette architecture. Ilyann redevient
nécessaire une fois, comme poste de build — plus jamais comme dépanneur en direct chez
le proche.

**Précisions fixées après relecture (à ne pas redemander) :**
- **Python visé : 3.12**, aligné sur `docs/installeur.md` étape B et sur la CI.
- **torch (et les modèles du pipeline RAG) ne sont pas embarqués dans le paquet.**
  Téléchargés au premier lancement, même logique que le modèle vocal Piper
  (`docs/purge-onnx.md`) : le paquet initial reste léger ; le prix est qu'une connexion
  réseau est nécessaire au premier usage du RAG documentaire, ce qui n'est pas une
  contrainte nouvelle — `install.ps1` tire déjà le modèle Ollama en ligne.

---

## §0 — Ce qui existe et doit être vérifié avant tout code

Rien ci-dessous ne doit être supposé — chaque point conditionne la faisabilité du reste.

- **Le frontend construit (`npm run build`) peut-il être servi tel quel par FastAPI ?**
  Vérifier si le routing SPA (mode history) nécessite une route de repli côté FastAPI
  (`StaticFiles` + catch-all vers `index.html`), et si le frontend fait des appels API en
  chemin relatif ou en URL absolue codée en dur pour le dev (`localhost:8000`).

- **Le Python embeddable (distribution officielle python.org, 3.12) supporte-t-il les
  extensions natives sans réglage caché ?** L'embeddable désactive `site-packages` par
  défaut (il faut décommenter la ligne `import site` dans son `python312._pth`) et
  n'embarque ni `pip` ni `distutils`. À tester isolément — installer `chroma-hnswlib` et
  les extensions natives listées ci-dessous dans un embeddable, avec `pip` amené à part
  (`get-pip.py`), et vérifier qu'elles s'importent — **avant** de construire tout un
  installeur dessus. Si ça échoue, c'est le point qui invalide l'approche entière.
  Paquets de `backend/requirements.txt` à extension native, donc décisifs pour ce test :
  `chromadb` (→ `chroma-hnswlib`), `sentence-transformers` (→ torch, hors paquet — voir
  ci-dessus), `faster-whisper` (→ `ctranslate2`, `onnxruntime`), `pandas`, `Pillow`,
  `piper-tts`.

- **Poids réel du paquet sans torch.** Mesurer la taille de `site-packages` une fois
  torch exclu (téléchargé à la demande) et zippé. Une mesure, pas une estimation.

- **Périmètre exact de l'Atelier à retirer.** Lister les routes et composants concernés
  (`module_workshop.py`, les écrans du front associés) pour savoir ce qui doit être
  masqué dans le paquet livré, sans toucher à l'instance de développement d'Ilyann, qui
  garde tout.

---

## §1 — Étapes

### Étape A — Frontend construit, servi par FastAPI

Remplace, **pour la distribution seulement**, les deux processus (Vite + uvicorn) par un
seul. Le mode développement d'Ilyann ne change pas — Vite reste utile pour l'Atelier, qui
n'existe que chez lui.

Vérification : `npm run build`, servir le résultat par FastAPI, ouvrir dans un navigateur
propre (cache vidé), naviguer sur toutes les routes du front y compris par URL directe.

### Étape B — Constituer un paquet pour un destinataire donné

Un script **côté Ilyann, jamais livré** : prend une liste de modules choisis pour cette
personne, copie le backend (Atelier retiré ou désactivé), le frontend construit, un
`site-packages` pré-installé (Python embeddable 3.12, torch exclu), une configuration
minimale, et zippe le tout. Vit dans un dossier séparé (`tools/` ou dépôt à part), pas
dans ce qui part chez le proche.

### Étape C — Installeur minimal pour le proche

Dézippe l'archive, installe Ollama s'il est absent, tire le modèle, pose un raccourci
Bureau. Plus de détection Python/Node/git.

Vérification : sur une machine où rien n'est installé, mesurer le temps et le nombre
d'étapes réellement franchies sans intervention manuelle.

### Étape D — Réglages du proche : catalogue restreint

L'écran Réglages n'affiche et ne permet d'activer/désactiver que les modules présents
dans **son** paquet. Deux personnes avec des paquets différents ne doivent voir aucune
trace l'une de l'autre.

Test à casser exprès : vérifier qu'un module absent du paquet n'apparaît nulle part,
même par un appel API direct à l'endpoint de statut.

### Étape E — Documentation

`docs/partage.md` et `docs/installation-chez-un-proche.md` sont à réécrire en
profondeur : git et winget côté proche disparaissent entièrement du parcours.

---

## §5 — Hors périmètre, et pourquoi

**Exécutable unique (PyInstaller/Nuitka).** Écarté au profit d'un dossier autonome +
Python embeddable : moins de risques d'imports cachés cassés silencieusement sur une
stack avec extensions natives.

**Build ARM64.** Reporté tant qu'aucun proche confirmé n'est sur cette architecture
au-delà de sandr. À construire à la demande, sur une machine ARM empruntée.

**macOS et Linux pour les proches.** Non demandé à ce stade.

**Mécanisme de mise à jour automatique.** Le proche n'a plus de dépôt git. Une mise à
jour est une nouvelle archive renvoyée par Ilyann. Ne jamais écraser au passage : `.env`,
`memory/`, `chroma_db/`, `workspace/` — même règle que `install.ps1` aujourd'hui.

---

## §6 — Ce qui n'a pas été vérifié

- Le Python embeddable avec extensions natives : non testé, c'est le point qui peut
  faire échouer toute l'approche.
- Le poids réel du paquet final sans torch.
- La compatibilité du routing SPA avec `StaticFiles` + catch-all FastAPI.
- Le temps qu'Ilyann devra consacrer à reconstituer un paquet par destinataire — workflow
  manuel acceptable pour deux ou trois proches, à revoir si ça grandit.
