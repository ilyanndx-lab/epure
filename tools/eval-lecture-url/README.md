# Éval du budget de lecture d'URL (#75)

Cette évaluation aide à fixer le plafond de `core.webcontent.budget_lecture`.
Elle porte sur 10 pages Wikipédia figées en fixtures (`pages/*.html.gz`, sous
licence CC BY-SA, téléchargées le 2026-10-19) et sur une question précise par
page, dont la réponse se trouve entre 46 et 73 % du texte extrait
(`questions.json`). Elle n'est **pas câblée en CI**.

Elle se fait en deux étages :

- **Étage 1, sans LLM.** Pour chaque question et chaque budget, l'extraction
  passe par le code réel de #75 (passages, embedding local partagé, sélection
  au budget). On vérifie ensuite que **tous** les `fragments` de la réponse
  attendue figurent dans l'extrait. La correspondance est normalisée : casse,
  accents, apostrophes, espaces, et espaces autour de la ponctuation.
- **Étage 2, avec LLM.** Le contexte système réel du chat est envoyé à LM
  Studio, raisonnement coupé (`reasoning_effort: "none"`, le seul réglage
  mesuré efficace sur Qwen3.5). On l'exécute aux budgets 4 000 et 16 000,
  uniquement sur les questions où l'étage 1 diffère entre ces deux budgets ou
  échoue à 4 000. Les résultats s'écrivent au fil de l'eau dans
  `resultats/resultats.json`. La notation est manuelle.

## Prérequis

- Le venv du backend (`.venv`), avec le modèle d'embedding installé : sans lui,
  la sélection retombe sur l'ordre de la page, et l'étage 1 le signale.
- Pour l'étage 2 : LM Studio sur le port 1234 (`$LMSTUDIO_HOST` sinon), avec
  `huihui-qwen3.5-9b-abliterated` chargé à 8 192 tokens.
- **Conditions de l'étage 2** :
  - portable **branché** : sur batterie, on mesure environ 5 tokens/s au lieu
    d'environ 20 ;
  - **MATLAB fermé** ;
  - au moins 3 Go de mémoire libre **une fois le modèle chargé**. En dessous,
    l'étage 2 s'arrête proprement : résultats partiels écrits, modèle
    déchargé avec `--decharger`.

## Commandes (Windows, depuis la racine du dépôt)

```powershell
.\.venv\Scripts\Activate.ps1
python tools\eval-lecture-url\eval.py etage1
lms load huihui-qwen3.5-9b-abliterated --context-length 8192 -y
python tools\eval-lecture-url\eval.py etage2 --modele huihui-qwen3.5-9b-abliterated --decharger
```

Les données de l'instance (mémoire, historique, store vectoriel) pointent vers
un dossier temporaire. Seul le modèle d'embedding réel est lu.

## Résultats de référence

### Étage 1 (2026-10-19) : `resultats/etage1.json`

Chaque case indique si la réponse est présente, puis la taille réelle de
l'extrait en caractères. La sélection s'est faite par pertinence partout :
l'embedding était disponible.

| Question | 4 000 | 8 000 | 16 000 |
|---|---|---|---|
| photosynthese | ✅ 3 714 | ✅ 7 987 | ✅ 15 955 |
| relativite_restreinte | ❌ 3 848 | ❌ 7 239 | ✅ 15 454 |
| fourier | ✅ 3 810 | ✅ 7 365 | ✅ 15 629 |
| turing | ✅ 3 432 | ✅ 7 869 | ✅ 15 611 |
| mitochondrie | ✅ 3 677 | ✅ 7 469 | ✅ 15 998 |
| photoelectrique | ❌ 3 354 | ❌ 7 185 | ✅ 14 984 |
| nombre_premier | ✅ 3 642 | ✅ 7 995 | ✅ 15 488 |
| entropie | ✅ 3 648 | ✅ 7 979 | ✅ 15 850 |
| revolution_industrielle | ✅ 3 718 | ✅ 7 361 | ✅ 15 908 |
| trou_noir | ✅ 3 952 | ✅ 7 616 | ✅ 15 837 |
| **Présente** | **8/10** | **8/10** | **10/10** |

Ce qui manque :
- relativite_restreinte : « référentiel de Lorentz ». Le passage du glossaire
  n'est pas assez proche de la question.
- photoelectrique : les deux fragments.

### Étage 2 (2026-10-19) : `resultats/resultats.json`

Conditions : modèle chargé **en local** (ilyann1) à 8 192 tokens, raisonnement
coupé (0 token de raisonnement à chaque essai). La mémoire libre est restée
entre 3,6 et 4,9 Go. L'essai s'est fait **sur batterie, avec MATLAB ouvert** :
les délais sont donc pessimistes. La notation est manuelle et se fait sur les
réponses brutes du fichier.

| Question | Budget | Extrait | 1er token | Prompt | Sortie |
|---|---|---|---|---|---|
| relativite_restreinte | 4 000 | 3 848 | 12,6 s | 1 057 | 307 |
| relativite_restreinte | 16 000 | 15 454 | 39,9 s | 3 923 | 132 |
| photoelectrique | 4 000 | 3 354 | 14,5 s | 1 224 | 234 |
| photoelectrique | 16 000 | 14 984 | 67,6 s | 5 044 | 111 |

**Piège LM Link** : `lms load … -y` peut charger le modèle sur l'appareil
relié (ilyann2) plutôt qu'en local. Vérifier la colonne `DEVICE` de
`lms ps`. Sur ilyann2, le 2026-10-19 :
- un essai a ignoré `reasoning_effort: "none"` (1 024 tokens de raisonnement,
  réponse vide) ;
- le modèle y a ensuite été déchargé en plein flux.

Le harnais signale désormais ces cas (`anomalie`) au lieu de les compter comme
des essais.
