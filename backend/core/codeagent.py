import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Generator, Optional

from core.paths import resolve_workspace

logger = logging.getLogger(__name__)

# Racine de travail : <repo>/workspace par défaut, surchargeable via
# $EPURE_WORKSPACE (cf. core.paths.resolve_workspace / .env.example).
WORKSPACE = resolve_workspace()

# Libs qui ouvrent une fenêtre : on les lance en process externe (sinon un
# plt.show()/mainloop bloque jusqu'au timeout). matplotlib est un cas à part
# depuis le rendu inline des figures (cf. _PLOT_SITECUSTOMIZE_DIR ci-dessous) :
# `MPLBACKEND=Agg` + le hook `sitecustomize` évitent toute fenêtre, donc plus
# besoin de la router vers `_launch_gui` — retirée d'ici, PAS des autres.
GUI_LIBS = frozenset(["pygame", "tkinter", "turtle", "wx", "PyQt", "pyglet", "kivy"])

# ── Rendu inline des figures matplotlib (module Code) ────────────────────────
#
# Dossier de code STATIQUE (comme BACKEND_DIR/REPO_ROOT dans core.paths), pas
# un dossier de données : pas de surcharge d'environnement, dérivé de
# `__file__` une fois pour toutes. Contient un unique `sitecustomize.py`, posé
# en tête de PYTHONPATH de CHAQUE exécution `.py` (cf. execute_code) — jamais
# dans WORKSPACE, pour rester invisible et non éditable depuis l'arbre de
# fichiers de l'utilisateur.
_PLOT_SITECUSTOMIZE_DIR = Path(__file__).resolve().parent / "_plot_support"

# Plafonds de la Phase « figures inline » — protection réelle, pas cosmétique
# (cf. leur usage dans `_collect_plot_images`) : une boucle utilisateur qui
# génère des centaines de figures, ou une figure haute résolution, ne doit pas
# gonfler démesurément la réponse WebSocket d'un tour d'exécution.
_PLOT_MAX_IMAGES = 10
_PLOT_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 Mo par image

# On exécute avec le MÊME interpréteur que celui qui lance le backend
# (sys.executable), pas un "python" ambigu du PATH. Sinon un package installé
# via `sys.executable -m pip` reste introuvable pour un autre python.py du PATH.
_EXEC_CMDS: dict[str, list[str] | None] = {
    ".py":   [sys.executable, "-u"],
    ".js":   ["node"],
    ".ts":   ["npx", "ts-node"],
    ".sh":   ["bash"],
    ".html": None,  # preview only
    ".tex":  None,  # compile via compile_latex
}
_MAX_READ = 50_000
_EXEC_TIMEOUT = 30

# ── Interpréteur Python : primaire + repli ───────────────────────────────────
# Certains packages n'ont pas encore de wheel pour la version qui lance le
# backend (ex. Python 3.14). On détecte un interpréteur de repli (ex. 3.11) et,
# si une install échoue sur le primaire, on bascule l'install ET l'exécution
# dessus pour que le code retrouve ses dépendances.

_FALLBACK_VERSIONS = ("3.11", "3.12", "3.10", "3.13")
_fallback_python_cache: Optional[str] = None  # résolu une fois
_fallback_resolved = False
# Interpréteur réellement utilisé pour exécuter le code (bascule vers le repli
# quand une install n'a réussi que là).
_exec_python_path: str = sys.executable


def _py_launcher(version: str) -> Optional[str]:
    """Résout le chemin réel d'un Python via le lanceur Windows `py -X.Y`."""
    if not shutil.which("py"):
        return None
    try:
        out = subprocess.run(
            ["py", f"-{version}", "-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            path = out.stdout.strip()
            if path and Path(path).exists():
                return path
    except Exception:
        return None
    return None


def _python_is_sane(python: str) -> bool:
    """Vérifie que l'interpréteur démarre vraiment avec SA stdlib et a pip.
    Lancé avec l'env propre (sans PYTHONPATH du primaire)."""
    try:
        out = subprocess.run(
            [python, "-c", "import threading, ssl, ctypes, pip; print('ok')"],
            capture_output=True, text=True, timeout=20, env=_make_exec_env(python),
        )
        return out.returncode == 0 and "ok" in out.stdout
    except Exception:
        return False


def find_fallback_python() -> Optional[str]:
    """Interpréteur de repli SAIN (différent du primaire), résolu une seule fois.
    Priorité : variable EPURE_PYTHON_FALLBACK, puis lanceur `py -3.11/3.12/...`.
    On écarte tout candidat qui ne démarre pas correctement (stdlib incohérente,
    pas de pip…)."""
    global _fallback_python_cache, _fallback_resolved
    if _fallback_resolved:
        return _fallback_python_cache

    candidates: list[str] = []
    env_path = os.environ.get("EPURE_PYTHON_FALLBACK", "").strip()
    if env_path and Path(env_path).exists():
        candidates.append(env_path)
    for ver in _FALLBACK_VERSIONS:
        exe = _py_launcher(ver)
        if exe:
            candidates.append(exe)

    primary = Path(sys.executable).resolve()
    chosen: Optional[str] = None
    for cand in candidates:
        try:
            if Path(cand).resolve() == primary:
                continue
        except OSError:
            continue
        if _python_is_sane(cand):
            chosen = cand
            break
        logger.warning("Python de repli ignoré (interpréteur cassé) : %s", cand)

    _fallback_python_cache = chosen
    _fallback_resolved = True
    if chosen:
        logger.info("Python de repli détecté : %s", chosen)
    return chosen


def _exec_python() -> str:
    """Interpréteur courant pour exécuter du Python (primaire ou repli actif)."""
    return _exec_python_path


class SecurityError(Exception):
    pass


def _safe_path(relative: str) -> Path:
    """Resolve path and abort if it escapes the workspace.

    Compare des chemins résolus via ``is_relative_to`` (et non un
    ``startswith`` de chaînes, contournable par un dossier frère du type
    ``workspace-autre/`` ou une traversée ``..``/symlink).
    """
    target = (WORKSPACE / relative).resolve()
    if not target.is_relative_to(WORKSPACE):
        logger.warning("SECURITY: accès refusé hors workspace — %s", target)
        raise SecurityError(f"Accès refusé hors workspace : {target}")
    return target


# ── File tools ─────────────────────────────────────────────────────────────

def create_file(path: str, content: str) -> str:
    target = _safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("create_file: %s", target)
    return f"Fichier créé : {path}"


def read_file(path: str) -> str:
    target = _safe_path(path)
    if not target.exists():
        return f"Erreur : fichier introuvable — {path}"
    return target.read_text(encoding="utf-8", errors="replace")[:_MAX_READ]


def edit_file(path: str, old_content: str, new_content: str) -> str:
    target = _safe_path(path)
    if not target.exists():
        return f"Erreur : fichier introuvable — {path}"
    original = target.read_text(encoding="utf-8", errors="replace")
    if old_content not in original:
        return f"Erreur : texte introuvable dans {path}"
    target.write_text(original.replace(old_content, new_content, 1), encoding="utf-8")
    logger.info("edit_file: %s", target)
    return f"Fichier modifié : {path}"


def list_files_text(directory: str = ".") -> str:
    try:
        target = _safe_path(directory)
    except SecurityError as e:
        return str(e)
    if not target.exists():
        return "(dossier vide)"
    lines: list[str] = []
    _walk(target, 0, lines, max_depth=3)
    return "\n".join(lines) if lines else "(dossier vide)"


def _walk(current: Path, depth: int, lines: list, max_depth: int) -> None:
    if depth > max_depth:
        return
    try:
        entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return
    for entry in entries:
        indent = "  " * depth
        lines.append(f"{indent}{entry.name}{'/' if entry.is_dir() else ''}")
        if entry.is_dir():
            _walk(entry, depth + 1, lines, max_depth)


def get_tree() -> list:
    """Returns a structured tree for the frontend."""
    if not WORKSPACE.exists():
        return []
    return _tree_node(WORKSPACE, depth=0, max_depth=4)


def _tree_node(path: Path, depth: int, max_depth: int) -> list:
    if depth > max_depth:
        return []
    result = []
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return []
    for entry in entries:
        rel = str(entry.relative_to(WORKSPACE)).replace("\\", "/")
        if entry.is_dir():
            result.append({
                "name": entry.name,
                "path": rel,
                "type": "dir",
                "children": _tree_node(entry, depth + 1, max_depth),
            })
        else:
            result.append({"name": entry.name, "path": rel, "type": "file"})
    return result


def delete_path(path: str) -> str:
    target = _safe_path(path)
    if not target.exists():
        return f"Erreur : introuvable — {path}"
    if target.is_dir():
        import shutil
        shutil.rmtree(target)
    else:
        target.unlink()
    logger.info("delete_path: %s", target)
    return f"Supprimé : {path}"


def create_folder(path: str) -> str:
    target = _safe_path(path)
    target.mkdir(parents=True, exist_ok=True)
    logger.info("create_folder: %s", target)
    return f"Dossier créé : {path}"


def rename_path(old: str, new: str) -> str:
    """Renomme/déplace un fichier ou dossier dans le workspace (confiné)."""
    src = _safe_path(old)
    dst = _safe_path(new)
    if not src.exists():
        return f"Erreur : introuvable — {old}"
    if dst.exists():
        return f"Erreur : la cible existe déjà — {new}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    logger.info("rename_path: %s → %s", src, dst)
    return f"Renommé : {old} → {new}"


_SENSITIVE = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS")
_EXPLICIT_DENY = {
    "GROQ_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY",
    "NVIDIA_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
}


def _make_exec_env(python: Optional[str] = None) -> dict:
    """Env minimal pour lancer `python` (défaut : l'interpréteur primaire).

    IMPORTANT : on n'injecte le PYTHONPATH du backend (= sys.path de Python 3.14)
    QUE si la cible est ce même interpréteur. Pour un interpréteur différent
    (repli 3.11), injecter ce PYTHONPATH lui ferait charger la stdlib de 3.14 →
    crash (`_thread.start_joinable_thread`). On le laisse alors utiliser sa
    propre stdlib. On ne pose jamais PYTHONHOME.

    Cette restriction ne s'applique PAS à `_PLOT_SITECUSTOMIZE_DIR` (ajouté à
    part, par `execute_code`, après cet appel) : ce dossier ne contient qu'un
    unique fichier pur Python (`atexit`/`os`/`sys` de la stdlib, jamais résolus
    via `sys.path`), sans aucune dépendance tierce ni extension compilée — rien
    qui puisse faire charger la stdlib d'un autre interpréteur. Sûr quel que
    soit l'interpréteur cible, contrairement au PYTHONPATH complet ci-dessus.
    """
    same_interp = python is None or (
        Path(python).resolve() == Path(sys.executable).resolve()
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    if same_interp:
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    # Remove empty values and any accidentally included sensitive vars
    return {
        k: v for k, v in env.items()
        if v and k not in _EXPLICIT_DENY and not any(s in k.upper() for s in _SENSITIVE)
    }


def compile_latex(path: str) -> dict:
    """Compile un fichier .tex avec pdflatex."""
    target = _safe_path(path)
    if not target.exists():
        return {"stdout": "", "stderr": f"Fichier introuvable : {path}", "returncode": -1, "duration_ms": 0}
    if not shutil.which("pdflatex"):
        return {
            "stdout": "",
            "stderr": (
                "pdflatex introuvable — aucune distribution TeX n'est installée.\n"
                "Installez MiKTeX (puis rouvrez l'app pour rafraîchir le PATH) :\n"
                "    winget install --id MiKTeX.MiKTeX -e\n"
                "ou TeX Live :\n"
                "    winget install --id TeXLive.TeXLive -e\n"
                "À défaut de winget : https://miktex.org/download"
            ),
            "returncode": -1,
            "duration_ms": 0,
        }
    t0 = time.time()
    try:
        result = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", str(target)],
            capture_output=True, text=True,
            timeout=60, cwd=str(WORKSPACE),
        )
        dur = round((time.time() - t0) * 1000)
        if result.returncode == 0:
            pdf_path = target.with_suffix(".pdf")
            if pdf_path.exists():
                try:
                    os.startfile(str(pdf_path))
                except Exception:
                    pass
            return {"stdout": f"✓ PDF compilé : {pdf_path.name}", "stderr": result.stderr[:500], "returncode": 0, "duration_ms": dur}
        return {"stdout": result.stdout[:500], "stderr": result.stderr[:1000], "returncode": result.returncode, "duration_ms": dur}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Timeout pdflatex (60s)", "returncode": -1, "duration_ms": 60000}
    except Exception as exc:
        logger.exception("compile_latex: %s", path)
        return {"stdout": "", "stderr": str(exc), "returncode": -1, "duration_ms": 0}


_GUI_GRACE = 2.0  # délai pour distinguer un vrai lancement d'un crash immédiat


def _launch_gui(target: Path) -> dict:
    """Lance une appli GUI dans une fenêtre externe. Si elle plante dans les
    premières secondes (module manquant, exception…), on capture la sortie et on
    la remonte au lieu de laisser la fenêtre clignoter sans message."""
    # Sortie redirigée vers un fichier temp : ne bloque pas un process GUI qui dure,
    # et reste lisible si le process meurt tout de suite.
    log = tempfile.NamedTemporaryFile(mode="w+", suffix=".log", delete=False, encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [_exec_python(), str(target)], cwd=str(WORKSPACE), env=_make_exec_env(_exec_python()),
            stdout=log, stderr=subprocess.STDOUT,
        )
    except Exception as exc:
        log.close()
        try:
            os.unlink(log.name)
        except OSError:
            pass
        return {"stdout": "", "stderr": str(exc), "returncode": -1, "duration_ms": 0}

    try:
        # S'il se termine pendant le délai de grâce → crash (ou script très court).
        proc.wait(timeout=_GUI_GRACE)
        log.flush()
        log.seek(0)
        output = log.read().strip()
        log.close()
        try:
            os.unlink(log.name)
        except OSError:
            pass
        if proc.returncode == 0:
            # Terminé proprement très vite : on traite comme une exécution normale.
            return {"stdout": output, "stderr": "", "returncode": 0, "duration_ms": 0}
        logger.warning("_launch_gui: crash immédiat — %s (rc=%s)", target.name, proc.returncode)
        return {
            "stdout": "",
            "stderr": output or f"Le programme s'est fermé immédiatement (code {proc.returncode}).",
            "returncode": proc.returncode,
            "duration_ms": 0,
        }
    except subprocess.TimeoutExpired:
        # Toujours vivant après le délai → vraie fenêtre GUI, on la laisse tourner.
        log.close()  # le process garde son propre handle vers le fichier
        return {"external": True, "stdout": "", "stderr": "", "returncode": 0, "duration_ms": 0}


def _collect_plot_images(plot_dir: str, out: dict) -> list[dict]:
    """Lit les PNG déposés par `core/_plot_support/sitecustomize.py` dans
    `plot_dir` (une exécution = un dossier dédié) et les encode en base64.

    Deux plafonds — une protection RÉELLE, pas cosmétique : `_PLOT_MAX_IMAGES`
    borne le NOMBRE d'images renvoyées (une boucle qui trace des centaines de
    figures ne doit pas gonfler la réponse WebSocket d'un tour), et
    `_PLOT_MAX_IMAGE_BYTES` la taille de CHACUNE (une figure haute résolution
    isolée ne doit pas non plus la faire exploser). Un dépassement — nombre ou
    taille — est signalé dans `stderr` de `out` (muté sur place), jamais avalé
    en silence : l'utilisateur doit comprendre pourquoi une figure manque.

    Tri par numéro (pas lexicographique) : `figure_10.png` ne doit pas se
    retrouver avant `figure_2.png`.
    """
    images: list[dict] = []
    try:
        pngs = list(Path(plot_dir).glob("figure_*.png"))
    except OSError:
        return images

    def _numero(p: Path) -> int:
        m = re.search(r"\d+", p.stem)
        return int(m.group()) if m else 0

    pngs.sort(key=_numero)

    for p in pngs[:_PLOT_MAX_IMAGES]:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if len(data) > _PLOT_MAX_IMAGE_BYTES:
            out["stderr"] = (
                out.get("stderr", "")
                + f"\n[figures] {p.name} ignorée : {len(data)} octets dépasse la "
                  f"limite ({_PLOT_MAX_IMAGE_BYTES} octets)."
            ).strip()
            continue
        images.append({"nom": p.name, "data_base64": base64.b64encode(data).decode("ascii")})

    if len(pngs) > _PLOT_MAX_IMAGES:
        out["stderr"] = (
            out.get("stderr", "")
            + f"\n[figures] {len(pngs)} figures produites, seules les "
              f"{_PLOT_MAX_IMAGES} premières sont affichées."
        ).strip()
    return images


def execute_code(path: str, args: str = "") -> dict:
    target = _safe_path(path)
    if target.suffix not in _EXEC_CMDS:
        allowed = ", ".join(sorted(_EXEC_CMDS))
        return {"stdout": "",
                "stderr": (f"Extension non exécutable : « {target.suffix or '(aucune)'} ». "
                           f"Extensions gérées : {allowed}. "
                           f"Renommez le fichier (ex. en .py) pour l'exécuter."),
                "returncode": -1, "duration_ms": 0}
    if not target.exists():
        return {"stdout": "", "stderr": f"Fichier introuvable : {path}", "returncode": -1, "duration_ms": 0}

    # HTML → preview seulement
    if target.suffix == ".html":
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"html_preview": True, "content": content, "stdout": "", "stderr": "", "returncode": 0, "duration_ms": 0}

    # LaTeX → compilation
    if target.suffix == ".tex":
        return compile_latex(path)

    # Python avec lib GUI RÉELLE (tkinter, pygame, ...) → fenêtre externe.
    # matplotlib N'EST PLUS dans GUI_LIBS : ses figures sont capturées et
    # rendues inline (cf. plot_dir ci-dessous), plus besoin de fenêtre.
    if target.suffix == ".py":
        try:
            src = target.read_text(encoding="utf-8", errors="replace")
            if any(lib in src for lib in GUI_LIBS):
                return _launch_gui(target)
        except Exception:
            pass

    if target.suffix == ".py":
        cmd = [_exec_python(), "-u", str(target)]
    else:
        cmd = list(_EXEC_CMDS[target.suffix]) + [str(target)]  # type: ignore[operator]

    if args.strip():
        cmd += args.strip().split()

    env = _make_exec_env(_exec_python()) if target.suffix == ".py" else _make_exec_env()

    # Rendu inline des figures matplotlib : dossier de sortie DÉDIÉ à CETTE
    # exécution (jamais partagé entre deux tours), backend headless pour
    # qu'aucune fenêtre ne soit tentée, et le hook `sitecustomize` trouvable
    # par n'importe quel interpréteur cible (cf. son docstring et celui de
    # `_make_exec_env` pour l'absence de risque de stdlib croisée). Seulement
    # pour .py : matplotlib n'existe que là.
    plot_dir: Optional[str] = None
    if target.suffix == ".py":
        plot_dir = tempfile.mkdtemp(prefix="epure_plots_")
        env["MPLBACKEND"] = "Agg"
        env["EPURE_PLOT_OUTPUT_DIR"] = plot_dir
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(_PLOT_SITECUSTOMIZE_DIR), env.get("PYTHONPATH", "")) if p
        )

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_EXEC_TIMEOUT, cwd=str(WORKSPACE), env=env,
        )
        dur = round((time.time() - t0) * 1000)
        logger.info("execute_code: %s → rc=%d in %dms", path, result.returncode, dur)
        out = {"stdout": result.stdout, "stderr": result.stderr,
               "returncode": result.returncode, "duration_ms": dur}
        if plot_dir is not None:
            out["images"] = _collect_plot_images(plot_dir, out)
        return out
    except subprocess.TimeoutExpired:
        # Le script a pu sauvegarder des figures avant le timeout, mais on ne
        # les retourne PAS ici : un timeout est déjà un signal d'erreur en soi,
        # pas la peine de le mélanger avec un résultat partiel. Le dossier est
        # quand même nettoyé (cf. finally ci-dessous).
        logger.warning("execute_code: timeout — %s", path)
        return {"stdout": "", "stderr": "Timeout (30s dépassé)", "returncode": -1,
                "duration_ms": _EXEC_TIMEOUT * 1000}
    except Exception as exc:
        logger.exception("execute_code: %s", path)
        return {"stdout": "", "stderr": str(exc), "returncode": -1, "duration_ms": 0}
    finally:
        if plot_dir is not None:
            shutil.rmtree(plot_dir, ignore_errors=True)


_PKG_RE = re.compile(r'^[a-zA-Z0-9_\-\.\[\]>=<~!,\s]+$')

# Packages that must use pre-compiled wheels (source build fails on Python 3.14+)
_BINARY_ONLY_PKGS = {
    "pygame", "numpy", "scipy", "pillow", "pil",
    "opencv-python", "cv2", "lxml", "psutil",
    "cryptography", "cffi", "greenlet",
}


def _pkg_base_name(package: str) -> str:
    """Extract base package name from a specifier like 'pygame==2.5.0'."""
    return re.split(r'[>=<!~\[]', package)[0].strip().lower()


_INSTALL_TIMEOUT = 300  # gros builds (3.14 compile parfois depuis les sources)


def _run_pip_install(cmd: list[str], env: dict) -> Generator:
    """Lance une commande pip et streame sa sortie. Yield des dicts line, puis un
    rc final via la clé '__rc__'."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(WORKSPACE), env=env,
    )
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            stripped = line.rstrip()
            if stripped:
                yield {"type": "line", "line": stripped}
        proc.wait(timeout=_INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        yield {"type": "error", "line": f"Timeout ({_INSTALL_TIMEOUT}s dépassé)"}
        yield {"__rc__": -1}
        return
    yield {"__rc__": proc.returncode}


def install_package(package: str) -> Generator:
    """Stream pip install output line by line. Yields dicts: line | done | error."""
    package = package.strip()
    if not package or not _PKG_RE.match(package):
        yield {"type": "error", "line": f"Nom de package invalide : {package}"}
        return

    base = _pkg_base_name(package)

    # Module déjà fourni par la stdlib → inutile (et impossible) à installer via pip.
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    if base.replace("-", "_") in stdlib:
        yield {"type": "line",
               "line": f"« {base} » fait partie de la bibliothèque standard Python — "
                       f"pas besoin de l'installer, importez-le directement."}
        yield {"type": "done", "returncode": 0, "package": package}
        return

    binary_only = base in _BINARY_ONLY_PKGS

    def _py_version(python: str) -> str:
        if python == sys.executable:
            return ".".join(str(v) for v in sys.version_info[:3])
        try:
            out = subprocess.run([python, "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or "?"
        except Exception:
            return "?"

    def _attempt(python: str) -> Generator:
        """Tente l'install sur un interpréteur donné : prefer-binary puis, en cas
        d'échec, only-binary. Yield les lignes, puis {'__rc__': code}.
        L'env est construit pour CET interpréteur (pas de PYTHONPATH croisé)."""
        env = _make_exec_env(python)  # pip a APPDATA/LOCALAPPDATA via _make_exec_env
        base_cmd = [python, "-m", "pip", "install", package,
                    "--progress-bar", "off", "--no-input", "--prefer-binary"]
        cmd = base_cmd + (["--only-binary", ":all:"] if binary_only else [])
        logger.info("install_package: %s", " ".join(cmd))
        rc = -1
        for ev in _run_pip_install(cmd, env):
            if "__rc__" in ev:
                rc = ev["__rc__"]
            else:
                yield ev
        if rc != 0 and not binary_only:
            yield {"type": "line",
                   "line": "↻ échec — nouvelle tentative en wheel précompilé (--only-binary :all:)…"}
            for ev in _run_pip_install(base_cmd + ["--only-binary", ":all:"], env):
                if "__rc__" in ev:
                    rc = ev["__rc__"]
                else:
                    yield ev
        yield {"__rc__": rc}

    global _exec_python_path
    try:
        # 1) Interpréteur primaire (celui qui exécute le code en ce moment).
        primary = _exec_python()
        prim_ver = _py_version(primary)
        yield {"type": "line", "line": f"$ pip install {package}  (Python {prim_ver})"}
        rc = -1
        for ev in _attempt(primary):
            if "__rc__" in ev:
                rc = ev["__rc__"]
            else:
                yield ev

        # 2) Repli : échec sur le primaire → on retente sur un autre Python (ex. 3.11)
        #    et, si ça marche, on bascule l'exécution dessus.
        if rc != 0:
            fallback = find_fallback_python()
            if fallback and fallback != primary:
                fb_ver = _py_version(fallback)
                yield {"type": "line",
                       "line": f"↻ échec sur Python {prim_ver} — bascule sur Python {fb_ver} ({fallback})…"}
                rc_fb = -1
                for ev in _attempt(fallback):
                    if "__rc__" in ev:
                        rc_fb = ev["__rc__"]
                    else:
                        yield ev
                if rc_fb == 0:
                    _exec_python_path = fallback
                    rc = 0
                    yield {"type": "line",
                           "line": f"✓ installé sur Python {fb_ver}. L'exécution utilisera "
                                   f"désormais cette version pour retrouver ce package."}
                else:
                    rc = rc_fb

        if rc != 0:
            fb = find_fallback_python()
            hint = (f"Aucun wheel compatible Python {prim_ver} pour « {base} »"
                    + ("" if fb else " — et aucun interpréteur de repli détecté (installez Python 3.11 "
                                     "ou définissez EPURE_PYTHON_FALLBACK)."))
            yield {"type": "line", "line": f"✗ pip a renvoyé le code {rc}. {hint}"}
        yield {"type": "done", "returncode": rc, "package": package}
    except Exception as exc:
        logger.exception("install_package: %s", package)
        yield {"type": "error", "line": str(exc)}
        yield {"type": "done", "returncode": -1, "package": package}


# ── Tool parser ─────────────────────────────────────────────────────────────

_TAG_RE = re.compile(
    r"<tool>\s*(?P<tool>\w+)\s*</tool>"
    r"(?:\s*<path>\s*(?P<path>[^<]*?)\s*</path>)?"
    r"(?:\s*<directory>\s*(?P<directory>[^<]*?)\s*</directory>)?"
    r"(?:\s*<args>\s*(?P<args>[^<]*?)\s*</args>)?"
    r"(?:\s*<content>([\s\S]*?)</content>)?"
    r"(?:\s*<old>([\s\S]*?)</old>)?"
    r"(?:\s*<new>([\s\S]*?)</new>)?",
    re.IGNORECASE,
)

_JSON_RE = re.compile(r'\{[^{}]*?"tool"\s*:\s*"(?P<tool>\w+)"[^{}]*?\}', re.DOTALL)

# **edit_file** `path` or **create_file** `path` followed by optional code block
_MD_TOOL_RE = re.compile(
    r'\*\*(?P<tool>create_file|edit_file|read_file|delete_file|list_files|execute_code)\*\*'
    r'\s+`(?P<path>[^`\n]+)`'
    r'(?:\s*\n```\w*\r?\n(?P<content>[\s\S]*?)\r?\n```)?',
    re.IGNORECASE,
)

# Standalone code block — used by the no-tool fallback
_CODE_BLOCK_RE = re.compile(r'```(?:\w+)?\r?\n([\s\S]*?)\r?\n```')


def parse_tool_calls(text: str) -> list[dict]:
    calls: list[dict] = []
    for m in _TAG_RE.finditer(text):
        g = m.groups()
        # Named: tool(0), path(1), directory(2), args(3); Unnamed: content(4), old(5), new(6)
        call: dict = {"tool": g[0]}
        if g[1]: call["path"] = g[1].strip()
        if g[2]: call["directory"] = g[2].strip()
        if g[3]: call["args"] = g[3].strip()
        if g[4] is not None: call["content"] = g[4]
        if g[5] is not None: call["old"] = g[5]
        if g[6] is not None: call["new"] = g[6]
        calls.append(call)
    if not calls:
        for m in _JSON_RE.finditer(text):
            try:
                calls.append(json.loads(m.group(0)))
            except json.JSONDecodeError:
                pass
    if not calls:
        for m in _MD_TOOL_RE.finditer(text):
            tool = m.group("tool").lower()
            path = m.group("path").strip()
            content = m.group("content")
            call: dict = {"tool": tool, "path": path}
            if content is not None:
                if tool == "edit_file":
                    # Markdown gives full content only — treat as full file write
                    call["tool"] = "create_file"
                call["content"] = content
            calls.append(call)
    return calls


def dispatch_tool(call: dict) -> dict:
    tool = call.get("tool", "")
    try:
        if tool == "create_file":
            return {"status": "success", "result": create_file(call["path"], call.get("content", ""))}
        elif tool == "read_file":
            return {"status": "success", "result": read_file(call["path"])}
        elif tool == "edit_file":
            return {"status": "success", "result": edit_file(call["path"], call.get("old", ""), call.get("new", ""))}
        elif tool == "list_files":
            return {"status": "success", "result": list_files_text(call.get("directory", "."))}
        elif tool == "delete_file":
            return {"status": "success", "result": delete_path(call["path"])}
        elif tool == "execute_code":
            return {"needs_confirm": True, "path": call.get("path", ""), "args": call.get("args", "")}
        elif tool == "compile_latex":
            r = compile_latex(call.get("path", ""))
            if r.get("returncode", -1) == 0:
                return {"status": "success", "result": r.get("stdout", "✓ Compilé")}
            return {"status": "error", "result": r.get("stderr", "Erreur compilation LaTeX")}
        else:
            return {"status": "error", "result": f"Outil inconnu : {tool}"}
    except SecurityError as e:
        return {"status": "error", "result": str(e)}
    except Exception as e:
        logger.exception("dispatch_tool: %s", tool)
        return {"status": "error", "result": str(e)}


# ── System prompt ────────────────────────────────────────────────────────────

_CODE_KEYWORDS = frozenset([
    "crée", "créer", "écris", "écrire", "fais", "faire", "génère", "générer",
    "script", "fonction", "programme", "code", "implémente", "implémenter",
    "développe", "construis", "ajoute", "modifie", "corrige",
])


def _is_code_request(message: str) -> bool:
    words = set(message.lower().split())
    return bool(words & _CODE_KEYWORDS)


def _approx_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.3))


# ── Verify / Tests ────────────────────────────────────────────────────────────

def verify_code(path: str, llm, model: Optional[str] = None) -> str:
    """Analyse le code avec le LLM local. Retourne '✓ Code OK' ou liste de problèmes."""
    try:
        content = read_file(path)
        if content.startswith("Erreur"):
            return "✓ Pas de vérification disponible"
        prompt = (
            "Analyse ce code. Si correct, réponds UNIQUEMENT '✓ Code OK'. "
            "Sinon, liste les problèmes de façon concise (5 lignes max).\n\n"
            f"Code ({path}):\n{content[:3000]}"
        )
        result = llm.generate([{"role": "user", "content": prompt}], model=model)
        return result.strip() or "✓ Pas de vérification disponible"
    except Exception:
        logger.exception("verify_code: %s", path)
        return "✓ Pas de vérification disponible"


def generate_tests(path: str, llm, model: Optional[str] = None) -> Generator:
    """Stream les tokens de tests unitaires et crée test_<name>.py."""
    try:
        content = read_file(path)
        if content.startswith("Erreur"):
            return
        stem = Path(path).stem
        test_path = str(Path(path).parent / f"test_{stem}.py")
        prompt = (
            "Génère 3-5 tests unitaires simples pour ce code. "
            "Utilise unittest. Génère UNIQUEMENT le code Python, sans bloc markdown.\n\n"
            f"Code ({path}):\n{content[:3000]}"
        )
        test_content = ""
        for token in llm.stream([{"role": "user", "content": prompt}], model=model, max_tokens=1024):
            if isinstance(token, str):
                test_content += token
                yield token
        if test_content.strip():
            cleaned = re.sub(r"```(?:python)?\n?|```\n?", "", test_content).strip()
            try:
                create_file(test_path, cleaned)
            except Exception:
                logger.exception("generate_tests: create %s", test_path)
    except Exception:
        logger.exception("generate_tests: %s", path)


_SYSTEM = """\
Tu es un agent de coding expert. Tu travailles dans un workspace isolé.
Utilise ces outils avec la syntaxe XML exacte :

<tool>create_file</tool><path>src/main.py</path><content>
# code ici
</content>

<tool>read_file</tool><path>src/main.py</path>

<tool>edit_file</tool><path>src/main.py</path><old>ancien texte</old><new>nouveau texte</new>

<tool>list_files</tool><directory>.</directory>

<tool>delete_file</tool><path>src/main.py</path>

<tool>execute_code</tool><path>src/main.py</path>

<tool>compile_latex</tool><path>rapport.tex</path>

Règles : explique ce que tu fais, crée des fichiers complets et fonctionnels, ne sors jamais du workspace.

RÈGLE ABSOLUE : Tu ne peux PAS modifier un fichier en montrant du code dans ta réponse. Tu DOIS utiliser les tools.
Si tu veux modifier un fichier, utilise TOUJOURS edit_file ou create_file.
Jamais de bloc ```code``` pour montrer des modifications — utilise les tools.
Si tu n'utilises pas de tool, le fichier ne sera PAS modifié.

Fichier actif : {file_context}
Arborescence :
{tree}
"""


_VERIFIABLE_EXTS = {".py", ".js", ".ts"}
_CRITICAL_WORDS = {"bug critique", "crash", "traceback", "exception non gérée", "segfault"}


class CodeAgent:
    def __init__(self, llm):
        self._llm = llm

    def run_turn(
        self,
        message: str,
        file_context: str,
        model: Optional[str] = None,
        reflection_model: Optional[str] = None,
        pipeline: Optional[dict] = None,
        history: Optional[list] = None,
    ) -> Generator:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        system = _SYSTEM.format(
            file_context=file_context or "(aucun)",
            tree=list_files_text("."),
        )

        # Resolve per-step config from pipeline (if provided) or legacy params
        def _step(name: str, fallback_model, fallback_enabled: bool = True):
            if pipeline and name in pipeline:
                cfg = pipeline[name]
                return cfg.get("enabled", True), cfg.get("model") or fallback_model
            return fallback_enabled, fallback_model

        ref_enabled, eff_ref_model = _step("reflection", reflection_model)
        _, eff_code_model = _step("code", model)
        _, eff_ver_model = _step("verification", None)
        tests_enabled, _ = _step("tests", None)

        # ── Réflexion (LLM cloud) ────────────────────────────────────────────
        reflection_ctx = ""
        if ref_enabled and eff_ref_model and _is_code_request(message):
            yield {"type": "reflection_start"}
            ref_msgs = [
                {
                    "role": "system",
                    "content": (
                        "Tu es en phase de RÉFLEXION uniquement. "
                        "N'écris PAS de code. N'utilise PAS de blocs ```code```.\n"
                        "Réfléchis uniquement en prose : architecture, edge cases, "
                        "approche, pièges potentiels, structure suggérée.\n"
                        "Maximum 200 mots. Sois concis et direct."
                    ),
                },
                {"role": "user", "content": message},
            ]
            ref_full = ""
            ref_tok = 0
            for item in self._llm.stream(ref_msgs, model=eff_ref_model, max_tokens=800):
                if isinstance(item, str):
                    ref_full += item
                    yield {"type": "reflection_token", "content": item}
                elif isinstance(item, dict) and item.get("__stats__"):
                    ref_tok = item.get("output_tokens", 0)
            ref_tok = ref_tok or _approx_tokens(ref_full)
            reflection_ctx = ref_full
            yield {"type": "reflection_done"}
            yield {"type": "tokens", "step": "reflection", "count": ref_tok}

        # ── Génération code ──────────────────────────────────────────────────
        messages: list[dict] = [{"role": "system", "content": system}]
        # Contexte des tours précédents : l'IA voit ce qui a déjà été demandé et
        # répondu (utile surtout pour corriger un code généré juste avant).
        if history:
            for h in history:
                role = h.get("role")
                text = (h.get("content") or "").strip()
                if role in ("user", "assistant") and text:
                    messages.append({"role": role, "content": text[:4000]})
        if reflection_ctx:
            messages.append({
                "role": "system",
                "content": f"Ta réflexion préalable :\n{reflection_ctx}",
            })
        messages.append({"role": "user", "content": message})

        full = ""
        gen_tok = 0
        for item in self._llm.stream(messages, model=eff_code_model, max_tokens=4096):
            if isinstance(item, str):
                full += item
                yield {"type": "token", "content": item}
            elif isinstance(item, dict) and item.get("__stats__"):
                gen_tok = item.get("output_tokens", 0)
        gen_tok = gen_tok or _approx_tokens(full)
        yield {"type": "tokens", "step": "generation", "count": gen_tok}

        # ── Exécution des tools ──────────────────────────────────────────────
        created_files: list[str] = []
        edited_files: list[str] = []
        deleted_files: list[str] = []
        calls = parse_tool_calls(full)

        # Fallback : code block présent mais aucun tool appelé
        if not calls:
            cb = _CODE_BLOCK_RE.search(full)
            if cb:
                active_path = (file_context or "").split("\n")[0].strip()
                if active_path:
                    code_content = cb.group(1)
                    yield {"type": "warning", "content": "Tool non utilisé — modification appliquée automatiquement"}
                    auto_call = {"tool": "create_file", "path": active_path, "content": code_content}
                    yield {"type": "tool_call", "tool": "create_file", "path": active_path, "status": "pending"}
                    res = dispatch_tool(auto_call)
                    yield {
                        "type": "tool_result", "tool": "create_file", "path": active_path,
                        "result": res.get("result", ""), "status": res.get("status", "error"),
                    }
                    if res.get("status") == "success":
                        created_files.append(active_path)

        for call in calls:
            tool_name = call.get("tool", "")
            path = call.get("path", "")
            yield {"type": "tool_call", "tool": tool_name, "path": path, "status": "pending"}
            result = dispatch_tool(call)
            if result.get("needs_confirm"):
                yield {"type": "execute_request", "path": result["path"], "args": result.get("args", "")}
            else:
                yield {
                    "type": "tool_result",
                    "tool": tool_name,
                    "path": path,
                    "result": result.get("result", ""),
                    "status": result.get("status", "error"),
                }
                if result.get("status") == "success":
                    if tool_name == "create_file":
                        created_files.append(path)
                    elif tool_name in ("edit_file", "write_file", "patch_file"):
                        edited_files.append(path)
                    elif tool_name == "delete_file":
                        deleted_files.append(path)

        # ── Vérification (toujours active, modèle configurable) ──────────────
        for fpath in created_files:
            if Path(fpath).suffix not in _VERIFIABLE_EXTS:
                continue
            yield {"type": "verification_start", "path": fpath}
            ver_result = verify_code(fpath, self._llm, model=eff_ver_model)
            ver_tok = _approx_tokens(ver_result) + 80
            yield {"type": "verification_done", "path": fpath, "result": ver_result}
            yield {"type": "tokens", "step": "verification", "count": ver_tok}

            # Proposer tests si activés et pas d'erreurs critiques
            result_lower = ver_result.lower()
            if tests_enabled and not any(w in result_lower for w in _CRITICAL_WORDS):
                yield {"type": "tests_prompt", "path": fpath}

        # ── Conclusion : une phrase résumant les changements ─────────────────
        if created_files or edited_files or deleted_files:
            changes = []
            if created_files:
                changes.append("créé " + ", ".join(dict.fromkeys(created_files)))
            if edited_files:
                changes.append("modifié " + ", ".join(dict.fromkeys(edited_files)))
            if deleted_files:
                changes.append("supprimé " + ", ".join(dict.fromkeys(deleted_files)))
            facts = " ; ".join(changes)
            sentence = ""
            try:
                concl_msgs = [
                    {"role": "system", "content":
                        "Résume en UNE phrase courte (français, à la première personne) "
                        "ce qui vient d'être fait, à partir des faits fournis. "
                        "Pas de liste, pas de markdown, pas de code."},
                    {"role": "user", "content":
                        f"Demande : {message}\nFichiers touchés : {facts}\n"
                        f"Réponse produite (extrait) : {full[:1200]}"},
                ]
                for item in self._llm.stream(concl_msgs, model=eff_code_model, max_tokens=120):
                    if isinstance(item, str):
                        sentence += item
            except Exception:
                logger.exception("Conclusion run_turn")
            sentence = sentence.strip() or f"C'est fait : j'ai {facts}."
            yield {"type": "conclusion", "content": sentence,
                   "created": created_files, "edited": edited_files, "deleted": deleted_files}
            yield {"type": "tokens", "step": "generation", "count": _approx_tokens(sentence) + 40}

        yield {"type": "done"}
