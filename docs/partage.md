# Partager Épure — ce qui manque au dépôt

Le dépôt seul ne suffit pas. Quatre choses ne sont pas dedans, et la première
bloque au tout premier geste.

---

## 1. Git — l'œuf et la poule

`install.ps1` est **dans** le dépôt. Pour le lancer il faut avoir cloné ; pour
cloner il faut git. Aucun script ne peut automatiser cette étape, puisqu'il
faudrait déjà l'avoir franchie pour l'exécuter.

C'est donc la première ligne des instructions, avant toute autre chose :

```powershell
winget install Git.Git
```

Puis **rouvrir le terminal** — le `PATH` n'est rafraîchi qu'au nouveau shell.
C'est le piège qui a coûté un aller-retour lors de l'installation de `gh`.

### Le ZIP est un piège

GitHub propose un bouton « Download ZIP », qui paraît plus simple que git. Un
ZIP n'a pas de dossier `.git`, donc **pas de `git pull`**, donc toute la
procédure de mise à jour du README tombe : il faudrait re-télécharger et
réinstaller à chaque version, en risquant d'écraser les données personnelles au
passage.

À dire explicitement, parce que c'est le raccourci que quelqu'un prendra
naturellement : **cloner, jamais télécharger le ZIP.**

---

## 2. L'accès au dépôt — décision à prendre

Tant qu'il est privé, un proche a besoin d'un compte GitHub, d'une invitation
nominative, **et de s'authentifier pour cloner** — gestionnaire d'identifiants
Windows, `gh auth login`, ou un jeton personnel. Pour quelqu'un qui n'a jamais
utilisé git, c'est de loin l'étape la plus pénible du parcours, et elle arrive
avant tout le reste.

En public, `git clone` est anonyme : rien à configurer, rien à inviter.

| | Privé | Public |
|---|---|---|
| Clone | compte + invitation + authentification | anonyme, immédiat |
| Lecture du code | cercle choisi | tout le monde, robots inclus |
| Réversible | oui | **non** — ce qui a été cloné l'est |

Les conditions du passage en public sont réunies : licence MIT posée, historique
purgé des données personnelles (juillet, puis l'ONNX), aucun secret versionné,
`.env` ignoré, `litellm.yaml` nettoyé de sa clé.

**Recommandation : passer public.** La friction supprimée est exactement celle
que les destinataires ne sauront pas franchir seuls. Mais c'est une décision, pas
une évidence — le prix est que n'importe qui peut lire, y compris des robots qui
scannent les dépôts en continu.

---

## 3. Leurs propres clés API

Ne jamais partager `backend/.env`. Sans aucune clé, Épure fonctionne sur Ollama
local — c'est le cas nominal et il suffit. Pour du cloud, chacun crée ses
comptes et met ses clés dans son propre `.env`, copié depuis `.env.example`.

À dire d'avance, sinon quelqu'un finira par demander les tiennes.

---

## 4. Le guide ne se trouve pas tout seul

`docs/guide.md` est écrit pour eux, mais personne n'ouvre un dossier `docs/` de
sa propre initiative. Le lien doit être dans le message d'envoi **et** en tête
du README.

---

## Le message à envoyer

À copier tel quel, en remplaçant l'URL :

```
Épure — assistant d'étude qui tourne entièrement sur ta machine.

Avant de commencer : compte 30 à 45 minutes, dont l'essentiel en
téléchargements (Python, Node, Ollama et son modèle, ~7 Go au total).
Tu peux lancer et aller faire autre chose.

1. Installe git, puis FERME ET ROUVRE le terminal :
      winget install Git.Git

2. Clone le dépôt — surtout pas le bouton « Download ZIP », il casse
   les mises à jour :
      git clone <URL>

3. Entre dedans et lance l'installeur :
      cd epure
      .\install.ps1

4. Double-clique sur le raccourci Épure sur ton Bureau.

Le guide d'utilisation : <lien vers docs/guide.md>

Deux choses à savoir :
- tout reste chez toi, rien n'est envoyé nulle part sans clé cloud ;
- pour mettre à jour plus tard, relance install.ps1 : tes données,
  tes documents et tes modules ne sont jamais touchés.

Si tu bloques, note l'endroit exact et le message d'erreur avant de
m'appeler — ça m'aide plus que la solution.
```

La dernière phrase n'est pas de la politesse : la première installation sert à
trouver où les instructions cassent, et une erreur notée vaut plus qu'une
installation réparée en silence. Cf. `docs/installation-chez-un-proche.md`.
