import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator, Optional

logger = logging.getLogger(__name__)

WORKSPACE = Path("C:/Users/Ilyan/epure/workspace").resolve()

GUI_LIBS = frozenset(["pygame", "tkinter", "turtle", "wx", "PyQt", "pyglet", "kivy"])

_EXEC_CMDS: dict[str, list[str] | None] = {
    ".py":   ["python", "-u"],
    ".js":   ["node"],
    ".ts":   ["npx", "ts-node"],
    ".sh":   ["bash"],
    ".html": None,  # preview only
    ".tex":  None,  # compile via compile_latex
}
_MAX_READ = 50_000
_EXEC_TIMEOUT = 30


class SecurityError(Exception):
    pass


def _safe_path(relative: str) -> Path:
    """Resolve path and abort if it escapes the workspace."""
    target = (WORKSPACE / relative).resolve()
    if not str(target).startswith(str(WORKSPACE)):
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


_SENSITIVE = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS")
_EXPLICIT_DENY = {
    "GROQ_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY",
    "NVIDIA_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
}


def _make_exec_env() -> dict:
    """Minimal env with Python packages accessible, no sensitive vars."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONIOENCODING": "utf-8",
    }
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
        return {"stdout": "", "stderr": "pdflatex non installé — installez TeX Live ou MiKTeX", "returncode": -1, "duration_ms": 0}
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


def execute_code(path: str, args: str = "") -> dict:
    target = _safe_path(path)
    if target.suffix not in _EXEC_CMDS:
        return {"stdout": "", "stderr": f"Extension non autorisée : {target.suffix}", "returncode": -1, "duration_ms": 0}
    if not target.exists():
        return {"stdout": "", "stderr": f"Fichier introuvable : {path}", "returncode": -1, "duration_ms": 0}

    # HTML → preview seulement
    if target.suffix == ".html":
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"html_preview": True, "content": content, "stdout": "", "stderr": "", "returncode": 0, "duration_ms": 0}

    # LaTeX → compilation
    if target.suffix == ".tex":
        return compile_latex(path)

    # Python avec lib GUI → lancer dans une fenêtre externe
    if target.suffix == ".py":
        try:
            src = target.read_text(encoding="utf-8", errors="replace")
            if any(lib in src for lib in GUI_LIBS):
                try:
                    subprocess.Popen(["python", str(target)], cwd=str(WORKSPACE))
                    return {"external": True, "stdout": "", "stderr": "", "returncode": 0, "duration_ms": 0}
                except Exception as exc:
                    return {"stdout": "", "stderr": str(exc), "returncode": -1, "duration_ms": 0}
        except Exception:
            pass

    cmd_base = _EXEC_CMDS[target.suffix]
    cmd = cmd_base + [str(target)]  # type: ignore[operator]

    if args.strip():
        cmd += args.strip().split()

    env = _make_exec_env()

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_EXEC_TIMEOUT, cwd=str(WORKSPACE), env=env,
        )
        dur = round((time.time() - t0) * 1000)
        logger.info("execute_code: %s → rc=%d in %dms", path, result.returncode, dur)
        return {"stdout": result.stdout, "stderr": result.stderr,
                "returncode": result.returncode, "duration_ms": dur}
    except subprocess.TimeoutExpired:
        logger.warning("execute_code: timeout — %s", path)
        return {"stdout": "", "stderr": "Timeout (30s dépassé)", "returncode": -1,
                "duration_ms": _EXEC_TIMEOUT * 1000}
    except Exception as exc:
        logger.exception("execute_code: %s", path)
        return {"stdout": "", "stderr": str(exc), "returncode": -1, "duration_ms": 0}


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


def install_package(package: str) -> Generator:
    """Stream pip install output line by line. Yields dicts: line | done | error."""
    package = package.strip()
    if not package or not _PKG_RE.match(package):
        yield {"type": "error", "line": f"Nom de package invalide : {package}"}
        return

    base = _pkg_base_name(package)
    binary_only = base in _BINARY_ONLY_PKGS

    logger.info("install_package: pip install %s (binary_only=%s)", package, binary_only)
    cmd = [sys.executable, "-m", "pip", "install", package,
           "--quiet", "--progress-bar", "off"]
    if binary_only:
        cmd += ["--only-binary", ":all:"]

    env = _make_exec_env()
    # pip needs APPDATA/LOCALAPPDATA on Windows for its cache
    for k in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        if os.environ.get(k):
            env[k] = os.environ[k]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(WORKSPACE), env=env,
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            stripped = line.rstrip()
            if stripped:
                yield {"type": "line", "line": stripped}
        proc.wait(timeout=120)
        if proc.returncode == 0:
            yield {"type": "done", "returncode": 0, "package": package}
        else:
            yield {"type": "done", "returncode": proc.returncode, "package": package}
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.warning("install_package: timeout — %s", package)
        yield {"type": "error", "line": "Timeout (120s dépassé)"}
    except Exception as exc:
        logger.exception("install_package: %s", package)
        yield {"type": "error", "line": str(exc)}


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
        for call in parse_tool_calls(full):
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
                if tool_name == "create_file" and result.get("status") == "success":
                    created_files.append(path)

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

        yield {"type": "done"}
