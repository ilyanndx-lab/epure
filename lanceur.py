"""Logique du lanceur d'Épure, séparée de l'icône.

**Aucun import de pystray ni de PIL**, et rien de spécifique à Windows au niveau
module : ce fichier doit s'importer partout, y compris sur un runner Linux sans
serveur X. C'est ce qui rend ``backend/test_lanceur.py`` exécutable par la
découverte automatique de la CI, sans job ni display supplémentaire — la logique
du tray n'était couverte par rien tant qu'elle vivait à côté de ``pystray.Icon``.

``epure_tray.py`` garde l'icône, le menu et l'orchestration. Tout ce qui se
raisonne — quel port, tenu par qui, prêt ou non — vit ici.

**Rien n'importe ``core.*``.** Le lanceur tourne AVANT le backend ; importer
``core.runtime`` instancierait les moteurs dans le process du tray (CLAUDE.md
§3.2), y compris son thread de préchauffage.

Les appels système (``netstat``, ``tasklist``, ``taskkill``) ne sont émis qu'à
l'APPEL, jamais à l'import, et échouent proprement là où ils n'existent pas.

**Le venv dédié du backend (CLAUDE.md section 2) est résolu ici, pas dans
``epure_tray.py``.** Raison structurelle, pas de goût : ``backend/requirements.txt``
le dit noir sur blanc à propos de ``pystray`` — « aucun test ne charge le tray ».
Un module qui importe ``pystray``/``PIL`` est donc inimportable par la suite de
tests, sur CI comme en local sans display. La résolution de l'interpréteur qui
fera tourner uvicorn doit vivre dans un fichier testable : c'est celui-ci, pour
la même raison que le reste.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT_BACKEND = 8000
PORT_OLLAMA = 11434
PORT_FLM = 11435
PORT_VITE = 5173

#: Racine du dépôt — ancre STATIQUE dérivée de ``__file__`` (``lanceur.py`` vit
#: à la racine, à côté de ``epure_tray.py``), jamais d'une variable
#: d'environnement : c'est un repère de code, pas un dossier de données
#: (même distinction que ``core.paths.REPO_ROOT``).
RACINE = Path(__file__).resolve().parent


def fenetre_masquee():
    """``STARTUPINFO`` masquant la console d'un enfant, ou None hors Windows.

    Fonction et NON constante de module : ``subprocess.STARTUPINFO`` n'existe
    que sous Windows. Évalué à l'import, il levait un AttributeError sur Linux
    et rendait ce module — donc ses tests — impossible à charger en CI.
    """
    if not hasattr(subprocess, "STARTUPINFO"):
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def url_interface(port: int) -> str:
    return f"http://localhost:{port}"


class EtatLanceur:
    """État partagé entre l'orchestration et le menu de l'icône.

    Existe pour supprimer une classe de bug, pas pour ranger des variables.
    L'URL vivait dans un global du tray ; une affectation sans ``global`` en a
    fait une variable locale, et le navigateur s'ouvrait bien sur le port réel
    pendant que « Ouvrir Épure » rouvrait l'ancien — donc l'ancien frontend,
    exactement le symptôme qu'on corrigeait. Une affectation d'attribut ne peut
    pas se tromper de portée : le bug n'est plus évitable, il est impossible.
    """

    def __init__(self, port_interface: int = PORT_VITE):
        self.url = url_interface(port_interface)
        self.port_interface = port_interface
        self.incidents: list[str] = []

    def definir_port_interface(self, port: int) -> bool:
        """Enregistre le port réellement servi. True s'il diffère du port attendu.

        Le booléen sert à décider d'un incident : Vite qui bascule de port n'est
        pas une erreur, mais l'utilisateur doit l'apprendre, sans quoi il croit
        regarder la nouvelle interface.
        """
        self.port_interface = port
        self.url = url_interface(port)
        return port != PORT_VITE

    def ajouter_incident(self, message: str) -> None:
        self.incidents.append(message)

    def reinitialiser(self) -> None:
        self.incidents.clear()

    def resume(self) -> str:
        return " · ".join(self.incidents)

    def infobulle(self, limite: int = 125) -> str:
        """Texte d'infobulle, tronqué. Windows coupe au-delà de 127 caractères.

        On coupe nous-mêmes pour que la fin soit un « … » lisible plutôt qu'un
        mot arbitrairement sectionné.
        """
        if not self.incidents:
            return "Épure"
        texte = "Épure — dégradé : " + self.resume()
        return (texte[: limite - 1] + "…") if len(texte) > limite else texte


# ── Qui écoute sur ce port ? ─────────────────────────────────────────────────

def port_occupant(port: int):
    """PID du processus qui ÉCOUTE sur ``port``, ou None.

    Les colonnes de netstat sont découpées, et non cherchées en sous-chaîne.
    La version d'origine testait ``":8000" in ligne``, ce qui inspectait aussi
    l'adresse DISTANTE et acceptait n'importe quelle ligne où ce motif traînait.
    Ici seule la colonne d'adresse locale compte, et seules les lignes LISTENING.
    """
    try:
        res = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, encoding="utf-8", errors="ignore",
        )
    except Exception:
        return None
    for ligne in (res.stdout or "").splitlines():
        champs = ligne.split()
        if len(champs) < 5:
            continue
        if champs[0].upper() != "TCP" or champs[3].upper() != "LISTENING":
            continue
        if champs[1].rsplit(":", 1)[-1] != str(port):
            continue
        try:
            return int(champs[4])
        except ValueError:
            continue
    return None


def nom_processus(pid: int) -> str:
    """Nom d'image d'un PID, ou « inconnu ». Sert à NOMMER un occupant étranger."""
    try:
        res = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, encoding="utf-8", errors="ignore",
        )
        premiere = (res.stdout or "").strip().splitlines()[0]
        return premiere.split('","')[0].strip('"')
    except Exception:
        return "inconnu"


def tuer_arbre(pid: int) -> None:
    """``taskkill /T`` : le process ET ses descendants.

    ``/T`` est indispensable pour npm, lancé derrière un shell : terminer le
    shell seul laissait ``node`` vivant et le port de Vite pris. À n'appeler que
    sur un PID dont on a établi qu'il est le nôtre.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, encoding="utf-8", errors="ignore",
        )
    except Exception:
        pass


# ── Identification par le comportement ───────────────────────────────────────

def http_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def ollama_repond(port: int = PORT_OLLAMA) -> bool:
    """True si un vrai serveur Ollama sert déjà ce port — auquel cas on le réutilise."""
    data = http_json(f"http://127.0.0.1:{port}/api/tags")
    return isinstance(data, dict) and "models" in data


def backend_epure_repond(port: int = PORT_BACKEND, timeout: float = 12.0) -> bool:
    """True si le port est tenu par un backend Épure — et non par un inconnu.

    Identification par le COMPORTEMENT et non par le nom du process : ``/health``
    est la seule route ouverte sans token (CLAUDE.md §3.1) et sa forme est
    reconnaissable. C'est ce qui autorise à libérer le port sans reproduire
    l'erreur de start.ps1 — on ne tue que ce qu'on a identifié.

    Délai large et assumé : la justesse prime sur la vitesse, puisque se tromper
    signifie soit tuer le processus d'autrui, soit refuser de récupérer le sien.
    """
    data = http_json(f"http://127.0.0.1:{port}/health", timeout=timeout)
    return isinstance(data, dict) and "ollama" in data and "model" in data


# ── Disponibilité ────────────────────────────────────────────────────────────

def port_accepte(port: int, timeout: float = 1.0, hote: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((hote, port), timeout=timeout):
            return True
    except OSError:
        return False


def attendre_backend(port: int = PORT_BACKEND, timeout: float = 45.0,
                     pause: float = 0.5) -> bool:
    """Attend qu'uvicorn ACCEPTE les connexions. False s'il reste muet.

    Connexion TCP, et surtout PAS ``/health`` : uvicorn ne se lie au port
    qu'une fois le démarrage applicatif terminé, donc accepter suffit à prouver
    qu'il est prêt — et cela ne dépend d'aucun service tiers.

    La version à ``/health`` annonçait « le backend n'a pas répondu » dès
    qu'Ollama était éteint, parce que le handler y attendait alors le timeout de
    connexion du client Ollama : la sonde mesurait la santé d'Ollama, pas celle
    du backend, pendant qu'uvicorn écrivait « Application startup complete »
    dans le même journal.
    """
    limite = time.monotonic() + timeout
    while True:
        if port_accepte(port, timeout=min(1.0, max(0.05, pause * 2))):
            return True
        if time.monotonic() >= limite:
            return False
        time.sleep(pause)


# ── Port réellement retenu par Vite ──────────────────────────────────────────

_RE_VITE_URL = re.compile(r"http://localhost:(\d+)/")


def lire_port_vite(journal, offset: int = 0, timeout: float = 25.0,
                   pause: float = 0.5, defaut: int = PORT_VITE) -> int:
    """Lit le port que Vite a réellement retenu, dans sa propre sortie.

    Vite bascule silencieusement sur 5174 quand 5173 est pris — un ``npm run
    dev`` oublié, par exemple. Le tray ouvrait alors 5173 quoi qu'il arrive, et
    l'utilisateur regardait l'ANCIEN frontend sans le moindre avertissement.

    ``offset`` est la taille du journal juste avant le lancement de Vite : sans
    lui, on relirait le port d'un démarrage précédent, ce qui ferait mentir la
    détection au lieu de la corriger.
    """
    chemin = Path(journal)
    limite = time.monotonic() + timeout
    while True:
        try:
            with open(chemin, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                trouve = _RE_VITE_URL.search(fh.read())
            if trouve:
                return int(trouve.group(1))
        except OSError:
            pass
        if time.monotonic() >= limite:
            return defaut
        time.sleep(pause)


def taille_journal(journal) -> int:
    """Offset courant du journal, à passer à :func:`lire_port_vite`."""
    try:
        return Path(journal).stat().st_size
    except OSError:
        return 0


# ── Venv dédié du backend (CLAUDE.md section 2) ──────────────────────────────
#
# `tools\dev-epure.ps1` possède déjà cette logique, en PowerShell : elle ne
# peut pas être appelée depuis là pour la créer (il faut UN python pour lancer
# quoi que ce soit côté Python, et PowerShell n'a besoin d'aucun python pour
# exister — la dépendance ne peut aller que dans l'autre sens). Les DEUX
# lanceurs résolvent donc le même chemin, chacun dans son langage ; ce qui ne
# doit jamais diverger, et qui est vérifié par les tests des deux côtés, c'est
# CE chemin (`$RACINE\.venv`) et la même échappatoire `$env:EPURE_PYTHON`/
# `EPURE_PYTHON`.

def venv_python(racine: Path | None = None) -> Path:
    """Chemin de l'interpréteur du venv dédié — qu'il existe ou non.

    Même arborescence que ``tools\\dev-epure.ps1`` (``$VENV_PYTHON``) : ne pas
    diverger sous peine que les deux lanceurs se mettent à parler de deux
    environnements différents sans que rien ne le signale.
    """
    base = (racine or RACINE) / ".venv"
    return base / "Scripts" / "python.exe" if os.name == "nt" else base / "bin" / "python"


def _pythons_du_path(nom: str) -> list[str]:
    """Tous les ``nom`` trouvables sur le PATH, dans l'ordre, sans doublon.

    ``shutil.which`` seul ne rend que le premier : il faut ici la liste
    complète pour pouvoir écarter les alias du Microsoft Store (cf.
    ``_bootstrap_python``) sans perdre les candidats réels qui les suivraient.
    """
    vus: set[str] = set()
    resultat: list[str] = []
    for dossier in os.environ.get("PATH", "").split(os.pathsep):
        if not dossier:
            continue
        candidat = Path(dossier) / nom
        chemin = str(candidat)
        if candidat.exists() and chemin not in vus:
            vus.add(chemin)
            resultat.append(chemin)
    return resultat


def _python_utilisable(exe: str) -> bool:
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _bootstrap_python() -> str | None:
    """Un interpréteur CPython quelconque pour créer le venv — n'a plus besoin
    d'exister ensuite. Même priorité que ``Trouver-Bootstrap-Python`` dans
    ``tools\\dev-epure.ps1`` : ``py -3.14`` d'abord (version déjà validée sur ce
    poste, CLAUDE.md section 2), sinon le premier ``python.exe`` RÉEL du PATH —
    les alias ``WindowsApps`` (qui ouvrent le Store au lieu de lancer Python
    quand l'appli n'est pas installée) en tout dernier recours.
    """
    py = shutil.which("py")
    if py:
        try:
            r = subprocess.run(
                [py, "-3.14", "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=15,
            )
            chemin = r.stdout.strip()
            if r.returncode == 0 and chemin and Path(chemin).exists():
                return chemin
        except Exception:
            pass

    nom = "python.exe" if os.name == "nt" else "python3"
    tous = _pythons_du_path(nom)
    reels = [p for p in tous if "WindowsApps" not in p]
    shims = [p for p in tous if "WindowsApps" in p]
    for exe in reels + shims:
        if _python_utilisable(exe):
            return exe
    return None


def assurer_venv_backend(racine: Path | None = None, log=None) -> Path | None:
    """Garantit un interpréteur pour le backend, en créant le venv dédié au
    premier besoin. Rend ``None`` si aucun interpréteur utilisable n'a pu être
    obtenu — **jamais un repli sur un autre python** : c'est précisément le
    risque de rétrogradation de dépendances que ce venv existe pour supprimer
    (CLAUDE.md section 2), un repli silencieux le réintroduirait par la porte
    qu'on vient de fermer.

    ``$EPURE_PYTHON``/``EPURE_PYTHON`` reste l'échappatoire de
    ``tools\\dev-epure.ps1`` : posée, elle est rendue TELLE QUELLE, sans
    création ni vérification ici — un chemin nommé explicitement doit échouer
    nommé, pas être neutralisé par une garantie automatique. C'est l'appelant
    (:func:`python_backend_verifie`) qui doit alors vérifier.

    **Ne resynchronise PAS les dépendances d'un venv déjà présent.** Contraste
    volontaire avec ``tools\\dev-epure.ps1``, qui relit ``requirements.txt`` à
    CHAQUE lancement parce qu'il est le workflow « après un ``git pull`` ». Un
    lanceur du quotidien n'a pas cette raison de payer un aller-retour pip à
    chaque démarrage ; le venv est resynchronisé par ``dev-epure.ps1`` quand le
    code a bougé. Seule la création initiale (poste neuf, premier lancement du
    tray) installe ``requirements.txt`` — il n'y a alors rien d'autre à faire
    pour obtenir un backend qui démarre.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    force = os.environ.get("EPURE_PYTHON", "").strip()
    if force:
        return Path(force)

    racine = racine or RACINE
    python = venv_python(racine)
    if python.exists():
        return python

    _log(f"venv dedie absent -- creation ({python.parent.parent})")
    bootstrap = _bootstrap_python()
    if not bootstrap:
        _log("aucun interprete Python trouve pour creer le venv dedie")
        return None

    try:
        r = subprocess.run(
            [bootstrap, "-m", "venv", str(python.parent.parent)],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as exc:
        _log(f"creation du venv dedie impossible : {exc}")
        return None
    if r.returncode != 0 or not python.exists():
        _log("creation du venv dedie a echoue : " + (r.stdout + r.stderr).strip())
        return None

    requirements = racine / "backend" / "requirements.txt"
    try:
        r = subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(requirements),
             "--disable-pip-version-check"],
            capture_output=True, text=True, timeout=900,
        )
    except Exception as exc:
        _log(f"installation des dependances dans le venv dedie impossible : {exc}")
        return None
    if r.returncode != 0:
        _log("pip install a echoue dans le venv dedie : " + (r.stdout + r.stderr).strip())
        return None

    _log("venv dedie cree et synchronise")
    return python


def python_backend_verifie(python: Path, log=None) -> bool:
    """True si ``python`` a réellement ``fastapi``/``uvicorn`` importables.

    Le VRAI gate, décorrélé de la création du venv : un ``$EPURE_PYTHON`` mal
    pointé, ou un venv corrompu (suppression partielle, poste éteint en plein
    ``pip install``), doit être détecté ICI plutôt que de laisser
    ``subprocess.Popen`` démarrer un uvicorn qui plante sur ``ModuleNotFound``
    sans que ``epure_tray.py`` sache pourquoi (il ne capte pas le stdout de son
    backend, cf. sa docstring : le journal est le seul canal).
    """
    try:
        r = subprocess.run(
            [str(python), "-c", "import fastapi, uvicorn"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:
        if log:
            log(f"verification de l'interprete backend impossible ({python}) : {exc}")
        return False
    if r.returncode != 0:
        if log:
            log(f"l'interprete backend n'a pas fastapi/uvicorn ({python}) : "
                + (r.stdout + r.stderr).strip())
        return False
    return True
