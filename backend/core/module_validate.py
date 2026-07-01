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
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Réutilise les garde-fous de codeagent (source unique de vérité).
from core.codeagent import _EXPLICIT_DENY, _SENSITIVE, _make_exec_env

logger = logging.getLogger(__name__)

# Pas de fenêtre console visible quand on lance tsc/npx (.cmd) sous Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"

# Modules dont l'import seul est TOUJOURS refusé (exécution de process, etc.).
_ALWAYS_FORBIDDEN_IMPORTS = {"subprocess", "socket", "importlib", "ctypes", "multiprocessing"}
# Modules réseau : refusés dans un module généré (doit passer par core.runtime),
# tolérés en ré-édition d'un module core (chat/settings utilisent urllib).
_NETWORK_IMPORTS = {"urllib", "http", "requests", "httpx", "aiohttp"}
# Racines de modules « système » : cibles d'un getattr dynamique = suspect.
_SYSTEM_MODULE_ROOTS = _ALWAYS_FORBIDDEN_IMPORTS | _NETWORK_IMPORTS | {"os"}
# Appels de fonctions interdits (par nom simple).
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "globals", "vars"}
# Attributs os.* interdits (exécution de process / shell).
_FORBIDDEN_OS_ATTRS = {"system", "popen", "spawn", "spawnl", "spawnv", "exec", "execv",
                       "execvp", "execve", "execl", "fork", "posix_spawn"}
# Méthodes de décoration de route sur `router`.
_ROUTE_METHODS = {"get", "post", "put", "delete", "patch", "head", "options",
                  "websocket", "api_route", "route"}

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


def _const_str(node: Optional[ast.AST]) -> Optional[str]:
    """Valeur d'une chaîne littérale, ou None si l'expression n'est pas une
    constante str (variable, concaténation, appel… = non prouvable sûr)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _RouterChecker(ast.NodeVisitor):
    """Gate d'exécution d'un router généré. Résout les alias d'import (``import
    os as o``, ``from os import environ``) pour que les contournements par
    renommage ne passent pas."""

    def __init__(self, report: ValidationReport, module_id: str = "",
                 backend_prefix: Optional[str] = None, is_core: bool = False):
        self.r = report
        self.module_id = module_id
        self.backend_prefix = backend_prefix
        self.is_core = is_core
        self.has_router = False
        # alias de module → racine réelle : {"o": "os", "sp": "subprocess"}
        self.module_aliases: dict[str, str] = {}
        # noms liés à os.environ / os.getenv via `from os import ...`
        self.env_names: set[str] = set()
        self.getenv_names: set[str] = set()

    # ── Pré-passe : cartographie des alias avant l'analyse des usages ────────
    def collect_aliases(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    bound = alias.asname or root
                    self.module_aliases[bound] = root
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "os":
                for alias in node.names:
                    if alias.name == "environ":
                        self.env_names.add(alias.asname or "environ")
                    elif alias.name == "getenv":
                        self.getenv_names.add(alias.asname or "getenv")

    def _resolve(self, name: str) -> str:
        """Racine réelle d'un nom (via alias), ou le nom lui-même."""
        return self.module_aliases.get(name, name)

    def _network_blocked(self) -> bool:
        return not self.is_core

    # ── Imports ──────────────────────────────────────────────────────────────
    def _check_import_root(self, root: str, node: ast.AST, label: str) -> None:
        if root in _ALWAYS_FORBIDDEN_IMPORTS:
            self.r.fail(f"Import interdit : `{label}` (ligne {node.lineno})")
        elif root in _NETWORK_IMPORTS and self._network_blocked():
            self.r.fail(
                f"Import réseau interdit : `{label}` (ligne {node.lineno}) — "
                f"tout accès réseau/LLM doit passer par core.runtime."
            )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import_root(alias.name.split(".")[0], node, f"import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        self._check_import_root(root, node, f"from {node.module} import ...")
        # `from os import system/popen/...` : importer la fonction dangereuse
        # elle-même (avec ou sans alias) est refusé.
        if root == "os":
            for alias in node.names:
                if alias.name in _FORBIDDEN_OS_ATTRS:
                    self.r.fail(
                        f"Import interdit : `from os import {alias.name}` (ligne {node.lineno})"
                    )
        self.generic_visit(node)

    # ── Accès à l'environnement (secrets) ────────────────────────────────────
    def _is_environ_target(self, node: ast.AST) -> bool:
        """`node` désigne-t-il os.environ (via os.environ ou un from-import) ?"""
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            return isinstance(node.value, ast.Name) and self._resolve(node.value.id) == "os"
        return isinstance(node, ast.Name) and node.id in self.env_names

    def _check_env_key(self, key: Optional[ast.AST], node: ast.AST) -> None:
        """La clé d'accès à l'environnement doit être une constante (sinon on ne
        peut pas prouver qu'elle ne vise pas un secret par concaténation)."""
        if self.is_core:
            return  # ré-édition core : clés dynamiques légitimes tolérées
        if _const_str(key) is None:
            self.r.fail(
                f"Accès à l'environnement par clé non constante interdit "
                f"(ligne {node.lineno}) — risque d'accès à un secret par calcul."
            )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_environ_target(node.value):
            self._check_env_key(node.slice, node)
        self.generic_visit(node)

    # ── Appels ────────────────────────────────────────────────────────────────
    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        if isinstance(fn, ast.Name):
            if fn.id in _FORBIDDEN_CALLS:
                self.r.fail(f"Appel interdit : `{fn.id}(...)` (ligne {node.lineno})")
            elif fn.id == "getattr" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Name) and self._resolve(arg0.id) in _SYSTEM_MODULE_ROOTS:
                    self.r.fail(
                        f"`getattr` sur un module système ({self._resolve(arg0.id)}) interdit "
                        f"(ligne {node.lineno}) — résolution dynamique d'attribut."
                    )
            elif fn.id in self.getenv_names and node.args:
                self._check_env_key(node.args[0], node)
            elif fn.id == "APIRouter":
                self.has_router = True
        elif isinstance(fn, ast.Attribute):
            if isinstance(fn.value, ast.Name):
                real = self._resolve(fn.value.id)
                if real == "os" and fn.attr in _FORBIDDEN_OS_ATTRS:
                    self.r.fail(f"Appel interdit : `os.{fn.attr}(...)` (ligne {node.lineno})")
                elif real in _ALWAYS_FORBIDDEN_IMPORTS:
                    self.r.fail(f"Appel interdit : `{real}.{fn.attr}(...)` (ligne {node.lineno})")
                elif real in _NETWORK_IMPORTS and self._network_blocked():
                    self.r.fail(f"Appel réseau interdit : `{real}.{fn.attr}(...)` (ligne {node.lineno})")
                elif real == "os" and fn.attr == "getenv" and node.args:
                    self._check_env_key(node.args[0], node)
            # os.environ.get(<clé>) / environ.get(<clé>)
            if fn.attr == "get" and self._is_environ_target(fn.value) and node.args:
                self._check_env_key(node.args[0], node)
            if fn.attr == "APIRouter":
                self.has_router = True
        self.generic_visit(node)

    # ── Variables sensibles littérales (*_API_KEY, TOKEN, SECRET…) ────────────
    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _is_sensitive_name(node.value):
            self.r.fail(f"Accès à une variable sensible interdit : {node.value!r} (ligne {node.lineno})")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "router":
                self.has_router = True
        self.generic_visit(node)

    # ── Préfixe des routes ─────────────────────────────────────────────────────
    def _check_route_prefix(self, fnode) -> None:
        """Quand backend.prefix est vide, chaque route doit commencer par
        /<module_id> (sinon un module généré peut masquer une route core)."""
        if self.backend_prefix != "" or not self.module_id:
            return  # prefix non vide (mount préfixe déjà) ou id inconnu
        expected = f"/{self.module_id}"
        for dec in fnode.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if not (isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "router"
                    and dec.func.attr in _ROUTE_METHODS):
                continue
            path = _const_str(dec.args[0]) if dec.args else None
            if path is None:
                self.r.fail(
                    f"Chemin de route non littéral (ligne {dec.lineno}) — impossible "
                    f"de vérifier le préfixe /{self.module_id}."
                )
            elif not (path == expected or path.startswith(expected + "/")):
                self.r.fail(
                    f"Route `{path}` (ligne {dec.lineno}) non préfixée par "
                    f"`{expected}` — collision possible avec une route core."
                )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_route_prefix(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_route_prefix(node)
        self.generic_visit(node)


def validate_router_py(source: str, module_id: str = "",
                       backend_prefix: Optional[str] = None,
                       is_core: bool = False) -> ValidationReport:
    """Valide le router.py d'un module (gate d'exécution).

    - ``module_id`` / ``backend_prefix`` : si le prefix de montage est vide (""),
      chaque route doit être préfixée par ``/<module_id>``.
    - ``is_core`` : ré-édition d'un module core (chat/settings) → tolère les
      imports réseau et les clés d'environnement dynamiques (les modules générés,
      eux, doivent passer par core.runtime).
    """
    report = ValidationReport()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        report.fail(f"Erreur de syntaxe Python : {exc}")
        return report

    checker = _RouterChecker(report, module_id, backend_prefix, is_core)
    checker.collect_aliases(tree)
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

# Import depuis le barrel UI partagé : `import { A, B } from '.../components/ui'`.
_UI_IMPORT_RE = re.compile(
    r"import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['\"][^'\"]*components/ui(?:/index)?(?:\.ts)?['\"]"
)
_UI_INDEX = _FRONTEND_DIR / "src" / "components" / "ui" / "index.ts"


def ui_component_exports() -> list[str]:
    """Noms réellement exportés par src/components/ui/index.ts.

    Source unique de vérité partagée par la validation ET le prompt de l'atelier,
    pour que le LLM n'invente pas de composants (Label, CardHeader…) inexistants.
    """
    try:
        src = _UI_INDEX.read_text(encoding="utf-8")
    except OSError:
        return []
    names: set[str] = set()
    # export { default as Button } from './Button'  ou  export { A, B as C } ...
    for block in re.findall(r"export\s*\{([^}]*)\}", src):
        for part in block.split(","):
            part = part.strip()
            if not part:
                continue
            # 'default as Button' / 'Foo as Bar' → on garde le nom exposé (après 'as').
            m = re.search(r"\bas\s+(\w+)$", part)
            names.add(m.group(1) if m else part)
    # export const/function/class X  /  export default X
    for m in re.finditer(r"export\s+(?:const|function|class)\s+(\w+)", src):
        names.add(m.group(1))
    names.discard("default")
    return sorted(names)


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

    # Imports UI : refuse tout composant inexistant dans components/ui (cause
    # fréquente du crash « does not provide an export named 'X' » à l'activation).
    allowed = ui_component_exports()
    if allowed:
        imported: set[str] = set()
        for block in _UI_IMPORT_RE.findall(source):
            for part in block.split(","):
                part = part.strip()
                if not part:
                    continue
                # 'Foo as Bar' → le nom importé réel est 'Foo' (avant 'as').
                name = re.split(r"\s+as\s+", part)[0].strip()
                if name:
                    imported.add(name)
        unknown = sorted(n for n in imported if n not in allowed)
        if unknown:
            report.fail(
                f"Composant(s) UI inexistant(s) importé(s) depuis components/ui : "
                f"{', '.join(unknown)}. Exports disponibles : {', '.join(allowed)}. "
                f"N'importez QUE ceux-là (ou créez vos éléments avec des balises HTML)."
            )

    if run_tsc:
        _run_tsc(source, module_id, report)

    return report


def typecheck_component(source: str, module_id: str) -> ValidationReport:
    """Type-check tsc SEUL (best-effort, warnings) — lancé en tâche de fond,
    découplé du gate rapide qui débloque la revue."""
    report = ValidationReport()
    _run_tsc(source, module_id, report)
    return report


def _tsc_command() -> Optional[list[str]]:
    """Binaire tsc à utiliser : local au projet en priorité, sinon `npx --yes tsc`."""
    if not (_FRONTEND_DIR / "package.json").is_file():
        return None
    local = _FRONTEND_DIR / "node_modules" / ".bin" / ("tsc.cmd" if os.name == "nt" else "tsc")
    if local.is_file():
        return [str(local)]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if npx:
        return [npx, "--yes", "tsc"]
    return None


def _run_tsc(source: str, module_id: str, report: ValidationReport) -> None:
    """Type-check du composant — BEST-EFFORT (warnings uniquement), jamais bloquant.

    Le fichier est écrit dans src/modules/_workshop_check/<id>/ : MÊME profondeur
    que la cible réelle (src/modules/generated/<id>/) pour que les imports
    relatifs (../../../components/ui, ../../registry…) résolvent, mais HORS de
    generated/ — sinon son apparition/suppression déclenche un full reload Vite
    (le watcher de import.meta.glob('./generated/**') se déclenche sur tout
    ajout/retrait dans l'arbre, même exclu du résultat). Puis supprimé.
    Le gate d'exécution reste l'AST de router.py ; ici on ne fait que signaler.
    """
    base = _tsc_command()
    if base is None:
        report.warn("tsc indisponible (projet/toolchain absent) — type-check ignoré.")
        return

    safe_id = re.sub(r"[^a-z0-9_]", "", module_id.lower()) or "mod"
    check_dir = _FRONTEND_DIR / "src" / "modules" / "_workshop_check" / safe_id
    check_file = check_dir / "Component.tsx"
    try:
        check_dir.mkdir(parents=True, exist_ok=True)
        check_file.write_text(source, encoding="utf-8")
        proc = subprocess.run(
            base + ["--noEmit", "-p", "tsconfig.app.json"],
            cwd=str(_FRONTEND_DIR),
            capture_output=True, text=True, timeout=30,
            env=_make_exec_env(), stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rel = f"_workshop_check/{safe_id}/Component.tsx"
        errs = [ln for ln in out.splitlines() if rel in ln or rel.replace("/", "\\") in ln]
        if errs:
            # Best-effort : on signale en WARNING (ne bloque pas l'activation).
            report.warn("Type-check du composant :\n" + "\n".join(errs[:15]))
    except subprocess.TimeoutExpired:
        report.warn("tsc : timeout (30s) — type-check incomplet.")
    except Exception:
        logger.exception("Erreur exécution tsc pour %s", module_id)
        report.warn("tsc : exécution impossible — type-check ignoré.")
    finally:
        try:
            shutil.rmtree(check_dir, ignore_errors=True)
        except Exception:
            logger.exception("Nettoyage tsc check dir %s", check_dir)
