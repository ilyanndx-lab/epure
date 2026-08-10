"""Lanceur système d'Épure : Ollama, uvicorn, Vite, plus une icône de notification.

Windows uniquement (``subprocess.STARTUPINFO``, ``netstat``, ``taskkill``).

**Le journal est le seul canal de diagnostic.** Sous ``pythonw`` (le lanceur
double-cliquable) il n'y a pas de console, donc pas de stderr : une traceback
n'existe nulle part. Tout ce qui rate doit passer par :func:`_log`, et tout ce
qui dégrade le service doit remonter à l'infobulle de l'icône — une icône
d'apparence normale au-dessus de rien qui tourne est le pire état possible, et
c'est l'état qu'on a mesuré quand Ollama manquait.

**Une seule instance.** Rien ne détectait un tray déjà lancé : quatre sont
restés vivants une nuit entière sur le poste de dev. Avec un raccourci sur le
Bureau, le double-lancement devient le cas normal — chaque instance ajoutant son
icône et son jeu d'enfants se disputant les ports. Cf. :func:`_verrou_instance`.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

ROOT = Path(__file__).parent.resolve()
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
LOG_FILE = ROOT / "epure_tray.log"
URL = "http://localhost:5173"

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


# ── Instance unique ──────────────────────────────────────────────────────────

_MUTEX_NOM = "Local\\EpureTray"
_ERROR_ALREADY_EXISTS = 183


def _verrou_instance() -> bool:
    """True si ce process est le seul tray. False s'il y en a déjà un.

    Mutex nommé plutôt que fichier de verrou à PID : le noyau le libère quand le
    process meurt, quelle qu'en soit la façon. Un fichier survit à un `taskkill`
    et il faut alors vérifier si le PID qu'il contient est encore vivant — et si
    ce PID n'a pas été recyclé par un process sans rapport.

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
    webbrowser.open(URL)
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

def _kill_existing():
    for name in ("ollama.exe", "flm.exe", "uvicorn"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True,
                encoding="utf-8",
                errors="ignore",
            )
        except Exception:
            pass
    # Also kill by port in case uvicorn runs under python.exe
    for port in ("8000",):
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                encoding="utf-8",
                errors="ignore",
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True,
                        encoding="utf-8",
                        errors="ignore",
                    )
        except Exception:
            pass


_HIDDEN = subprocess.STARTUPINFO()
_HIDDEN.dwFlags |= subprocess.STARTF_USESHOWWINDOW
_HIDDEN.wShowWindow = subprocess.SW_HIDE


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


def _elaguer_processus():
    """Retire de ``_processes`` les Popen déjà morts, en les nommant.

    Un Popen mort qui reste dans la liste fait croire que le service tourne :
    c'est ce qui masquait l'échec de ``ollama serve`` quand son port était déjà
    pris. Le tray se croyait complet au-dessus d'un processus inexistant.
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
    """Lance un binaire, ou signale son absence sans interrompre le reste.

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


def _start_processes():
    """Démarre les services. Ne lève jamais : tout échec finit dans le journal.

    Le try/except global est le filet de dernier recours. Cette fonction tourne
    dans un thread démon ; sans lui, une exception imprévue tue le thread en
    silence et la traceback part sur stderr — qui n'existe pas sous pythonw.
    L'icône resterait alors parfaitement normale au-dessus de rien.
    """
    global _processes
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
    fh = _open_log()

    env_ollama = os.environ.copy()
    env_ollama["OLLAMA_GPU_LAYERS"] = "-1"
    # Garde le modèle chargé en VRAM (pas de re-chargement à chaque requête après
    # 5 min d'inactivité).
    env_ollama["OLLAMA_KEEP_ALIVE"] = "-1"

    _log("Lancement ollama serve")
    if _lancer(
        "ollama", ["ollama", "serve"],
        env=env_ollama, stdout=fh, stderr=fh, startupinfo=_HIDDEN,
        encoding="utf-8", errors="ignore",
    ) is None:
        _incident(
            "Ollama introuvable — le chat ne fonctionnera pas "
            "(installez-le depuis ollama.com, puis « Redémarrer »)"
        )

    _log("Lancement flm serve")
    if _lancer(
        "flm", ["flm", "serve", "--port", "11435"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=_HIDDEN,
    ) is None:
        _log("flm non trouvé — ignoré (runtime NPU optionnel)")

    time.sleep(4)
    _elaguer_processus()

    _log("Lancement uvicorn")
    # Interface d'écoute : LOOPBACK par défaut. Écouter sur 0.0.0.0 rendait le
    # port 8000 visible de tout le réseau (wifi de la prépa) alors que l'API
    # n'est protégée que par un token et expose l'exécution de commandes.
    # EPURE_BIND=0.0.0.0 rouvre au LAN — penser alors à compléter
    # EPURE_ALLOWED_HOSTS, sinon le middleware Host rejettera les requêtes.
    _bind = _bind_host()
    uvicorn_cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", _bind, "--port", "8000",
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

    time.sleep(6)
    _elaguer_processus()

    _log("Lancement npm run dev")
    if _lancer(
        "npm", ["npm", "run", "dev"],
        cwd=str(FRONTEND_DIR), stdout=fh, stderr=fh, startupinfo=_HIDDEN,
        shell=True, encoding="utf-8", errors="ignore",
    ) is None:
        _incident("npm introuvable — l'interface ne démarrera pas")

    _log("Services démarrés — ouverture du navigateur dans 8 s")
    time.sleep(8)
    _elaguer_processus()
    if _incidents:
        _notifier("Épure — démarrage dégradé", " · ".join(_incidents))
    _maj_infobulle()
    webbrowser.open(URL)


def _stop_processes():
    """Arrête NOS processus, et eux seuls.

    On ne connaît comme siens que les PID enregistrés au lancement. ``_tuer_arbre``
    est appelé sur chacun — et sur eux seuls.
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
    webbrowser.open(URL)


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
    _kill_existing()
    time.sleep(2)

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
