# Épure — Performance LM Studio : mesures du 2026-10-20

> Séance de mesure du 2026-10-20 (LM Studio, `huihui-qwen3.5-9b-abliterated`,
> contexte chargé 8 192). Bruts archivés dans
> `tools/eval-lecture-url/resultats/experiences/` ; rien ici n'est câblé dans
> le code. **Chaque chiffre vient d'UN passage** : ce sont des ordres de
> grandeur, pas des moyennes (cf. la limite statistique en fin de document).

---

## 1. `mmap` activé contre désactivé

Fichiers : `mmap-true.json`, `mmap-false.json`.

| | mmap activé | mmap désactivé |
|---|---|---|
| Temps de chargement | 22,3 s | 37,9 s |
| Mémoire « working set » de LM Studio | 11,95 Go | 2,54 Go |
| Mémoire privée de LM Studio | 7,59 Go | 8,11 Go |
| Mémoire libre une fois chargé | **0,84 Go** | **5,36 Go** |

- Avec `mmap`, le modèle est mappé depuis le disque : le chargement est plus
  court, mais la machine finit à 0,84 Go libres. Sans `mmap`, il est copié en
  mémoire privée : +15 s de chargement, mais ~4,5 Go de plus restent libres.
- Les deux états sont stables après une requête (`libre_apres_go` 5,07 Go,
  `lmstudio_ws_apres_go` 2,76 Go côté `mmap` désactivé).
- **Vitesse de génération non comparée** : `mmap-true.json` ne contient pas de
  requête. Le seul chiffre de vitesse (`mmap-false.json`) est 75,9 tok/s de
  lecture du prompt (1 280 tokens, 16,9 s) et 6,8 tok/s de génération (101
  tokens, 14,7 s), sans référence `mmap` activé pour le situer.
- `mmap-true.json` n'était pas dans la liste de la demande ; il est archivé
  parce que sans lui le tableau n'a pas de colonne de gauche.

## 2. 1 contre 4 requêtes parallèles

Fichiers : `slots-1.json`, `slots-4.json`. Même scénario aux deux réglages :
tour 1, tour 2 (prompt différent), tour 2 répété à l'identique, puis les mêmes
trois étapes avec la variante (§4).

**Vitesse de lecture du prompt** (`prompt_eval`, tokens / temps) :

| Essai | Tokens | 1 parallèle | 4 parallèles |
|---|---|---|---|
| ref tour 1 | 1 280 | 16,5 s (~77 tok/s) | 29,6 s (~43 tok/s) |
| ref tour 2 | 1 307 | 17,0 s (~77 tok/s) | 30,6 s (~43 tok/s) |
| var tour 1 | 2 041 | 31,0 s (~66 tok/s) | 43,3 s (~47 tok/s) |
| var tour 2 | 2 104 | 47,7 s (~44 tok/s) | 41,3 s (~51 tok/s) |

À 4 parallèles, la lecture du prompt est ~1,8× plus lente sur les deux essais
de référence. Le dernier essai (`var tour 2`) va à contre-sens (44 contre
51 tok/s) : à un seul passage on ne sait pas dire s'il s'agit de bruit.
Mémoire libre après chargement : 6,24 Go à 1 parallèle, 5,83 Go à 4.

**Rotation des emplacements (« slots »).** Le journal LM Studio dit, pour chaque
requête, quel emplacement a servi et pourquoi :

- à 4 parallèles, chaque nouveau prompt tombe sur un emplacement **vierge**
  différent (`LRU, n_tokens = 0` : 0, puis 1, puis 2, puis 3) ; seul le
  **même prompt rejoué à l'identique** retrouve son emplacement (`LCP
  similarity, f_sim_best = 1.000`, emplacement 1) ;
- à 1 parallèle, tout passe par l'emplacement 0, qui contient déjà le tour
  précédent (`LRU, n_tokens = 1295`, puis 1322, puis 2056) — et **pourtant le
  prompt entier est relu** au tour suivant (`retraites` = tous les tokens).

Conclusion pratique : l'emplacement n'est donc pas ce qui limite la
réutilisation entre deux tours ; avec 4 emplacements, on paie en plus une
lecture plus lente. Voir §3 pour ce qui est réutilisé, et quand.

## 3. Réutilisation du cache sur Qwen3.5

Observé dans `slots-1.json` et `slots-4.json` :

| Cas | Tokens relus |
|---|---|
| Tour 1 | 1 280 sur 1 280 |
| Tour 2 (prolonge la conversation, prompt différent) | 1 307 sur 1 307 |
| Tour 2 **rejoué à l'identique** | **4 sur 1 307** (0,3 à 0,5 s) |

**Interprétation, NON confirmée par le journal** : Qwen3.5 est un modèle
**hybride** (couches à état récurrent). Il ne peut pas rejouer un préfixe
partiel du cache comme un transformeur pur : seul un prompt qui **prolonge le
précédent à l'octet près** serait réutilisé, et encore, ici on n'a mesuré que
le cas « identique ». Le journal ne dit ni « hybride », ni « point de reprise
impossible » : on a le résultat (tout relu dès que le prompt diverge, même tard)
et l'hypothèse la plus simple, sans la preuve. À vérifier avant de bâtir une
optimisation dessus : un prompt qui ne fait qu'**ajouter** à la fin du précédent
(cas non mesuré ici) est le test qui départagerait.

## 4. Variante « données avant le dernier message » — abandonnée

Branche jetable `exp/contexte-avant-dernier-message`, **supprimée** après cet
archivage. Dernier commit : `2b34aad` (jamais poussé ; récupérable par
`git branch <nom> 2b34aad` tant que le reflog et le ramasse-miettes de git ne l'ont pas emporté).

Idée : garder au système les seules consignes (contrat de citation, sécurité)
et mettre les données variables du tour (extraits de page, résultats web,
chunks, date) en tête du **dernier message utilisateur**, pour que le préfixe
système reste identique d'un tour à l'autre. Envois comparés : `envois-main.json`
(données au système, 4 591 caractères au tour 1) et `envois-exp.json`
(système de 1 061 caractères, 6 305 caractères de données dans le message
utilisateur).

**Raison de l'abandon : incompatible avec #66.** Sur Qwen3.5, le cache n'est
réutilisé que si le prompt du tour n+1 prolonge celui du tour n à l'octet près
(§3). Pour que la variante en profite, il faudrait donc garder les extraits de
chaque tour dans l'historique, ce qui :

- **(a)** réinjecterait du contenu web non fiable à chaque tour suivant,
  contrairement à la règle de #66 (seules les métadonnées `sources` et la
  trace sont persistées) ;
- **(b)** gonflerait le contexte, à 8 192 tokens ;
- **(c)** retirerait les outils pour tout le reste de la conversation dès la
  première URL, puisqu'un tour qui contient du contenu web n'expose plus
  d'outils.

Mesure, pour la mémoire : même avec la variante, le système du tour 2 différait
du tour 1 (1 455 contre 1 061 caractères) — consigne « page non lue » ajoutée
parce que la lecture de Turing avait expiré. Le préfixe n'est donc pas resté
identique dans ce passage, et la relecture complète au tour 2 (§2, §3) ne
prouve rien pour ni contre la variante. Le tour 2 de la variante n'est de toute
façon pas comparable : le message utilisateur y contient « Recherche web :
Erreur de recherche web : <urlopen error tim… » au lieu du contenu de la page.

## 5. Notation de l'étage 2 (variante)

Fichier : `etage2-variante.json`. Conditions : sur secteur, raisonnement coupé,
8 192 de contexte, 2 questions × 2 budgets. Contexte de l'étage 1 (`README.md` : à 4 000, l'extrait
de `relativite_restreinte` ne contient pas « référentiel de Lorentz », et celui
de `photoelectrique` ne contient aucun des deux fragments).

Notation d'Ilyann, sur les réponses brutes. « Référence » = le code de `main`
(`resultats/resultats.json`, 2026-10-19) ; « Variante » = `etage2-variante.json`.

| Cas | Référence | Variante |
|---|---|---|
| relativité 4k | ✗ fabrique un second « nom » (« classe des référentiels inertiels ») | ~ incomplète mais honnête (« inertiel » seul) |
| relativité 16k | ✓ (section citée douteuse) | ✓ citation mot pour mot |
| photoélectrique 4k | ~ signale que l'extrait ne contient pas les valeurs, mais cite [1] pour 4 et 5 et ajoute « vers 3 » sans source | ✗✗ attribue à [1] des valeurs et des notions absentes de l'extrait (« électrons lents », « ondes porteuses ») |
| photoélectrique 16k | ✓ | ✓ |

**Conclusion** : la variante est meilleure sur 3 cas et pire sur le cas
critique, la fidélité à la source. Avec 1 tirage par case, on ne peut pas
trancher (cf. #83). Le cas photoélectrique 4k montre qu'une notation sur la
seule réponse finale le compterait juste.

**Correction (#83, 2026-10-27)** : « électrons lents » et « ondes porteuses »
ne sont PAS absents de l'extrait à 4 000 caractères : ils y figurent, dans des
passages sans rapport avec la dépendance en Z (« faisceau d'électrons « lents » »,
« génération d'ondes porteuses non atténuées »). Ce qui manque à l'extrait, ce
sont les valeurs 4 et 5 (hors renvois de notes « [ 4 ] , [ 5 ] »). La faute de
la variante est donc double : des valeurs absentes, et des mots réels
détournés de leur sens — ce que l'heuristique de `core.citations.verifier_fidelite`
ne peut signaler que pour les valeurs.

**Temps (non comparables à l'étage 1 du README, pris sur batterie et MATLAB
ouvert ; celui-ci est sur secteur)** : premier token 22,6 s et 17,0 s à 4 000, 60,8 s et 153,7 s à 16 000.
Le 153,7 s de `photoelectrique` 16 000 (5 141 tokens) n'a pas d'explication
dans les données.

## 6. Limite statistique

Un seul tirage par case (question × budget). À température d'échantillonnage
non nulle, l'écart entre deux cases peut être du bruit. L'éval devrait faire
**5 tirages par case** pour séparer un effet réel de ce bruit (suivi dans
l'issue « Fidélité des citations »).

## 7. Voie 1 validée (JIT et réglages par modèle)

Réglages faits par Ilyann dans LM Studio pour `huihui-qwen3.5-9b-abliterated`
(1 requête parallèle, mmap désactivé), puis modèle déchargé et un tour envoyé
pour déclencher le chargement JIT (2026-10-22). Le tour est une requête
`/v1/chat/completions` directe, de même forme que celle d'Épure, **sans passer
par le backend d'Épure**.

- `n_slots = 1` dans le journal llama.cpp ; `parallel: 1` dans
  `/api/v1/models` (`loaded_instances[].config`).
- mmap désactivé, **déduit** du profil mémoire du processus LM Studio : 3,01 Go
  en working set et 8,49 Go en privé, comme `mmap-false.json` (2,54 / 8,11) et
  pas comme `mmap-true.json` (11,95 / 7,59). Le champ n'apparaît ni dans
  `/api/v1/models` ni dans le journal.
- Chargement d'environ 28,7 s selon le journal (50,2 s vu du client, requête
  comprise). Un passage par mode : l'écart avec les 37,9 s du SDK ne se tranche
  pas.
- Mémoire libre : 6,95 Go avant, 1,92 Go après. Non comparable à la séance
  mmap (ligne de base différente).
- Délai d'inactivité de 60 min avant déchargement (`lms ps`, TTL 60m / 1h).
  Origine (réglage local ou défaut LM Studio) **non vérifiée**.

**Conclusion** : Épure garde sa position « JIT, on ne charge pas »
(`core/models.py`, `etat_modele_lmstudio`). Les réglages recommandés
(1 requête parallèle, mmap désactivé) se font par modèle dans LM Studio. La
sonde sur `POST /api/v1/models/load` n'a pas été nécessaire.
