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

    _log("Lancement ollama serve")
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
    uvicorn_cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "0.0.0.0", "--port", "8000",
    ]
    # Rechargement auto en dev (EPURE_RELOAD=0 pour désactiver). On surveille
    # UNIQUEMENT backend/core/ : surveiller modules/ serait néfaste — chaque
    # approbation de l'atelier y écrit un router.py, ce qui relancerait le backend
    # (alors qu'il monte déjà les routes à chaud) et le rendrait « injoignable »
    # quelques secondes, et le watcher verrouillerait modules/_staging sous Windows.
    # (Une modif de main.py nécessite un redémarrage manuel du tray — rare.)
    if os.environ.get("EPURE_RELOAD", "1").strip().lower() not in ("0", "false", "no", ""):
        uvicorn_cmd += ["--reload", "--reload-dir", "core"]
        _log("uvicorn : rechargement auto activé sur core/ (EPURE_RELOAD=0 pour désactiver)")
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
