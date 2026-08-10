# Épure — guide d'utilisation

Ce guide s'adresse à quelqu'un qui veut **se servir** d'Épure. Il ne suppose
aucune connaissance en programmation et ne demande jamais d'ouvrir un terminal.

Pour installer Épure autrement qu'en un fichier, développer dessus ou la faire
tourner en Docker, c'est le [README](../README.md).

---

## 1. Ce qu'est Épure, et ce qu'elle n'est pas

Épure est un assistant qui tourne **sur votre ordinateur**. Le modèle d'IA par
défaut (Ollama) s'exécute localement : vos conversations et vos documents ne
partent nulle part.

Elle sait discuter, répondre en s'appuyant sur vos propres PDF, garder un
historique consultable, lire ses réponses à voix haute et transcrire la vôtre.
Le reste — cartes de révision, interrogations orales, agent de code, tri de
fichiers — arrive sous forme de **modules** que vous installez si vous en avez
l'usage. Le cœur ne présume rien de votre métier ni de vos études.

Ce qu'elle n'est pas :

- **Pas un service en ligne.** Rien ne fonctionne si l'application n'est pas
  lancée sur votre machine.
- **Pas un outil multi-utilisateurs.** Une instance, une personne.
- **Pas infaillible.** Un modèle d'IA se trompe, y compris en citant vos
  propres documents. Vérifiez ce qui compte.

Si vous branchez une clé d'API cloud (Gemini, Groq, Mistral…), les messages
envoyés à ce fournisseur-là sortent de votre machine. C'est optionnel et
désactivé par défaut.

---

## 2. Installation

Windows 10 ou 11. Téléchargez le dossier du projet, puis, dans le dossier :

1. Clic droit sur `install.ps1` → **Exécuter avec PowerShell**.

Le script installe ce qui manque (Python, Node.js, Ollama, le modèle) et crée un
raccourci **Épure** sur votre Bureau. Il annonce chaque étape et n'efface jamais
rien. Comptez **environ 5 Go de téléchargement** et un bon moment.

Vous pouvez le relancer quand vous voulez : il détecte ce qui est déjà là et
passe son chemin. C'est aussi comme ça qu'on met Épure à jour.

> **Pour regarder avant d'agir.** Ouvrez PowerShell dans le dossier et lancez
> `.\install.ps1 -DryRun` : le script affiche tout ce qu'il ferait, sans rien
> installer.

Si PowerShell refuse d'exécuter le script, c'est la politique d'exécution de
Windows. Lancez plutôt :
`powershell -ExecutionPolicy Bypass -File .\install.ps1`

---

## 3. Premier lancement

Double-cliquez sur le raccourci **Épure** du Bureau (ou sur `Epure.bat` dans le
dossier).

Il ne se passe rien de visible pendant quelques secondes, puis :

- une **icône verte** apparaît dans la zone de notification, en bas à droite ;
- votre navigateur s'ouvre sur Épure.

L'icône est le poste de commande. Clic droit dessus :

| Entrée | Effet |
|---|---|
| **Ouvrir Épure** | rouvre l'onglet |
| **Redémarrer** | relance les services, sans fermer l'icône |
| **Quitter** | arrête tout |

**Si l'infobulle de l'icône dit « Épure — dégradé »**, quelque chose n'a pas
démarré : elle vous dit quoi. Le détail complet est dans `epure_tray.log`, à
côté de `Epure.bat`. C'est le seul endroit où regarder quand rien n'apparaît :
l'application n'a pas de console.

Lancer Épure une seconde fois ne crée pas de doublon — une fenêtre vous le dit
et rouvre l'onglet existant.

---

## 4. Vos documents

Épure peut répondre en s'appuyant sur vos PDF. Deux réglages, dans
**Réglages › Fiches** :

- **la racine** : le dossier qui contient vos documents ;
- **les sous-dossiers surveillés** : ceux qu'Épure indexe réellement.

Tant qu'aucun sous-dossier n'est déclaré, **rien n'est indexé** — l'écran vous
le signale en jaune. C'est volontaire : Épure ne va pas fouiller vos disques
sans qu'on le lui demande.

Nommez ces dossiers comme vous voulez : ce sont vos catégories, et le module de
tri s'en sert telles quelles.

L'indexation tourne en arrière-plan. Un gros dossier prend plusieurs minutes la
première fois.

---

## 5. Le chat

Tapez, envoyez. Le modèle utilisé est affiché en bas ; vous pouvez en changer.

**Les préfixes `@`** modifient la façon de répondre :

| Préfixe | Effet |
|---|---|
| `@cours` | cherche dans *tous* vos documents indexés avant de répondre |
| `@strict` | réponse courte, sans introduction |
| `@mémoire` | affiche ce qu'Épure a retenu de vous |
| `@historique` | va chercher dans vos conversations passées |
| `@web` | fait une recherche web avant de répondre |

**Les commandes `/`** déclenchent une action. Tapez `/` pour voir la liste : elle
s'adapte à ce qui est installé chez vous. Il y a toujours `/résumé` (résume les
fichiers ouverts), `/modèle` (change de modèle), `/lacunes` et `/direct`. Chaque
module installé ajoute la sienne pour l'ouvrir.

**Le bouton d'effort** (à côté de l'envoi) fait travailler plusieurs modèles à la
suite — analyse, résolution, vérification — au lieu d'un seul. Plus lent, plus
soigné. Laissez sur *direct* pour une conversation normale.

---

## 6. Les modules

**Réglages › Catalogue.** Chaque module s'installe et se désinstalle d'un
bouton. Une sauvegarde horodatée est faite avant toute suppression.

| Module | À quoi il sert |
|---|---|
| **Docs** | analyse un document et discute avec vous à son sujet |
| **Flashcards** | cartes de révision à répétition espacée |
| **Kholle** | interrogation orale : l'IA pose, vous répondez, elle corrige |
| **Réviseur** | plan de révision quotidien à partir de vos lacunes |
| **Rangement** | classe un dossier, résume, détecte les doublons |
| **Code** | agent de code multi-langages, avec exécution |
| **Démo** | exemple minimal, sert de référence — supprimable |

Un module installé apparaît immédiatement dans la barre de gauche. Vous pouvez
réordonner ou masquer la barre depuis **Réglages › Modules**.

---

## 7. L'Atelier

L'Atelier fait **écrire un nouveau module par une IA** et l'ajoute à votre
Épure. Décrivez ce que vous voulez, relisez ce qui est proposé, approuvez.

Trois choses à savoir avant de vous en servir :

- **Le code généré s'exécute avec les mêmes droits que le reste d'Épure.**
  Il peut lire vos fichiers et accéder au réseau. Relisez ce que vous approuvez ;
  une validation automatique existe, mais elle attrape les accidents, pas les
  mauvaises intentions.
- **Vos modules ne partent pas dans les mises à jour** : ils vivent dans des
  dossiers exclus du dépôt. Ils survivent aux mises à jour.
- **En revanche, ne modifiez pas les fichiers d'Épure à la main.** La prochaine
  mise à jour entrerait en conflit. Passez par l'Atelier.

---

## 8. La voix

Épure peut lire ses réponses et transcrire les vôtres.

À la **première** utilisation de la lecture, un modèle de synthèse de **77 Mo**
est téléchargé. L'interface prévient avant, et n'insiste pas si vous refusez.
Hors ligne, la voix est indisponible et le dit — tout le reste continue de
fonctionner.

---

## 9. Vie privée et réseau

- Par défaut, Épure n'écoute que **sur votre machine**. Aucun autre appareil de
  votre réseau ne peut s'y connecter.
- Conversations, profil et documents restent dans le dossier d'Épure.
- Les clés d'API cloud sont facultatives. Sans elles, tout passe par le modèle
  local.

> ⚠️ Il existe un réglage pour ouvrir Épure aux autres appareils du réseau.
> **Ne l'activez pas sur un wifi partagé.** L'application peut exécuter du code
> sur votre machine, et elle n'est protégée que par un jeton. Sur le réseau
> d'un établissement, c'est une exposition réelle.

---

## 10. Quand ça ne marche pas

**L'icône apparaît mais le navigateur ne s'ouvre pas.**
Clic droit sur l'icône → Ouvrir Épure. Si la page reste blanche, attendez une
minute : le premier démarrage charge des modèles.

**L'infobulle dit « dégradé ».**
Elle nomme le problème. Le plus fréquent : Ollama n'est pas installé, donc le
chat ne peut pas répondre. Le reste d'Épure fonctionne quand même.

**Le chat répond « modèle introuvable ».**
Le modèle n'est pas téléchargé. Relancez `install.ps1`, il s'en charge.

**Le chat ne trouve rien dans mes documents.**
Vérifiez **Réglages › Fiches** : sans sous-dossier surveillé, rien n'est indexé.
Après en avoir ajouté un, laissez le temps à l'indexation.

**Rien du tout ne se passe au double-clic.**
Ouvrez `epure_tray.log`, à côté de `Epure.bat`. Sa dernière ligne dit où ça
s'est arrêté.

**Un module a planté.**
Il s'isole tout seul, le reste continue. Réglages › Catalogue pour le
réinstaller ou le supprimer.
