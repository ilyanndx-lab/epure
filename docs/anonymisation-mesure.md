# Anonymisation locale avant envoi cloud — protocole de mesure

**Nature de ce document.** Ce n'est pas un plan de fonctionnalité. C'est un
**protocole d'expérience**, dont le livrable attendu est un **taux de fuite
mesuré** — vraisemblablement un résultat négatif. Il est écrit pour être
exécutable indépendamment d'Épure, et pour ne rien y intégrer tant que la mesure
n'est pas faite.

**Décision actée avec Ilyann (2026-08-09) :** cadrage « exercice technique, je
veux voir si c'est faisable ». Ni outil personnel, ni brique pour Cloudiway.

---

## §0 — Ce qu'on cherche à réfuter

L'hypothèse à tester, formulée pour pouvoir échouer :

> **H₀ — Un pipeline tournant entièrement en local peut transformer un document
> métier en français de sorte qu'il soit sans risque de l'envoyer à un LLM
> hébergé par un tiers.**

Quatre raisons de penser que H₀ est fausse. Elles ne dispensent pas de mesurer —
elles déterminent *ce qu'il faut mesurer*.

1. **La détection statistique a un rappel < 100 %, et son échec est silencieux.**
   Les benchmarks publiés des outils de détection d'entités portent
   majoritairement sur des corpus anglais propres. Du français métier bruité est
   un autre régime. Or la valeur d'une anonymisation est **binaire** : 97 % de
   rappel ne vaut pas 97 % de la garantie, ça vaut zéro garantie. Un nom manqué
   suffit.
2. **Les quasi-identifiants survivent à un rappel parfait.** « Le client lyonnais
   de 12 personnes qui a migré d'Exchange vers Google Workspace en mars »
   ré-identifie de façon unique sans contenir la moindre entité nommée. Aucun
   détecteur d'entités ne voit quoi que ce soit à masquer.
3. **Ce qui est utile est réversible, donc c'est de la pseudonymisation.** Pour
   ré-hydrater la réponse du modèle, il faut une table de correspondance. RGPD
   art. 4(5) et considérant 26 : une donnée ré-attribuable au moyen
   d'informations supplémentaires **reste une donnée personnelle**. La
   revendication « on a anonymisé, donc ce n'est plus une donnée personnelle »
   est donc fausse en droit, pas seulement fragile. *(Je ne suis pas juriste ; la
   définition, elle, n'est pas ambiguë.)*
4. **Un LLM local qui anonymise est la pire variante.** Non déterministe (deux
   passes, deux résultats), il réécrit et paraphrase, donc il peut ré-introduire
   l'information sous une autre forme. Et il y a un paradoxe : la valeur de
   l'étape d'anonymisation est maximale quand le modèle local est trop faible
   pour faire la tâche — c'est-à-dire quand il est aussi trop faible pour
   anonymiser de façon fiable.

---

## §1 — Décisions de cadrage

| Question | Décision | Raison |
|---|---|---|
| Type de données | **Prose uniquement** (mails, comptes-rendus, tickets) | Le structuré (CSV, logs, exports) est trivialement solvable : les identifiants sont dans des champs connus. Mesurer dessus ne prouverait rien. |
| Corpus | **Synthétique, généré** | Vérité terrain exacte par construction. Et utiliser de vraies données clients pour tester un anonymiseur non validé serait exactement la faute qu'on étudie. |
| Transformation | **Pseudonymisation réversible** (`ACME` → `ORG_1`) | La suppression pure détruit l'utilité : on ne peut plus ré-hydrater la réponse. C'est la seule forme qui mérite d'être mesurée. |
| Intégration Épure | **Aucune** avant résultats | Le livrable est une mesure. S'il est négatif — c'est l'attente — il n'y a rien à intégrer. |
| Seuil de décision | **Fixé avant de mesurer** (§4) | Sinon on ajuste le seuil au résultat obtenu. |

---

## §2 — Les trois bras

Même corpus, même sortie attendue, trois implémentations.

| Bras | Ce qu'il teste | Prédiction |
|---|---|---|
| **A — Déterministe** : regex (emails, IP, GUID, téléphone, IBAN, SIRET) + dictionnaire d'entités connues | Le plancher honnête. Rappel 100 % sur ce qui est dans le dictionnaire, 0 % sur le reste. Le mode de défaillance est **connaissable**, pas statistique. | Bon sur les entités connues, effondrement sur les inconnues |
| **B — NER** : Presidio (`fr_core_news_lg`) ou GLiNER | Le rappel statistique réel sur du français métier, contre les chiffres annoncés | Rappel notablement inférieur aux benchmarks anglais |
| **C — LLM local** : Qwen via Ollama, prompt d'anonymisation | La variante intuitive, celle qu'on essaie en premier | La pire : non déterministe et réécrit le fond |

Un **bras A+B** (déterministe, puis NER pour signaler ce qu'il a manqué) est
l'architecture réellement défendable — le NER en **assistant de relecture**, pas
en filtre autonome. Le mesurer aussi, mais **après** A, B et C séparément :
mélanger avant de connaître les composants rend le résultat illisible.

---

## §3 — Le corpus, et le piège qui invaliderait tout

### 3.1 — Génération

Un script produit N documents (viser 300-500) à partir de :

- un **jeu de scénarios** (relance client, compte-rendu de migration, ticket
  d'incident, mail interne, note de réunion) ;
- des **pools d'entités** : organisations, personnes, domaines, villes,
  identifiants techniques ;
- une **vérité terrain écrite au moment de l'insertion** : pour chaque document,
  la liste exacte des spans (offset début, fin, type, valeur). Elle est produite
  par le générateur, jamais annotée après coup.

Deux niveaux de réalisme, mesurés séparément :

- **Niveau 1 — gabarits.** Phrases à trous. Facile, propre. Sert de contrôle : un
  bras qui échoue ici est disqualifié.
- **Niveau 2 — reformulé par un LLM local.** Le même document passé à Qwen avec
  la consigne « reformule sans changer les noms, dates ni chiffres ». Introduit
  du bruit, des variations d'orthographe, des tournures. **La vérité terrain doit
  être revérifiée après reformulation** (recherche exacte de chaque valeur
  attendue) et le document rejeté si une entité a été altérée — sinon on mesure
  contre une vérité fausse.

### 3.2 — Le piège : le dictionnaire ne doit pas connaître le corpus

**C'est le point qui décide de la validité de toute l'expérience.**

Si le dictionnaire du bras A contient les mêmes entités que celles injectées
dans le corpus, le bras A obtient 100 % — et ce 100 % ne mesure rien du tout,
sinon qu'une recherche de chaîne fonctionne.

Donc : **le pool d'entités est scindé en deux avant génération.**

| Sous-ensemble | Dans le dictionnaire du bras A | Rôle |
|---|---|---|
| `connu` (≈ 60 %) | oui | Simule les clients qu'on a pensé à déclarer |
| `inconnu` (≈ 40 %) | **non** | Simule ceux qu'on a oubliés — le cas réel |

Toutes les métriques sont rapportées **séparément sur les deux sous-ensembles**.
Un résultat global agrégé masquerait exactement ce qu'on cherche à voir.

---

## §4 — Métriques, et le seuil fixé d'avance

### 4.1 — La métrique principale

**Taux de documents parfaitement pseudonymisés** : proportion de documents où
**zéro** entité de la vérité terrain a fuité.

Pas le rappel par entité. La garantie qu'on prétendrait offrir porte sur le
document qu'on envoie, pas sur une moyenne d'entités. Un rappel de 98 % par
entité sur des documents contenant 10 entités donne ≈ 82 % de documents propres :
presque un document sur cinq fuite. C'est la métrique qui rend cet écart
visible, et c'est pour ça qu'elle est principale.

### 4.2 — Les métriques secondaires

| Métrique | Pourquoi |
|---|---|
| Rappel par type d'entité (personne, orga, email, identifiant…) | Localise où ça casse |
| **Précision** | La sur-redaction détruit l'utilité : un texte où tout est `ORG_1` n'est plus exploitable |
| **Déterminisme** : deux passes sur le même document, sorties identiques ? | Discrimine le bras C. Un anonymiseur non déterministe est inauditable |
| Latence par document | Un pipeline de 30 s par mail ne sera pas utilisé |

### 4.3 — Le seuil, écrit avant la mesure

> **Aucun taux inférieur à 99,9 % de documents parfaitement pseudonymisés ne
> justifierait d'envoyer des données réelles à un LLM tiers. Et même au-dessus,
> l'expérience 2 (§5) peut invalider le résultat à elle seule.**

Ce seuil est noté ici pour ne pas être ajusté après coup au résultat obtenu.
S'il n'est pas atteint, la conclusion est « H₀ réfutée », et c'est un résultat
complet — pas un échec du projet.

---

## §5 — Expérience 2 : ré-identification adverse

**À faire en premier**, avant même de peaufiner les trois bras : elle est peu
coûteuse et c'est celle qui décide.

Protocole :

1. Prendre les documents que le pipeline déclare pseudonymisés — y compris ceux
   où toutes les entités ont bien été remplacées.
2. Les envoyer à un LLM cloud avec : *« Quelle entreprise, quel secteur, quelle
   taille, quelle région, quelle personne ce texte décrit-il ? Formule des
   hypothèses même incertaines. »*
3. Mesurer le taux de ré-identification correcte, en comparant à la vérité
   terrain du générateur.

Ce que ça teste et que rien d'autre ne teste : **les quasi-identifiants**. Aucun
des trois bras n'a de prise dessus, par construction. Si le taux est
significatif — c'est l'attente — alors le débat sur le rappel du détecteur
devient secondaire : le problème n'est pas les entités nommées.

Une variante utile si le corpus le permet : demander au modèle de désigner la
bonne organisation **parmi une liste de K candidats** issus du pool. La mesure
devient un taux de succès contre un hasard de 1/K, donc interprétable sans
jugement subjectif sur ce qui compte comme « identification correcte ».

---

## §6 — Expérience 3 : la perte d'utilité

Une anonymisation parfaite qui rend le texte inexploitable ne vaut rien. Il faut
donc mesurer les deux bouts.

Pour un échantillon : poser la même question métier au même modèle sur le
document **original** et sur sa version pseudonymisée, puis comparer les
réponses (concordance des faits cités, des chiffres, des conclusions).

Le résultat attendu est une courbe : plus la pseudonymisation est agressive
(donc sûre), plus la réponse se dégrade. **S'il n'existe aucun point où les deux
sont acceptables simultanément, c'est la réponse à la question posée** — et
c'est une réponse bien plus intéressante qu'un pipeline à moitié fonctionnel.

---

## §7 — Livrables

1. `generateur.py` — corpus + vérité terrain, graine fixée pour être reproductible
2. `bras_a.py`, `bras_b.py`, `bras_c.py` — même interface :
   `anonymiser(texte) -> (texte_pseudonymisé, mapping)`
3. `mesure.py` — les métriques du §4, résultats en CSV
4. `reidentification.py` — l'expérience 2
5. `RESULTATS.md` — les tableaux, **et la conclusion même si elle est négative**

Le point 5 est le vrai livrable. Les quatre autres sont l'appareillage.

---

## §8 — Ce que ce protocole ne mesure pas

À ne pas lui faire dire :

- **Il ne mesure rien sur des données réelles.** Un corpus synthétique est plus
  facile qu'un corpus réel — moins de bruit, moins d'ambiguïté, pas d'OCR, pas de
  fautes de frappe, pas de pièces jointes. Un bon score ici ne se transporte pas ;
  un mauvais score, en revanche, se transporte parfaitement (ce qui échoue sur du
  facile échoue sur du difficile).
- **Il ne teste pas les documents mixtes** (prose + tableau + en-têtes de mail),
  qui sont le format réel.
- **Il ne teste aucune attaque active.** Un texte construit pour contourner le
  détecteur n'est pas dans le périmètre.
- **Il ne dit rien sur la conformité.** Le §0 point 3 se règle en droit, pas par
  la mesure. Aucun taux, même de 100 %, ne rendrait légale une donnée
  ré-attribuable par table de correspondance.

---

## §9 — Si le résultat est celui qu'on attend

Alors la bonne conclusion n'est pas « il faut un meilleur anonymiseur », c'est
**changer de mécanisme**.

L'architecture qui ne promet rien de faux : **classification de sensibilité →
politique de routage**. Un niveau de sensibilité par conversation ; au niveau le
plus élevé, seuls les modèles locaux sont sélectionnables et les fournisseurs
cloud sont grisés ; un détecteur local **avertit** quand ce qu'on s'apprête à
envoyer ressemble à de la donnée client.

C'est fiable parce que ça ne fait aucune promesse d'anonymat : pour le sensible,
on **dégrade vers le local** au lieu d'anonymiser-et-expédier. Et c'est cohérent
avec le local-first d'Épure, où le modèle local est déjà le défaut.

Le détecteur du bras B trouve là son emploi honnête : signaler à un humain, pas
autoriser un envoi.

**Ce serait alors ça, la fonctionnalité à ajouter à Épure** — et elle mérite son
propre document, écrit après la mesure.

---

## §10 — Prompt pour Claude Code

> Projet **autonome**, hors du dépôt Épure — nouveau dossier, aucune dépendance
> à `epure/`.
>
> Lis `docs/anonymisation-mesure.md`. Implémente le §7 : générateur de corpus
> synthétique français avec vérité terrain par construction (graine fixée), les
> trois bras derrière une interface commune
> `anonymiser(texte) -> (texte, mapping)`, et le script de mesure.
>
> Deux points de conception qui décident de la validité, à ne pas simplifier :
>
> 1. **Le pool d'entités est scindé `connu` / `inconnu` avant génération**, et le
>    dictionnaire du bras A ne reçoit que `connu`. Toutes les métriques sont
>    rapportées séparément sur les deux sous-ensembles. Sans cette scission, le
>    bras A obtient 100 % et l'expérience ne mesure rien.
> 2. **La métrique principale est le taux de documents *parfaitement*
>    pseudonymisés**, pas le rappel par entité. Rapporte les deux, mais présente
>    la première en tête — c'est celle qui correspond à la garantie qu'on
>    prétendrait offrir.
>
> Commence par l'expérience 2 (ré-identification adverse, §5) : elle est la moins
> chère et c'est celle qui peut clore la question. Si son taux est élevé,
> dis-le-moi avant de peaufiner les trois bras.
>
> Écris `RESULTATS.md` avec les tableaux et une conclusion explicite sur H₀,
> **y compris et surtout si elle est négative**. Le livrable est la mesure, pas
> un pipeline qui marche.
