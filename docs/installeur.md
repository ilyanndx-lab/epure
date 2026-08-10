# Installeur et lanceur — plan d'exécution

**Objectif.** Qu'un proche installe Épure en lançant un fichier, et l'utilise en
double-cliquant sur une icône. Aucun terminal, ni à l'installation, ni à l'usage.

**Décision prise.** `install.ps1` idempotent s'appuyant sur `winget`, plus un
raccourci vers `epure_tray.py`. Windows 10/11 uniquement — c'est la plateforme
primaire (CLAUDE.md §1) et celle de tous les destinataires connus.

**Décision reportée, volontairement.** Le passage du serveur Vite à un frontend
construit servi par FastAPI (un processus au lieu de deux) est un chantier
séparé, à juger *après* que les terminaux aient disparu. Motif : ce qui gêne
aujourd'hui, ce sont les terminaux, pas les processus ; et le build dégraderait
la boucle de l'Atelier de instantanée à ~10 s par approbation. Cf. §5.

---

## §0 — Ce qui existe déjà, à vérifier plutôt qu'à croire

`epure_tray.py` résout tous ses chemins depuis `Path(__file__).parent` — il est
portable, contrairement à `start.ps1`. Il lance Ollama, uvicorn et `npm run dev`,
ouvre le navigateur, et place une icône dans la barre système. **Le problème des
deux terminaux est donc déjà résolu dans le dépôt** ; il manque un point
d'entrée double-cliquable et deux dépendances déclarées.

À vérifier avant d'écrire quoi que ce soit, et à me rapporter :

- `pystray` et `PIL` sont importés (`epure_tray.py:9-10`) et **absents de
  `requirements.txt`**. Troisième occurrence du motif après `python-multipart`
  et `httpx`, tous deux documentés dans les commentaires de ce fichier.
- `epure_tray.py:110` fait `Popen(["ollama", "serve"])`. `flm` est correctement
  optionnel (`flm non trouvé — ignoré`, l. 131). **Ollama l'est-il ?** Si Ollama
  n'est pas installé, cette ligne lève `FileNotFoundError` — à mesurer, pas à
  supposer.
- Le comportement quand un port est déjà pris (Ollama déjà lancé, un `npm run
  dev` oublié) : `epure_tray.py:61-71` tue par nom et par port. Vérifier que
  c'est bien ciblé et qu'il ne tue pas un Ollama que l'utilisateur pilotait
  autrement.

---

## §1 — Étapes

### Étape A — Rendre `epure_tray.py` installable et lançable

- Déclarer `pystray` et `Pillow` dans `backend/requirements.txt`, épinglées,
  avec un commentaire disant qu'elles servent le lanceur et non le backend.
- Garder `Popen(["ollama", ...])` derrière la même garde que `flm` : Ollama
  absent doit produire un message clair dans le journal du tray et une
  application qui démarre quand même (le chat échouera, le reste fonctionnera).
- `git rm start.ps1` : chemins absolus `C:\Users\Ilyan\...`, lancement de `flm`
  (runtime NPU spécifique), `taskkill` sur le port 11434. C'est de l'outillage
  personnel. Mettre à jour la section « Lanceur Windows tout-en-un » du README.
- Créer `Épure.bat` à la racine : `pythonw epure_tray.py`. `pythonw`, pas
  `python` — c'est ce qui évite la fenêtre de console. Vérifier que le journal
  du tray reste écrit (`epure_tray.log`), sinon un échec au démarrage devient
  invisible, ce qui est pire que la fenêtre.

**Vérification** : depuis un clone frais, `pip install -r` puis double-clic sur
`Épure.bat`. L'icône apparaît, le navigateur s'ouvre, aucune fenêtre de console.
Puis le cas dégradé : renommer `ollama.exe`, relancer, constater que l'app
démarre et que le journal dit pourquoi le chat ne marchera pas.

Commits : `fix(deps): declare pystray et Pillow, requises par epure_tray` ·
`chore: retire start.ps1, non portable` · `feat(tray): lanceur double-cliquable
sans console`

### Étape B — `install.ps1`

Un script à la racine, **idempotent** : le relancer sur une installation
existante ne doit rien casser et sert de mise à jour.

Séquence, chaque étape précédée d'une détection :

1. Vérifier `winget`. Absent → message expliquant qu'il faut Windows 10 1809+
   et pointant vers l'App Installer du Microsoft Store. Ne pas tenter de
   l'installer.
2. Python ≥ 3.12 : `winget install Python.Python.3.12` si absent.
   **Attention** : la CI valide en 3.12, ce poste tourne en 3.14. Installer la
   3.12 et non « la dernière » aligne les proches sur la version testée.
3. Node ≥ 20 : `winget install OpenJS.NodeJS.LTS` si absent.
4. Ollama : `winget install Ollama.Ollama` si absent.
5. `ollama pull qwen2.5:7b` si le modèle de `backend/config.yaml` n'est pas déjà
   présent (`ollama list`). Lire le nom **depuis `config.yaml`**, ne pas le
   coder en dur — c'est ce qui fait que le script reste juste quand le défaut
   change.
6. `pip install -r backend/requirements.txt`.
7. `npm ci` dans `frontend/`.
8. Copier `backend/.env.example` vers `backend/.env` **s'il n'existe pas**.
   Jamais l'écraser.
9. Créer un raccourci sur le Bureau vers `Épure.bat`, avec l'icône du projet.

Exigences de forme :

- **Encodage.** Ce dépôt s'est fait mordre quatre fois par PowerShell : UTF-16
  des redirections, CRLF, collage multi-lignes, `@{0}` interprété comme table de
  hachage. Le script est en UTF-8 **sans BOM** — `.gitattributes` le force déjà
  en CRLF sur disque via `*.ps1 text eol=crlf`, ce qui est correct pour un `.ps1`.
- **Chaque étape annonce ce qu'elle fait et ce qu'elle a trouvé** : « Python
  3.12.8 déjà présent, ignoré », « Node absent, installation… ». Un script qui
  travaille en silence pendant vingt minutes est indistinguable d'un script
  bloqué.
- **Ne rien faire en silence de destructeur.** Aucun `Remove-Item`, aucune
  écriture par-dessus un `.env` existant.
- **Code de sortie non nul** dès qu'une étape échoue, avec la commande exacte à
  relancer à la main.

**Vérification, la seule qui compte** : exécuter le script sur une machine ou
une VM **où rien n'est installé**. Pas sur ce poste, où tout est déjà là et où
chaque détection renverra « déjà présent » — le script paraîtrait fonctionner
sans avoir rien installé. Si aucune VM n'est disponible, dis-le : ce sera une
limite déclarée, pas une vérification.

Commit : `feat(install): script d'installation idempotent (winget)`

### Étape C — Documentation

- README : une section « Installation en un fichier » en tête, avant
  l'installation manuelle, qui reste comme repli.
- La section « Mettre à jour » avec les six limites arrêtées : catalogue non
  propagé, `.env` non versionné, frontend à reconstruire, pas de migration de
  config, modèles Ollama manuels, et modification d'un fichier du cœur qui
  provoque un conflit au `git pull`.
- Dire que `install.ps1` est relançable et sert aussi de mise à jour.

Commit : `docs: installation en un fichier et procedure de mise a jour`

---

## §5 — Hors périmètre, et pourquoi

**Le frontend construit servi par FastAPI.** Un processus au lieu de deux, plus
de Node à l'exécution — mais Node reste requis à l'installation, puisque
installer un module écrit un `.tsx` qu'il faut construire. Le gain est donc
« un processus en moins », pas « une dépendance en moins ». Le coût est la
boucle de l'Atelier, qui passerait d'instantanée à ~10 s par approbation.

À rouvrir seulement après avoir vécu avec l'étape A : si les processus en
arrière-plan ne se remarquent plus une fois les terminaux disparus, ce chantier
n'a pas lieu d'être.

**macOS et Linux.** `install.ps1` est Windows. Un équivalent `install.sh` est
mécanique mais non testable ici. À écrire quand quelqu'un en aura besoin, pas
avant — un script non exécuté est une fiction.

**Exécutable packagé (Tauri, PyInstaller).** torch et chromadb rendent le bundle
énorme, et l'Atelier a besoin d'un environnement Python éditable. Plusieurs
semaines pour un résultat fragile.

---

## §6 — Ce qui n'a pas été vérifié

- Les identifiants `winget` (`Python.Python.3.12`, `OpenJS.NodeJS.LTS`,
  `Ollama.Ollama`) sont cités de mémoire. **À confirmer par `winget search`
  avant de les écrire dans le script** — c'est exactement le genre de valeur
  plausible qui se révèle fausse au premier usage chez quelqu'un d'autre.
- Le comportement de `epure_tray.py` quand Ollama est absent : non mesuré.
- Le comportement de `pythonw` vis-à-vis du journal du tray : non mesuré.
- Aucune exécution du script sur une machine vierge à ce stade.
