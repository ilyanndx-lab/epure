"""Lanceur système d'Épure : Ollama, uvicorn, Vite, plus une icône de notification.

Windows uniquement (``subprocess.STARTUPINFO``, ``netstat``, ``taskkill``).

Trois incidents mesurés dictent la forme de ce fichier ; les rejouer coûte cher,
donc ils sont écrits ici plutôt que dans un commit qu'on ne relira pas.

1. **Quatre trays vivants en même temps.** Rien ne détectait une instance déjà
   lancée. Avec un raccourci sur le Bureau, le double-lancement devient le cas
   normal : on accumulait les icônes, chacune avec son jeu d'enfants se
   disputant les ports. D'où le mutex nommé de :func:`_verrou_instance`.

2. **Épure fonctionnait par accident.** ``taskkill /F /IM ollama.exe`` tuait le
   serveur Ollama de l'utilisateur, mais ratait ``ollama app.exe`` — nom d'image
   différent — qui le relançait aussitôt. Le ``ollama serve`` du tray échouait
   alors sur un port déjà pris (« Only one usage of each socket address »),
   mourait, et restait dans ``_processes`` comme un Popen mort. Ce qui servait
   les requêtes, c'était l'Ollama de l'application de bureau, pas le nôtre.
   Désormais : si quelque chose répond sur 11434, on le réutilise.

3. **On tuait les processus des autres.** Le kill par port sur 8000 frappait le
   détenteur du port quel qu'il soit — exactement le reproche fait à
   ``start.ps1`` en le retirant du dépôt. Un port n'est libéré que si son
   occupant a été *identifié* comme un reste d'Épure (sonde ``/health``) ; sinon
   on refuse et on le dit.

**Le journal est le seul canal de diagnostic.** Sous ``pythonw`` (Epure.bat) il
n'y a pas de console, donc pas de stderr : une traceback n'existe nulle part.
Tout ce qui rate doit passer par :func:`_log`, et tout ce qui dégrade le service
doit remonter à l'infobulle de l'icône — une icône d'apparence normale au-dessus
de rien qui tourne est le pire état possible.
"""

import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

ROOT = Path(__file__).parent.resolve()
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
LOG_FILE = ROOT / "epure_tray.log"

PORT_BACKEND = 8000
PORT_OLLAMA = 11434
PORT_FLM = 11435
PORT_VITE = 5173

#: URL réellement ouverte. Vite bascule de port si 5173 est pris ; on la corrige
#: après lecture de sa sortie (cf. _detecter_port_vite).
_url = f"http://localhost:{PORT_VITE}"

_processes: list[subprocess.Popen] = []
_log_handle = None
_icon = None
#: Ce qui empêche Épure de fonctionner normalement, remonté à l'infobulle.
_incidents: list[str] = []
#: Handle du mutex d'instance unique — gardé en vie pour la durée du process.
_mutex_handle = None


def _bind_host() -> str:
    """Interface d'écoute d'uvicorn : $EPURE_BIND, sinon backend/.env, sinon loopback.

    Le backend charge .env tout seul (core.paths), mais le tray choisit l'hôte
    AVANT de le lancer : sans cette lecture, un EPURE_BIND posé dans
    backend/.env — là où .env.example le documente — serait ignoré en silence.

    On lit le fichier SANS le charger dans os.environ : le tray transmet son
    environnement à `ollama serve` (cf. env_ollama), et y injecter le contenu de
    .env y ferait entrer OLLAMA_HOST, que le serveur Ollama interprète comme son
    adresse d'écoute — piège connu (CLAUDE.md §8). Si python-dotenv manque,
    seules les vraies variables d'environnement comptent : le tray ne doit pas
    refuser de démarrer pour ça.
    """
    val = os.environ.get("EPURE_BIND", "").strip()
    if not val:
        try:
            from dotenv import dotenv_values

            val = (dotenv_values(BACKEND_DIR / ".env").get("EPURE_BIND") or "").strip()
        except ImportError:  # pragma: no cover - dépend de l'environnement
            val = ""
    return val or "127.0.0.1"


def _open_log():
    global _log_handle
    if _log_handle is None or _log_handle.closed:
        _log_handle = open(LOG_FILE, "a", buffering=1, encoding="utf-8")
    return _log_handle


def _log(msg: str):
    fh = _open_log()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    fh.write(f"[{ts}] {msg}\n")
    fh.flush()


# ── État dégradé, remonté à l'icône ──────────────────────────────────────────

def _maj_infobulle():
    """Reflète ``_incidents`` dans l'infobulle. Sans effet si l'icône n'existe pas encore.

    L'infobulle Windows est tronquée au-delà de 127 caractères ; on coupe
    nous-mêmes pour que la fin reste lisible plutôt qu'arbitraire.
    """
    if _icon is None:
        return
    try:
        if _incidents:
            texte = "Épure — dégradé : " + " · ".join(_incidents)
            _icon.title = (texte[:124] + "…") if len(texte) > 125 else texte
        else:
            _icon.title = "Épure"
    except Exception:  # pragma: no cover - dépend du backend pystray
        pass


def _incident(msg: str):
    """Journalise ET fait apparaître le problème dans l'interface.

    Tout ce qui passe ici est un service qui ne rendra pas le service attendu.
    Le journal seul ne suffit pas : personne ne lit un fichier de log quand
    l'icône a l'air normale.
    """
    _log(msg)
    _incidents.append(msg)
    _maj_infobulle()


def _notifier(titre: str, message: str):
    if _icon is None:
        return
    try:
        _icon.notify(message, titre)
    except Exception:  # pragma: no cover - notify indisponible selon le backend
        _log(f"(notification impossible : {titre} — {message})")


# ── Sondes de ports : qui écoute, et est-ce nous ? ───────────────────────────

def _port_pid(port: int):
    """PID du processus qui ÉCOUTE sur ``port``, ou None.

    On découpe les colonnes de netstat au lieu de chercher ``":8000"`` dans la
    ligne entière : la version en sous-chaîne attrapait aussi bien l'adresse
    distante qu'un port dont 8000 est un suffixe.
    """
    try:
        res = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, encoding="utf-8", errors="ignore",
        )
    except Exception:
        return None
    for ligne in res.stdout.splitlines():
        champs = ligne.split()
        if len(champs) < 5 or champs[0].upper() != "TCP" or champs[3].upper() != "LISTENING":
            continue
        if champs[1].rsplit(":", 1)[-1] == str(port):
            try:
                return int(champs[4])
            except ValueError:
                continue
    return None


def _nom_processus(pid: int) -> str:
    try:
        res = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, encoding="utf-8", errors="ignore",
        )
        premiere = res.stdout.strip().splitlines()[0]
        return premiere.split('","')[0].strip('"')
    except Exception:
        return "inconnu"


def _http_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _ollama_repond(port: int = PORT_OLLAMA) -> bool:
    """True si un vrai serveur Ollama sert déjà sur ce port."""
    data = _http_json(f"http://127.0.0.1:{port}/api/tags")
    return isinstance(data, dict) and "models" in data


def _backend_epure_repond(port: int = PORT_BACKEND) -> bool:
    """True si le port est tenu par un backend Épure — et non par un inconnu.

    Identification par le COMPORTEMENT et non par le nom du process : ``/health``
    est la seule route ouverte sans token (CLAUDE.md §3.1) et sa forme est
    reconnaissable. C'est ce qui autorise à tuer le détenteur du port sans
    reproduire l'erreur de start.ps1 — on ne tue que ce qu'on a identifié.

    ⚠️ Délai d'attente volontairement large. ``/health`` interroge Ollama et
    énumère ses modèles : mesuré à 2,05–2,20 s de façon stable Ollama allumé, et
    au-delà de 5 s Ollama ÉTEINT — le handler y attend le timeout de connexion
    du client Ollama (``connect=5.0``, core/llm.py). Deux valeurs trop courtes
    ont déjà fait conclure « le backend n'a pas répondu » alors qu'uvicorn
    écrivait « Application startup complete » dans le même journal.

    Ici la justesse prime sur la vitesse : se tromper, c'est soit tuer le
    processus d'autrui, soit refuser de récupérer son propre port.
    """
    data = _http_json(f"http://127.0.0.1:{port}/health", timeout=12.0)
    return isinstance(data, dict) and "ollama" in data and "model" in data


# ── Instance unique ──────────────────────────────────────────────────────────

_MUTEX_NOM = "Local\\EpureTray"
_ERROR_ALREADY_EXISTS = 183


def _verrou_instance() -> bool:
    """True si ce process est le seul tray. False s'il y en a déjà un.

    Mutex nommé plutôt que fichier de verrou à PID : le noyau le libère quand le
    process meurt, quelle qu'en soit la façon. Un fichier survit à un `taskkill`
    et il faut alors vérifier si le PID qu'il contient est encore vivant — et
    si ce PID n'a pas été recyclé par un process sans rapport.

    En cas d'échec de l'appel système, on renvoie True : mieux vaut un second
    tray qu'un lanceur qui refuse de démarrer pour une raison qu'il n'explique
    pas.
    """
    global _mutex_handle
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        # c_int et non c_bool : le BOOL de l'API Win32 fait 4 octets, le c_bool de
        # ctypes un seul. La différence ne se voit pas avec False, elle se voit un
        # jour où quelqu'un passe True.
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = k32.CreateMutexW(None, False, _MUTEX_NOM)
        erreur = ctypes.get_last_error()
    except Exception as exc:  # pragma: no cover - dépend de l'OS
        _log(f"Verrou d'instance indisponible ({exc}) — démarrage sans garde")
        return True
    if not handle:
        _log(f"Verrou d'instance non créé (erreur {erreur}) — démarrage sans garde")
        return True
    if erreur == _ERROR_ALREADY_EXISTS:
        return False
    _mutex_handle = handle
    return True


def _refuser_second_lancement():
    """Reprend la main sur l'instance existante, puis le dit — visiblement.

    Sans console, un `print` n'irait nulle part et l'utilisateur ne comprendrait
    pas pourquoi son double-clic n'a « rien fait ». On rouvre donc l'onglet de
    l'instance en place (c'est ce qu'il voulait) et on affiche une boîte de
    dialogue, seul canal visible depuis un process pythonw.
    """
    _log("Second lancement refusé : une instance d'Épure tourne déjà")
    webbrowser.open(_url)
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            "Épure est déjà lancé.\n\n"
            "L'onglet vient d'être rouvert dans votre navigateur. "
            "Utilisez l'icône dans la zone de notification pour redémarrer ou quitter.",
            "Épure",
            0x40,  # MB_OK | MB_ICONINFORMATION
        )
    except Exception:  # pragma: no cover - dépend de l'OS
        pass


# ── Cycle de vie des processus ───────────────────────────────────────────────

_HIDDEN = subprocess.STARTUPINFO()
_HIDDEN.dwFlags |= subprocess.STARTF_USESHOWWINDOW
_HIDDEN.wShowWindow = subprocess.SW_HIDE


def _elaguer_processus():
    """Retire de ``_processes`` les Popen déjà morts, en les nommant.

    Un Popen mort qui reste dans la liste fait croire que le service tourne :
    c'est ce qui masquait l'échec de ``ollama serve`` sur un port déjà pris.
    """
    global _processes
    vivants = []
    for p in _processes:
        code = p.poll()
        if code is None:
            vivants.append(p)
        else:
            nom = getattr(p, "_epure_nom", "processus")
            _log(f"{nom} s'est arrêté immédiatement (code {code}) — retiré du suivi")
    _processes = vivants


def _lancer(nom: str, cmd: list, **kwargs):
    """Lance un binaire, ou journalise son absence sans interrompre le reste.

    Chaque service est gardé individuellement : un binaire manquant doit coûter
    une ligne de journal, pas le démarrage des autres. Avant, seul `flm` l'était
    — un Ollama absent levait FileNotFoundError, qui remontait hors du thread
    démon de `_start_processes` et emportait uvicorn et Vite avec lui, sans rien
    écrire dans le journal.
    """
    try:
        p = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError:
        return None
    except Exception as exc:
        _incident(f"{nom} : lancement impossible ({exc})")
        return None
    p._epure_nom = nom
    _processes.append(p)
    return p


def _liberer_port_backend() -> bool:
    """Libère le port 8000 s'il est tenu par un reste d'Épure. True si utilisable.

    Ne tue QUE ce qui a été identifié comme un backend Épure par sa réponse à
    ``/health``. Un port 8000 tenu par autre chose n'est pas touché : c'est un
    refus explicite, pas un kill à l'aveugle. Le tray de la veille pouvait
    laisser un uvicorn orphelin ; un Jupyter ou un autre serveur de dev sur 8000,
    lui, n'a rien demandé.
    """
    pid = _port_pid(PORT_BACKEND)
    if pid is None:
        return True
    if _backend_epure_repond():
        _log(f"Backend Épure résiduel sur {PORT_BACKEND} (PID {pid}) — arrêté")
        _tuer_arbre(pid)
        time.sleep(1.5)
        return _port_pid(PORT_BACKEND) is None
    nom = _nom_processus(pid)
    _incident(
        f"port {PORT_BACKEND} occupé par {nom} (PID {pid}), qui n'est pas Épure — "
        "je ne le tue pas ; le backend ne démarrera pas"
    )
    return False


def _tuer_arbre(pid: int):
    """taskkill /T sur un PID donné : le process ET ses descendants.

    ``/T`` est indispensable pour npm, lancé via un shell intermédiaire :
    terminer le shell seul laissait `node` (Vite) vivant et le port 5173 pris.
    N'est appelé que sur des PID dont on a établi qu'ils sont les nôtres.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, encoding="utf-8", errors="ignore",
        )
    except Exception:
        pass


def _attendre_backend(timeout: float = 45.0) -> bool:
    """Attend qu'uvicorn ACCEPTE les connexions. False s'il reste muet.

    Simple connexion TCP, et surtout PAS ``/health`` : uvicorn ne se lie au port
    qu'une fois le démarrage applicatif terminé, donc accepter suffit à prouver
    qu'il est prêt — et cela ne dépend pas d'Ollama. La version à ``/health``
    signalait faussement « le backend n'a pas répondu » dès qu'Ollama était
    éteint, parce que le handler attend alors le timeout de connexion du client
    Ollama : la sonde mesurait la santé d'Ollama, pas celle du backend.

    Le premier démarrage charge des modèles ML : quelques dizaines de secondes
    sont normales. Mieux vaut ouvrir l'onglet tard que sur une API absente.
    """
    limite = time.time() + timeout
    while time.time() < limite:
        try:
            with socket.create_connection(("127.0.0.1", PORT_BACKEND), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


_RE_VITE_URL = re.compile(r"http://localhost:(\d+)/")


def _detecter_port_vite(offset: int, timeout: float = 25.0) -> int:
    """Lit le port réellement retenu par Vite dans sa propre sortie.

    Vite bascule silencieusement sur 5174 si 5173 est pris (un `npm run dev`
    oublié, par exemple). Le tray ouvrait alors 5173 quoi qu'il arrive, et
    l'utilisateur regardait l'ANCIEN frontend sans le moindre avertissement.
    On lit donc la ligne « Local: http://localhost:<port>/ » que Vite écrit dans
    le journal, à partir de l'offset noté juste avant son lancement.
    """
    limite = time.time() + timeout
    while time.time() < limite:
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                for m in _RE_VITE_URL.finditer(fh.read()):
                    return int(m.group(1))
        except Exception:
            pass
        time.sleep(0.5)
    return PORT_VITE


def _start_processes():
    """Démarre les services. Ne lève jamais : tout échec finit dans le journal.

    Le try/except global est le filet de sécurité de dernier recours. Cette
    fonction tourne dans un thread démon ; sans lui, une exception imprévue tue
    le thread en silence et la traceback part sur stderr — qui n'existe pas sous
    pythonw. L'icône resterait alors parfaitement normale au-dessus de rien.
    """
    global _processes, _url
    _processes = []
    _incidents.clear()
    _maj_infobulle()
    try:
        _demarrer()
    except Exception as exc:
        import traceback
        _incident(f"échec inattendu du démarrage : {type(exc).__name__} — {exc}")
        _log("Traceback :\n" + traceback.format_exc())
        _notifier("Épure", "Le démarrage a échoué. Détails dans epure_tray.log.")


def _demarrer():
    # `global` indispensable : sans lui, `_url = …` plus bas créait une variable
    # LOCALE. Le navigateur s'ouvrait bien sur le bon port (variable locale),
    # mais « Ouvrir Épure » dans le menu de l'icône relisait le global resté à
    # 5173 — donc l'ancien frontend, exactement le bug qu'on corrige ici.
    global _url
    fh = _open_log()

    # ── Ollama : réutilisé s'il tourne déjà ──────────────────────────────────
    # On ne tue plus rien ici. L'application de bureau Ollama (« ollama app.exe »)
    # relance son serveur dès qu'on le tue, et notre propre `ollama serve`
    # mourait alors sur un port pris. Un serveur qui répond fait le travail,
    # qu'il soit le nôtre ou non.
    if _ollama_repond():
        _log(f"Ollama déjà en service sur {PORT_OLLAMA} — réutilisé, pas de relance")
    else:
        env_ollama = os.environ.copy()
        env_ollama["OLLAMA_GPU_LAYERS"] = "-1"
        # Garde le modèle chargé en VRAM (pas de re-chargement à chaque requête
        # après 5 min d'inactivité).
        env_ollama["OLLAMA_KEEP_ALIVE"] = "-1"
        _log("Lancement ollama serve")
        p = _lancer(
            "ollama", ["ollama", "serve"],
            env=env_ollama, stdout=fh, stderr=fh, startupinfo=_HIDDEN,
            encoding="utf-8", errors="ignore",
        )
        if p is None:
            _incident(
                "Ollama introuvable — le chat ne fonctionnera pas "
                "(installez-le depuis ollama.com, puis « Redémarrer »)"
            )

    # ── flm : optionnel par nature, jamais un incident ───────────────────────
    if _port_pid(PORT_FLM) is not None:
        _log(f"flm déjà en service sur {PORT_FLM} — réutilisé")
    else:
        _log("Lancement flm serve")
        if _lancer(
            "flm", ["flm", "serve", "--port", str(PORT_FLM)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=_HIDDEN,
        ) is None:
            _log("flm non trouvé — ignoré (runtime NPU optionnel)")

    time.sleep(4)
    _elaguer_processus()

    # ── uvicorn ──────────────────────────────────────────────────────────────
    backend_lance = False
    if not _liberer_port_backend():
        _notifier("Épure", f"Le port {PORT_BACKEND} est déjà pris. Voir epure_tray.log.")
    else:
        _log("Lancement uvicorn")
        # Interface d'écoute : LOOPBACK par défaut. Écouter sur 0.0.0.0 rendait le
        # port 8000 visible de tout le réseau (wifi de la prépa) alors que l'API
        # n'est protégée que par un token et expose l'exécution de commandes.
        # EPURE_BIND=0.0.0.0 rouvre au LAN — penser alors à compléter
        # EPURE_ALLOWED_HOSTS, sinon le middleware Host rejettera les requêtes.
        _bind = _bind_host()
        uvicorn_cmd = [
            sys.executable, "-m", "uvicorn", "main:app",
            "--host", _bind, "--port", str(PORT_BACKEND),
            # Le token WebSocket voyage en query param (les navigateurs interdisent
            # les en-têtes sur `new WebSocket()`), donc l'access-log recopierait
            # « ?token=… » en clair dans epure_tray.log, non chiffré et conservé.
            # Le tray a déjà son propre journal applicatif ; l'access-log uvicorn
            # n'apporte rien de plus ici.
            "--no-access-log",
        ]
        if _bind != "127.0.0.1":
            _log(f"uvicorn : ATTENTION, écoute sur {_bind} — API exposée au-delà de la machine locale")
        # Rechargement auto : DÉSACTIVÉ par défaut (EPURE_RELOAD=1 pour l'activer en
        # dev). Le reloader uvicorn est instable sous Windows : à chaque restart il
        # fait `os.kill(pid, CTRL_C_EVENT)` qui lève « OSError [WinError 6] Descripteur
        # non valide » → backend qui crashe/devient « injoignable ». Quand activé, on
        # surveille UNIQUEMENT core/ (écrire un router.py dans modules/ lors d'une
        # approbation atelier ne doit pas relancer le backend, qui monte déjà les
        # routes à chaud).
        if os.environ.get("EPURE_RELOAD", "0").strip().lower() in ("1", "true", "yes"):
            uvicorn_cmd += ["--reload", "--reload-dir", "core"]
            _log("uvicorn : rechargement auto activé sur core/ (instable sous Windows ; EPURE_RELOAD=0 pour désactiver)")
        else:
            _log("uvicorn : rechargement auto désactivé (EPURE_RELOAD=1 pour l'activer en dev)")
        if _lancer(
            "uvicorn", uvicorn_cmd,
            cwd=str(BACKEND_DIR), stdout=fh, stderr=fh, startupinfo=_HIDDEN,
            encoding="utf-8", errors="ignore",
        ) is None:
            _incident(f"interpréteur introuvable ({sys.executable}) — backend non lancé")
        else:
            backend_lance = True

    time.sleep(6)
    _elaguer_processus()

    # ── Vite ─────────────────────────────────────────────────────────────────
    # Le port 5173 déjà pris n'est PAS une erreur : Vite en choisit un autre.
    # Ce qui était une erreur, c'était de continuer à ouvrir 5173 malgré tout.
    occupant = _port_pid(PORT_VITE)
    if occupant is not None:
        _log(
            f"port {PORT_VITE} déjà pris par {_nom_processus(occupant)} (PID {occupant}) — "
            "Vite prendra un autre port, que le tray lira dans sa sortie"
        )
    offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    _log("Lancement npm run dev")
    p_npm = _lancer(
        "npm", ["npm", "run", "dev"],
        cwd=str(FRONTEND_DIR), stdout=fh, stderr=fh, startupinfo=_HIDDEN,
        shell=True, encoding="utf-8", errors="ignore",
    )
    if p_npm is None:
        _incident("npm introuvable — l'interface ne démarrera pas")
        _maj_infobulle()
        return

    port_vite = _detecter_port_vite(offset)
    _url = f"http://localhost:{port_vite}"
    if port_vite != PORT_VITE:
        _incident(f"interface sur le port {port_vite} et non {PORT_VITE} (déjà occupé)")

    # Attendre le backend AVANT d'ouvrir l'onglet. Auparavant un `sleep(8)` fixe
    # servait de marge ; lire la sortie de Vite rend le démarrage plus rapide et
    # ferait ouvrir la page avant que l'API réponde. On sonde donc /health, ce
    # qui a l'avantage de transformer « backend muet » en incident visible au
    # lieu d'un onglet blanc.
    if backend_lance and not _attendre_backend():
        _incident(f"le backend n'a pas répondu sur {PORT_BACKEND} — voir epure_tray.log")

    _elaguer_processus()
    _log(f"Services démarrés — ouverture de {_url}")
    if _incidents:
        _notifier("Épure — démarrage dégradé", " · ".join(_incidents))
    _maj_infobulle()
    webbrowser.open(_url)


def _stop_processes():
    """Arrête NOS processus, et eux seuls.

    Plus aucun ``taskkill /IM`` : tuer par nom d'image frappait l'Ollama de
    l'utilisateur (et n'atteignait de toute façon pas « ollama app.exe »). On ne
    connaît comme siens que les PID enregistrés au lancement.
    """
    for p in list(_processes):
        nom = getattr(p, "_epure_nom", "processus")
        if p.poll() is not None:
            continue
        try:
            _tuer_arbre(p.pid)
        except Exception:
            _log(f"{nom} (PID {p.pid}) : arrêt impossible")
    for p in list(_processes):
        try:
            p.wait(timeout=5)
        except Exception:
            pass
    _processes.clear()
    _incidents.clear()
    _maj_infobulle()
    _log("Services arrêtés")


# ── Icône ────────────────────────────────────────────────────────────────────

def _make_icon() -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=(80, 200, 120, 255),
        outline=(40, 140, 80, 255),
        width=3,
    )
    return img


def _on_open(icon, item):
    webbrowser.open(_url)


def _on_restart(icon, item):
    _log("Redémarrage demandé")
    icon.notify("Redémarrage en cours…", "Épure")
    threading.Thread(target=_do_restart, daemon=True).start()


def _do_restart():
    _stop_processes()
    time.sleep(2)
    _start_processes()


def _on_quit(icon, item):
    _log("Arrêt demandé")
    _stop_processes()
    icon.stop()
    if _log_handle and not _log_handle.closed:
        _log_handle.close()


def main():
    global _icon
    if not _verrou_instance():
        _refuser_second_lancement()
        return

    _log("=== Épure démarrage ===")

    menu = pystray.Menu(
        pystray.MenuItem("Ouvrir Épure", _on_open, default=True),
        pystray.MenuItem("Redémarrer", _on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quitter", _on_quit),
    )
    _icon = pystray.Icon(
        name="epure",
        icon=_make_icon(),
        title="Épure",
        menu=menu,
    )
    # L'icône est créée AVANT le thread de démarrage : _incident() doit pouvoir
    # écrire dans l'infobulle dès la première seconde, y compris pour un
    # incident survenu avant que icon.run() ne prenne la main.
    threading.Thread(target=_start_processes, daemon=True).start()
    _icon.run()


if __name__ == "__main__":
    main()
