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

**Raison de l'abandon : incompatible avec #66** (décision d'Ilyann). Cette PR
(lecture directe d'une URL collée) traite la page lue comme une source citable
`[n]` et ajoute au prompt des consignes sur les pages non lues, avec un ensemble
de référence des citations et des images. La variante déplace ce découpage
consigne/donnée ; ses tests n'avaient d'ailleurs pas été adaptés (message du
commit). Le détail de l'incompatibilité n'a pas été réexaminé en écrivant cette
note : se reporter à #66.

Mesure, pour la mémoire : même avec la variante, le système du tour 2 différait
du tour 1 (1 455 contre 1 061 caractères) — consigne « page non lue » ajoutée
parce que la lecture de Turing avait expiré. Le préfixe n'est donc pas resté
identique dans ce passage, et la relecture complète au tour 2 (§2, §3) ne
prouve rien pour ni contre la variante. Le tour 2 de la variante n'est de toute
façon pas comparable : le message utilisateur y contient « Recherche web :
Erreur de recherche web : <urlopen error tim… » au lieu du contenu de la page.

## 5. Notation de l'étage 2 (variante)

Fichier : `etage2-variante.json`. Conditions : sur secteur, raisonnement coupé,
8 192 de contexte, 2 questions × 2 budgets. **Notation relue sur les réponses
brutes contre `attendu` et contre l'étage 1** (`README.md` : à 4 000, l'extrait
de `relativite_restreinte` ne contient pas « référentiel de Lorentz », et celui
de `photoelectrique` ne contient aucun des deux fragments).

| Question | Budget | Réponse attendue présente ? | Fidèle à l'extrait ? | Note |
|---|---|---|---|---|
| relativite_restreinte | 4 000 | **Non** : donne « inertiel », pas « Lorentz » | Oui (n'invente pas, mais incomplet) | ❌ |
| relativite_restreinte | 16 000 | Oui, les deux noms | Oui (citation textuelle vérifiable) | ✅ |
| photoelectrique | 4 000 | « 4 et 5 » dit, **mais absent de l'extrait** | **Non** : [1] se voit attribuer « électrons lents » et des valeurs que l'extrait ne porte pas | ❌ (bonne valeur, mauvaise source) |
| photoelectrique | 16 000 | Oui, « 4 et 5 » | Plausible, non vérifié contre l'extrait | ✅ |

Le cas `photoelectrique` 4 000 est le plus instructif : un correcteur qui ne
regarde que la réponse finale le note juste. Voir l'issue « Fidélité des
citations ».

**Temps (non comparables à l'étage 1 du README, pris sur batterie et MATLAB
ouvert ; celui-ci est sur secteur)** : premier token 22,6 s et 17,0 s à 4 000, 60,8 s et 153,7 s à 16 000.
Le 153,7 s de `photoelectrique` 16 000 (5 141 tokens) n'a pas d'explication
dans les données.

## 6. Limite statistique

Un seul tirage par case (question × budget). À température d'échantillonnage
non nulle, l'écart entre deux cases peut être du bruit. L'éval devrait faire
**5 tirages par case** pour séparer un effet réel de ce bruit (suivi dans
l'issue « Fidélité des citations »).
