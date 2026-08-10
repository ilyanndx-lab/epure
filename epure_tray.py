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


def _start_processes():
    global _processes
    _processes = []
    fh = _open_log()

    env_ollama = os.environ.copy()
    env_ollama["OLLAMA_GPU_LAYERS"] = "-1"
    # Garde le modèle chargé en VRAM (pas de re-chargement à chaque requête après
    # 5 min d'inactivité) — comme start.ps1.
    env_ollama["OLLAMA_KEEP_ALIVE"] = "-1"

    # Ollama absent du PATH : on journalise et on continue, comme pour flm.
    #
    # Sans ce try, le FileNotFoundError remontait hors de _start_processes, qui
    # tourne dans un thread démon (cf. main() et _do_restart) : le thread mourait
    # sur place, donc NI uvicorn NI npm n'étaient lancés et le navigateur ne
    # s'ouvrait pas. La traceback partait sur stderr — pas dans epure_tray.log,
    # dont la dernière ligne restait « Lancement ollama serve », sans erreur. Et
    # comme icon.run() vit sur le thread principal, l'icône apparaissait
    # normalement : une application qui a l'air lancée et dont rien ne répond.
    # Sous pythonw (Épure.bat) il n'y a même plus de stderr pour recueillir la
    # traceback : le journal est le seul endroit où l'incident peut se voir.
    #
    # Dégradation choisie : le chat échoue faute de modèle, tout le reste — RAG,
    # fiches, flashcards, réglages, Atelier — fonctionne.
    _log("Lancement ollama serve")
    try:
        p_ollama = subprocess.Popen(
            ["ollama", "serve"],
            env=env_ollama,
            stdout=fh,
            stderr=fh,
            startupinfo=_HIDDEN,
            encoding="utf-8",
            errors="ignore",
        )
        _processes.append(p_ollama)
    except FileNotFoundError:
        _log(
            "Ollama introuvable sur le PATH — le chat ne fonctionnera pas. "
            "Installez-le depuis https://ollama.com puis « Redémarrer » dans le "
            "menu de l'icône. Le reste d'Épure démarre normalement."
        )

    _log("Lancement flm serve")
    try:
        p_flm = subprocess.Popen(
            ["flm", "serve", "--port", "11435"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=_HIDDEN,
        )
        _processes.append(p_flm)
    except FileNotFoundError:
        _log("flm non trouvé — ignoré")

    time.sleep(4)

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
    # non valide » → backend qui crashe/devient « injoignable ». C'est ce qui
    # rendait le lancement par le raccourci lent/cassé alors que start.ps1 (sans
    # --reload) démarre vite. Quand activé, on surveille UNIQUEMENT core/ (écrire
    # un router.py dans modules/ lors d'une approbation atelier ne doit pas
    # relancer le backend, qui monte déjà les routes à chaud).
    if os.environ.get("EPURE_RELOAD", "0").strip().lower() in ("1", "true", "yes"):
        uvicorn_cmd += ["--reload", "--reload-dir", "core"]
        _log("uvicorn : rechargement auto activé sur core/ (instable sous Windows ; EPURE_RELOAD=0 pour désactiver)")
    else:
        _log("uvicorn : rechargement auto désactivé (EPURE_RELOAD=1 pour l'activer en dev)")
    p_uvicorn = subprocess.Popen(
        uvicorn_cmd,
        cwd=str(BACKEND_DIR),
        stdout=fh,
        stderr=fh,
        startupinfo=_HIDDEN,
        encoding="utf-8",
        errors="ignore",
    )
    _processes.append(p_uvicorn)

    time.sleep(6)

    _log("Lancement npm run dev")
    p_npm = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=str(FRONTEND_DIR),
        stdout=fh,
        stderr=fh,
        startupinfo=_HIDDEN,
        shell=True,
        encoding="utf-8",
        errors="ignore",
    )
    _processes.append(p_npm)

    _log("Services démarrés — ouverture du navigateur dans 8 s")
    time.sleep(8)
    webbrowser.open(URL)


def _stop_processes():
    for p in _processes:
        try:
            p.terminate()
        except Exception:
            pass
    _kill_existing()
    _processes.clear()
    _log("Services arrêtés")


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
    _log("=== Épure démarrage ===")
    _kill_existing()
    time.sleep(2)

    threading.Thread(target=_start_processes, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Ouvrir Épure", _on_open, default=True),
        pystray.MenuItem("Redémarrer", _on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quitter", _on_quit),
    )

    icon = pystray.Icon(
        name="epure",
        icon=_make_icon(),
        title="Épure",
        menu=menu,
    )
    icon.run()


if __name__ == "__main__":
    main()
