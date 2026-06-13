"""Validation de sécurité/qualité d'un module AVANT activation.

Deux frontières distinctes (cf. atelier) :
  - GÉNÉRATION : confinée au disque (modules/_staging) — gérée ailleurs.
  - EXÉCUTION : confiner le dossier ne protège PAS — un router activé est importé
    dans le process FastAPI. Donc validation OBLIGATOIRE avant toute activation :
      * router.py : ast.parse + refus import subprocess/socket/importlib,
        appels os.system/os.popen/eval/exec/compile/__import__, accès aux
        variables sensibles (*_API_KEY via _SENSITIVE/_EXPLICIT_DENY) ;
        exige `router = APIRouter()`.
      * Component.tsx : refus dangerouslySetInnerHTML/eval + `tsc --noEmit`.

En cas d'échec, le module reste en draft avec ce rapport.
"""

import ast
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Réutilise les garde-fous de codeagent (source unique de vérité).
from core.codeagent import _EXPLICIT_DENY, _SENSITIVE, _make_exec_env

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"

# Modules dont l'import seul est refusé dans un router généré.
_FORBIDDEN_IMPORTS = {"subprocess", "socket", "importlib", "ctypes", "multiprocessing"}
# Appels de fonctions interdits (par nom simple).
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "globals", "vars"}
# Attributs os.* interdits (exécution de process / shell).
_FORBIDDEN_OS_ATTRS = {"system", "popen", "spawn", "spawnl", "spawnv", "exec", "execv",
                       "execvp", "execve", "execl", "fork", "posix_spawn"}

_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass
class ValidationReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def _is_sensitive_name(name: str) -> bool:
    """True si `name` ressemble à une variable sensible (clé/token/secret)."""
    if name in _EXPLICIT_DENY:
        return True
    upper = name.upper()
    return _ENV_KEY_RE.match(name) is not None and any(s in upper for s in _SENSITIVE)


class _RouterChecker(ast.NodeVisitor):
    def __init__(self, report: ValidationReport):
        self.r = report
        self.has_router = False

    # imports interdits
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in _FORBIDDEN_IMPORTS:
                self.r.fail(f"Import interdit : `import {alias.name}` (ligne {node.lineno})")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root in _FORBIDDEN_IMPORTS:
            self.r.fail(f"Import interdit : `from {node.module} import ...` (ligne {node.lineno})")
        self.generic_visit(node)

    # appels interdits + détection router = APIRouter()
    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        if isinstance(fn, ast.Name):
            if fn.id in _FORBIDDEN_CALLS:
                self.r.fail(f"Appel interdit : `{fn.id}(...)` (ligne {node.lineno})")
            if fn.id == "APIRouter":
                self.has_router = True
        elif isinstance(fn, ast.Attribute):
            # os.system / os.popen / os.exec*  ou  subprocess.* / importlib.*
            if isinstance(fn.value, ast.Name):
                base = fn.value.id
                if base == "os" and fn.attr in _FORBIDDEN_OS_ATTRS:
                    self.r.fail(f"Appel interdit : `os.{fn.attr}(...)` (ligne {node.lineno})")
                if base in _FORBIDDEN_IMPORTS:
                    self.r.fail(f"Appel interdit : `{base}.{fn.attr}(...)` (ligne {node.lineno})")
            if fn.attr == "APIRouter":
                self.has_router = True
        self.generic_visit(node)

    # accès aux variables sensibles (chaînes de type *_API_KEY)
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _is_sensitive_name(node.value):
            self.r.fail(f"Accès à une variable sensible interdit : {node.value!r} (ligne {node.lineno})")
        self.generic_visit(node)

    # router = APIRouter()  (assignation top-level/quelconque)
    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "router":
                self.has_router = True
        self.generic_visit(node)


def validate_router_py(source: str) -> ValidationReport:
    """Valide le router.py d'un module (gate d'exécution)."""
    report = ValidationReport()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        report.fail(f"Erreur de syntaxe Python : {exc}")
        return report

    checker = _RouterChecker(report)
    checker.visit(tree)

    if not checker.has_router:
        report.fail("router.py doit définir `router = APIRouter()`.")
    if "APIRouter" not in source:
        report.fail("router.py doit importer et instancier APIRouter (fastapi).")

    return report


_TSX_DENY = [
    ("dangerouslySetInnerHTML", "dangerouslySetInnerHTML est interdit (injection HTML)."),
]
_TSX_EVAL_RE = re.compile(r"\beval\s*\(")
_TSX_NEWFUNC_RE = re.compile(r"\bnew\s+Function\s*\(")


def validate_component_tsx(source: str, module_id: str, run_tsc: bool = True) -> ValidationReport:
    """Valide le composant frontend : motifs interdits + tsc --noEmit (best-effort)."""
    report = ValidationReport()

    for needle, msg in _TSX_DENY:
        if needle in source:
            report.fail(msg)
    if _TSX_EVAL_RE.search(source):
        report.fail("eval(...) est interdit dans le composant.")
    if _TSX_NEWFUNC_RE.search(source):
        report.fail("new Function(...) est interdit dans le composant.")

    if run_tsc:
        _run_tsc(source, module_id, report)

    return report


def typecheck_component(source: str, module_id: str) -> ValidationReport:
    """Type-check tsc SEUL (best-effort, warnings) — lancé en tâche de fond,
    découplé du gate rapide qui débloque la revue."""
    report = ValidationReport()
    _run_tsc(source, module_id, report)
    return report


def _run_tsc(source: str, module_id: str, report: ValidationReport) -> None:
    """Vérifie le composant via `npx tsc --noEmit` dans le contexte du projet.

    Le fichier est écrit temporairement à la même profondeur que sa cible réelle
    (src/modules/generated/<id>/) pour que les imports relatifs résolvent, puis
    supprimé. Best-effort : si la toolchain est absente, on émet un warning sans
    bloquer (le gate d'exécution reste l'AST de router.py).
    """
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx or not (_FRONTEND_DIR / "package.json").is_file():
        report.warn("tsc indisponible (npx/projet absent) — vérification de type ignorée.")
        return

    safe_id = re.sub(r"[^a-z0-9_]", "", module_id.lower()) or "mod"
    check_dir = _FRONTEND_DIR / "src" / "modules" / "generated" / f"_workshop_check_{safe_id}"
    check_file = check_dir / "Component.tsx"
    try:
        check_dir.mkdir(parents=True, exist_ok=True)
        check_file.write_text(source, encoding="utf-8")
        proc = subprocess.run(
            [npx, "tsc", "--noEmit", "-p", "tsconfig.app.json"],
            cwd=str(_FRONTEND_DIR),
            capture_output=True, text=True, timeout=120,
            env=_make_exec_env(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rel = f"_workshop_check_{safe_id}/Component.tsx"
        errs = [ln for ln in out.splitlines() if rel in ln or rel.replace("/", "\\") in ln]
        if errs:
            report.fail("Erreurs TypeScript dans le composant :\n" + "\n".join(errs[:15]))
        elif proc.returncode != 0 and out.strip():
            # tsc a échoué globalement (autres fichiers) — on n'en tient pas le
            # module pour responsable, mais on le signale.
            report.warn("tsc a rapporté des erreurs hors de ce module (projet).")
    except subprocess.TimeoutExpired:
        report.warn("tsc : timeout (120s) — vérification de type incomplète.")
    except Exception:
        logger.exception("Erreur exécution tsc pour %s", module_id)
        report.warn("tsc : exécution impossible — vérification de type ignorée.")
    finally:
        try:
            shutil.rmtree(check_dir, ignore_errors=True)
        except Exception:
            logger.exception("Nettoyage tsc check dir %s", check_dir)
