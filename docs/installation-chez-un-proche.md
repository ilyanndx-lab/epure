# Installer Épure chez un proche — plan de visite

**Le but de cette première installation n'est pas d'avoir Épure qui tourne chez
lui.** C'est de découvrir où tes instructions se cassent. Si tu fais tout
toi-même, tu repars avec une installation qui marche et zéro information.

---

## La veille — 10 minutes, chez toi

- `git status` vide, `git push`, et `gh run list --branch main --limit 1` vert.
  Il installera depuis GitHub, pas depuis ton disque : ce qui n'est pas poussé
  n'existe pas.
- Clone à froid dans un dossier temporaire, lance `install.ps1`, puis
  `Epure.bat`. Si ça marche chez toi sur un clone neuf, tu élimines la moitié
  des surprises. Supprime le dossier après.
- Vérifie que le README décrit bien la procédure que tu vas lui faire suivre,
  et pas une version antérieure.

## Sur place

**Règle unique : c'est lui qui tape.** Tu regardes, tu notes, tu ne prends le
clavier que s'il est bloqué depuis plus de deux minutes. Chaque fois que tu dois
expliquer quelque chose oralement, c'est une ligne qui manque au README —
note-la sur le moment, tu l'oublieras.

### Ce qu'il fait, dans l'ordre

1. Ouvre le README sur GitHub et le suit.
2. `git clone` puis `install.ps1`.
3. Double-clic sur le raccourci.
4. Une première question dans le chat.
5. Installe un module depuis Réglages › Catalogue.

### Compte le temps réel de chaque étape

La plupart est du téléchargement : Python, Node, Ollama, le modèle (~5 Go),
`pip install` (~2 Go), `npm ci`. Sur une connexion domestique, prévois 30 à 45
minutes dont l'essentiel est de l'attente. **Note les durées** — un README qui
annonce « quelques minutes » là où il en faut quarante fait abandonner les gens.

### Ce qu'il faut observer sans intervenir

- À quel moment il hésite, relit, ou ouvre un autre onglet.
- Ce qu'il fait quand une étape ne dit rien pendant deux minutes : est-ce qu'il
  attend, ou est-ce qu'il croit que c'est planté ?
- S'il comprend ce qu'est Ollama et pourquoi il faut le tirer.
- S'il trouve le raccourci après l'installation, sans que tu le lui montres.
- Ce qu'il essaie en premier dans l'application. Ce n'est probablement pas ce
  que tu crois.

### Si ça casse

Ne répare pas en silence. Note l'erreur exacte, **puis** répare. La correction
t'intéresse moins que le message : c'est lui qui te dira si le prochain s'en
sortira seul.

Le journal est `epure_tray.log` à la racine. C'est le seul endroit où un échec
de démarrage laisse une trace, maintenant que le lanceur n'ouvre plus de
console.

## Avant de repartir

- Lance et relance Épure deux fois devant lui, pour qu'il sache que le raccourci
  suffit.
- Montre-lui `install.ps1` relancé : c'est aussi son bouton de mise à jour.
- Dis-lui **ce qu'il ne doit pas faire** : modifier un fichier du cœur à la
  main. S'il veut changer quelque chose, il passe par l'Atelier.
- Explique où sont ses données — `backend/memory/`, `chroma_db/`, `.env` — et
  qu'aucune mise à jour n'y touche.
- Demande-lui de te dire, dans une semaine, ce qu'il a réellement utilisé. Pas
  ce qu'il en pense : ce qu'il a ouvert.

## En rentrant — pendant que c'est frais

Reprends tes notes et transforme chaque hésitation en une ligne de README ou en
une issue. Une hésitation observée vaut dix suppositions.

Et si plusieurs personnes doivent l'installer, ne recommence pas la visite :
corrige d'abord, puis laisse le suivant se débrouiller **sans toi** et
rapporter. La deuxième installation est le vrai test ; la première n'en est que
la préparation.
