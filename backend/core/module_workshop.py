"""Atelier de création ET modification de modules Épure.

Tous les modules vivent dans backend/modules/<id>/ (plus de module protégé après
la migration). L'atelier génère/édite dans un bac à sable confiné
backend/modules/_staging/<id>/, valide AVANT toute activation, puis — sur
approbation humaine — sauvegarde l'existant (backend/modules/_backups/) et
déplace en place.

DEUX FRONTIÈRES :
  - GÉNÉRATION : toutes les écritures sont raciné sur backend/modules/ via
    _modules_safe_path (réutilise le motif de codeagent._safe_path). Les voies
    claude_* tournent en cwd=staging avec --add-dir limité au staging ; quoi
    qu'il arrive, l'atelier ne lit/copie QUE depuis le staging confiné.
  - EXÉCUTION : confiner le dossier ne protège pas (un router activé est importé
    dans le process). → validation AST OBLIGATOIRE (core.module_validate) +
    revue humaine avant activation.

Moteurs de génération : "ollama" (LLMEngine.stream), "claude_sub" (CLI claude
authentifié par abonnement), "claude_gateway" (CLI claude + ANTHROPIC_BASE_URL
vers une passerelle locale). Modes claude_* : "headless" (subprocess streamé) ou
"terminal" (session pilotée par l'utilisateur, re-scan au retour).
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request
from difflib import unified_diff
from pathlib import Path
from typing import Generator, Optional

from pydantic import BaseModel, field_validator

from core.codeagent import SecurityError, _make_exec_env
from core.instance import instance_config
from core.module_validate import validate_component_tsx, validate_router_py
from core import module_registry

logger = logging.getLogger(__name__)

# ── Racines confinées ────────────────────────────────────────────────────────
MODULES_DIR = (Path(__file__).parent.parent / "modules").resolve()
STAGING_DIR = MODULES_DIR / "_staging"
BACKUPS_DIR = MODULES_DIR / "_backups"
_FRONTEND_MODULES = (Path(__file__).parent.parent.parent / "frontend" / "src" / "modules").resolve()
_FRONTEND_GENERATED = _FRONTEND_MODULES / "generated"

_FILES = ("manifest.json", "router.py", "Component.tsx")
_RESERVED_IDS = {"_staging", "_backups", "generated", "settings"}
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
_CLAUDE_TIMEOUT = 600  # secondes (génération headless)


def _modules_safe_path(relative: str) -> Path:
    """Résout un chemin et refuse toute sortie de backend/modules/.

    Même garde-fou que codeagent._safe_path, mais raciné sur modules/.
    """
    target = (MODULES_DIR / relative).resolve()
    if not str(target).startswith(str(MODULES_DIR)):
        logger.warning("SECURITY: écriture refusée hors modules/ — %s", target)
        raise SecurityError(f"Écriture refusée hors de modules/ : {target}")
    return target


# ── Manifeste (plan) ─────────────────────────────────────────────────────────

class ModuleManifest(BaseModel):
    id: str
    version: str = "1.0.0"
    nom: str
    icon: str = "Box"
    description: str = ""
    frontend: dict = {"component": "Component"}
    backend: dict = {"prefix": ""}
    core_module: bool = False
    origin: str = "workshop"
    status: str = "active"
    removable: bool = True

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError("id invalide (minuscules, lettres/chiffres/_, 2-31 car.)")
        if v in _RESERVED_IDS:
            raise ValueError(f"id réservé : {v}")
        return v


# ── État de staging (.workshop.json) ─────────────────────────────────────────

def _staging_dir(module_id: str) -> Path:
    return _modules_safe_path(f"_staging/{module_id}")


def _meta_path(module_id: str) -> Path:
    return _staging_dir(module_id) / ".workshop.json"


def _read_meta(module_id: str) -> Optional[dict]:
    p = _meta_path(module_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Meta workshop illisible : %s", p)
        return None


def _write_meta(module_id: str, meta: dict) -> None:
    p = _meta_path(module_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def module_exists(module_id: str) -> bool:
    return (MODULES_DIR / module_id / "manifest.json").is_file()


def is_core(module_id: str) -> bool:
    m = module_registry.get_module(module_id)
    return bool(m and m.get("core_module"))


# ── Découverte / few-shot ────────────────────────────────────────────────────

def _active_files(module_id: str) -> dict:
    """Contenu actuel des 3 fichiers d'un module en place (chaînes, '' si absent)."""
    out = {"manifest.json": "", "router.py": "", "Component.tsx": ""}
    base = MODULES_DIR / module_id
    for name in ("manifest.json", "router.py"):
        f = base / name
        if f.is_file():
            out[name] = f.read_text(encoding="utf-8", errors="replace")
    comp = _frontend_component_path(module_id, must_exist=True)
    if comp and comp.is_file():
        out["Component.tsx"] = comp.read_text(encoding="utf-8", errors="replace")
    return out


def _few_shot() -> dict:
    """Exemple complet (module hello) pour guider la génération."""
    return _active_files("hello")


# ── Frontend component path ──────────────────────────────────────────────────

def _frontend_component_path(module_id: str, must_exist: bool = False) -> Optional[Path]:
    """Emplacement du composant frontend d'un module.

    Cherche d'abord src/modules/<id>/Component.tsx (modules core migrés), puis
    src/modules/generated/<id>/Component.tsx. Pour un module neuf : generated/.
    """
    core_path = _FRONTEND_MODULES / module_id / "Component.tsx"
    gen_path = _FRONTEND_GENERATED / module_id / "Component.tsx"
    if core_path.is_file():
        return core_path
    if gen_path.is_file():
        return gen_path
    return None if must_exist else gen_path


# ── Préparation du staging ───────────────────────────────────────────────────

def prepare(module_id: str, kind: str, engine: str, mode: str) -> dict:
    """Crée/réinitialise le staging. kind = 'new' | 'edit'.

    Pour 'edit' : copie d'abord le module actif dans le staging (puis on édite).
    """
    if kind not in ("new", "edit"):
        raise ValueError("kind doit être 'new' ou 'edit'")
    if not _ID_RE.match(module_id) or module_id in _RESERVED_IDS:
        raise ValueError("id de module invalide ou réservé")
    if kind == "new" and module_exists(module_id):
        raise ValueError(f"Le module '{module_id}' existe déjà — utilisez 'edit'.")
    if kind == "edit" and not module_exists(module_id):
        raise ValueError(f"Le module '{module_id}' n'existe pas — utilisez 'new'.")

    sdir = _staging_dir(module_id)
    if sdir.exists():
        shutil.rmtree(sdir)
    sdir.mkdir(parents=True, exist_ok=True)

    if kind == "edit":
        # Copie l'état actif dans le staging pour servir de base à l'édition.
        active = _active_files(module_id)
        for name, content in active.items():
            if content:
                (sdir / name).write_text(content, encoding="utf-8")

    meta = {
        "id": module_id,
        "kind": kind,
        "engine": engine,
        "mode": mode,
        "is_core": is_core(module_id),
        "status": "draft",
        "report": None,
        "ts": int(time.time()),
    }
    _write_meta(module_id, meta)
    return meta


# ── Passerelle / disponibilité des moteurs ───────────────────────────────────

def _gateway_cfg() -> dict:
    atelier = (instance_config.get().get("atelier") or {})
    return {
        "url": atelier.get("gateway_url", "http://localhost:4000"),
        "model": atelier.get("gateway_model", "claude-sonnet-4-5"),
        "api_key": (atelier.get("gateway_api_key") or "").strip(),
    }


def _claude_bin() -> Optional[str]:
    """Localise le binaire `claude` : chemin configuré (atelier.claude_path) sinon PATH."""
    atelier = (instance_config.get().get("atelier") or {})
    cp = (atelier.get("claude_path") or "").strip()
    if cp and Path(cp).exists():
        return cp
    return shutil.which("claude") or shutil.which("claude.cmd")


def gateway_reachable(url: Optional[str] = None) -> bool:
    url = url or _gateway_cfg()["url"]
    for path in ("/health", "/v1/models", "/"):
        try:
            req = urllib.request.Request(url.rstrip("/") + path, method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            continue
    return False


def engines_status() -> dict:
    """Disponibilité des 3 moteurs pour l'UI (avec diagnostic actionnable)."""
    claude_bin = _claude_bin()
    claude_cli = bool(claude_bin)
    gw = _gateway_cfg()
    gw_ok = claude_cli and gateway_reachable(gw["url"])
    no_cli = (
        "CLI `claude` introuvable : installez-le (npm i -g @anthropic-ai/claude-code) "
        "puis authentifiez-vous, ou renseignez son chemin dans Réglages › Atelier."
    )
    return {
        "ollama": {"available": True, "reason": ""},
        "claude_sub": {
            "available": claude_cli,
            "reason": "" if claude_cli else no_cli,
            "bin": claude_bin or "",
        },
        "claude_gateway": {
            "available": gw_ok,
            "reason": (
                "" if gw_ok
                else (no_cli if not claude_cli
                      else f"Passerelle injoignable : {gw['url']} (démarrez la passerelle ou corrigez l'URL dans Réglages › Atelier)")
            ),
            "url": gw["url"],
            "model": gw["model"],
        },
    }


# ── Génération : moteur ollama (LLMEngine.stream + balises) ───────────────────

_FILE_BLOCK_RE = re.compile(
    r"===FILE:(?P<name>[\w.]+)===\r?\n(?P<body>.*?)(?=\r?\n===FILE:|\r?\n===END===|\Z)",
    re.DOTALL,
)


def _ollama_prompt(module_id: str, spec: str, kind: str, current: dict) -> list[dict]:
    ex = _few_shot()
    fewshot = (
        "Exemple de module valide (hello) :\n"
        f"===FILE:manifest.json===\n{ex.get('manifest.json','')}\n"
        f"===FILE:router.py===\n{ex.get('router.py','')}\n"
        f"===FILE:Component.tsx===\n{ex.get('Component.tsx','')}\n===END===\n"
    )
    rules = (
        "Tu génères un module Épure : EXACTEMENT 3 fichiers, dans CE format, "
        "sans aucun texte autour :\n"
        "===FILE:manifest.json===\n<json>\n===FILE:router.py===\n<python>\n"
        "===FILE:Component.tsx===\n<tsx>\n===END===\n\n"
        "Contraintes STRICTES (sinon le module sera rejeté) :\n"
        f"- manifest.json : id=\"{module_id}\", backend.prefix=\"\", "
        "frontend.component=\"Component\", origin=\"workshop\".\n"
        "- router.py : `from fastapi import APIRouter` puis `router = APIRouter()`. "
        "INTERDIT : import subprocess/socket/importlib, os.system, eval/exec, "
        "accès aux clés API. Préfixe les routes par le nom du module (ex. /"
        f"{module_id}/...).\n"
        "- Component.tsx : composant React par défaut. Imports : "
        "`../../../components/ui` pour l'UI, `../../registry` pour SharedModuleProps. "
        "INTERDIT : dangerouslySetInnerHTML, eval. Appelle le backend via "
        "fetch('http://localhost:8000/...').\n"
    )
    if kind == "edit":
        rules += (
            "\nC'est une MODIFICATION. Voici les fichiers actuels — modifie-les "
            "selon la demande en gardant ce qui marche :\n"
            f"===FILE:manifest.json===\n{current.get('manifest.json','')}\n"
            f"===FILE:router.py===\n{current.get('router.py','')}\n"
            f"===FILE:Component.tsx===\n{current.get('Component.tsx','')}\n===END===\n"
        )
    return [
        {"role": "system", "content": rules + "\n" + fewshot},
        {"role": "user", "content": f"Demande pour le module « {module_id} » :\n{spec}"},
    ]


def generate_ollama(module_id: str, spec: str, kind: str, model: Optional[str] = None) -> Generator:
    """Génère les 3 fichiers via LLMEngine.stream, écrit dans le staging confiné."""
    from core.runtime import llm  # import tardif (évite de charger les moteurs en test)

    current = _active_files(module_id) if kind == "edit" else {}
    messages = _ollama_prompt(module_id, spec, kind, current)
    if model is None:
        model = (instance_config.get().get("providers") or {}).get("actif") or None

    yield {"type": "engine", "engine": "ollama", "model": model or "local"}
    full = ""
    try:
        for item in llm.stream(messages, model=model, max_tokens=6000):
            if isinstance(item, str):
                full += item
                yield {"type": "token", "content": item}
    except Exception as exc:
        logger.exception("Génération ollama échouée pour %s", module_id)
        yield {"type": "error", "content": f"Génération échouée : {exc}"}
        return

    written = _write_blocks_from_text(module_id, full)
    if not written:
        yield {"type": "error", "content": "Aucun fichier exploitable généré (format ===FILE:===)."}
        return
    for name in written:
        yield {"type": "file_written", "path": name}
    yield {"type": "generation_done", "files": written}


def _write_blocks_from_text(module_id: str, text: str) -> list[str]:
    """Parse les blocs ===FILE:name=== et écrit chaque fichier autorisé en staging."""
    written: list[str] = []
    for m in _FILE_BLOCK_RE.finditer(text):
        name = m.group("name").strip()
        if name not in _FILES:
            continue
        body = m.group("body").strip("\n")
        # Écriture confinée (lève SecurityError si hors modules/).
        target = _modules_safe_path(f"_staging/{module_id}/{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + "\n", encoding="utf-8")
        written.append(name)
    return written


# ── Génération : moteurs claude_* (CLI) ──────────────────────────────────────

def _claude_env(engine: str) -> dict:
    """Env minimal (clés API retirées) + ce qu'il faut pour authentifier claude.

    Important : _make_exec_env retire toute variable contenant TOKEN/KEY/SECRET —
    ce qui inclut CLAUDE_CODE_OAUTH_TOKEN (jeton d'abonnement headless). On le
    re-injecte explicitement, ainsi que le répertoire de config, sinon
    l'authentification par abonnement échoue en subprocess.
    """
    env = _make_exec_env()
    for k in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
              "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if engine == "claude_gateway":
        # ANTHROPIC_BASE_URL désactive l'OAuth abonnement → fournir un jeton/clé.
        gw = _gateway_cfg()
        env["ANTHROPIC_BASE_URL"] = gw["url"]
        env["ANTHROPIC_MODEL"] = gw["model"]
        env["ANTHROPIC_API_KEY"] = gw["api_key"] or os.environ.get("GATEWAY_API_KEY", "sk-gateway-local")
    # claude_sub : surtout NE PAS définir ANTHROPIC_API_KEY (il primerait sur
    # l'abonnement) — _make_exec_env l'a déjà retiré, on ne le réintroduit pas.
    return env


def _claude_cmd(prompt: str, staging_dir: Path) -> list[str]:
    """Commande claude headless confinée au staging (écriture seule là-dedans)."""
    claude = _claude_bin() or "claude"
    return [
        claude, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--add-dir", str(staging_dir),
        "--allowedTools", "Read,Edit,Write",
        "--permission-mode", "acceptEdits",
    ]


def _claude_prompt(module_id: str, spec: str, kind: str) -> str:
    verb = "Modifie" if kind == "edit" else "Crée"
    return (
        f"{verb} le module Épure « {module_id} » dans le dossier courant (qui est "
        f"déjà le bac à sable du module). Produis/édite EXACTEMENT 3 fichiers : "
        "manifest.json, router.py, Component.tsx.\n"
        "Contraintes : router.py = `from fastapi import APIRouter` + "
        "`router = APIRouter()`, AUCUN subprocess/socket/importlib/os.system/eval/"
        "exec, aucun accès aux clés API ; manifest.json avec "
        f"id=\"{module_id}\", backend.prefix=\"\", frontend.component=\"Component\", "
        "origin=\"workshop\" ; Component.tsx = composant React par défaut, sans "
        "dangerouslySetInnerHTML ni eval. N'écris QUE dans le dossier courant.\n\n"
        f"Demande : {spec}"
    )


def generate_claude_headless(module_id: str, spec: str, kind: str, engine: str) -> Generator:
    """Lance `claude -p` en subprocess, cwd=staging, et streame stdout (JSON)."""
    sdir = _staging_dir(module_id)
    sdir.mkdir(parents=True, exist_ok=True)
    cmd = _claude_cmd(_claude_prompt(module_id, spec, kind), sdir)
    env = _claude_env(engine)

    yield {"type": "engine", "engine": engine, "mode": "headless", "cwd": str(sdir)}
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(sdir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except FileNotFoundError:
        yield {"type": "error", "content": "CLI `claude` introuvable dans le PATH."}
        return
    except Exception as exc:
        logger.exception("Lancement claude échoué")
        yield {"type": "error", "content": f"Lancement claude échoué : {exc}"}
        return

    start = time.time()
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if line:
                yield {"type": "token", "content": line + "\n"}
            if time.time() - start > _CLAUDE_TIMEOUT:
                proc.kill()
                yield {"type": "error", "content": f"Timeout claude ({_CLAUDE_TIMEOUT}s)."}
                return
        proc.wait(timeout=10)
    except Exception as exc:
        logger.exception("Streaming claude échoué")
        yield {"type": "error", "content": str(exc)}
        return

    # On ne lit QUE le staging confiné, quoi que claude ait pu tenter ailleurs.
    present = [n for n in _FILES if (sdir / n).is_file()]
    if not present:
        yield {"type": "error", "content": "claude n'a produit aucun des 3 fichiers attendus dans le staging."}
        return
    yield {"type": "generation_done", "files": present}


def terminal_launch_spec(module_id: str, spec: str, kind: str, engine: str) -> dict:
    """Décrit la commande/cwd/env d'une session terminale confinée au staging.

    L'app lance un terminal réel pré-positionné ; à la fermeture (signal du
    frontend), on relance read_staging + validate_staging.
    """
    sdir = _staging_dir(module_id)
    sdir.mkdir(parents=True, exist_ok=True)
    cmd = _claude_cmd(_claude_prompt(module_id, spec, kind), sdir)
    # En mode terminal interactif on n'impose pas -p/stream-json : on ouvre claude
    # interactif dans le dossier confiné (l'utilisateur pilote).
    interactive = [cmd[0], "--add-dir", str(sdir), "--allowedTools", "Read,Write,Edit"]
    return {"cmd": interactive, "cwd": str(sdir), "env": _claude_env(engine), "prompt": _claude_prompt(module_id, spec, kind)}


def open_terminal(module_id: str, spec: str, kind: str, engine: str) -> dict:
    """Ouvre une vraie fenêtre terminal positionnée sur le staging (best-effort)."""
    info = terminal_launch_spec(module_id, spec, kind, engine)
    sdir = info["cwd"]
    try:
        if os.name == "nt":
            # Nouvelle fenêtre cmd dans le dossier confiné.
            subprocess.Popen(f'start "Atelier {module_id}" cmd /K cd /d "{sdir}"', shell=True)
        elif shutil.which("tmux"):
            subprocess.Popen(["tmux", "new-window", "-c", sdir])
        else:
            subprocess.Popen(["x-terminal-emulator"], cwd=sdir)
        return {"opened": True, "cwd": sdir, "cmd": info["cmd"]}
    except Exception as exc:
        logger.exception("Ouverture terminal échouée")
        return {"opened": False, "error": str(exc), "cwd": sdir, "cmd": info["cmd"]}


# ── Lecture staging + diff ───────────────────────────────────────────────────

def read_staging(module_id: str) -> dict:
    """Les 3 fichiers stagés + diff vs actif (si édition) + meta."""
    sdir = _staging_dir(module_id)
    if not sdir.is_dir():
        raise FileNotFoundError(f"Pas de staging pour '{module_id}'")
    staged = {}
    for name in _FILES:
        f = sdir / name
        staged[name] = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""

    meta = _read_meta(module_id) or {}
    diffs = {}
    if meta.get("kind") == "edit":
        active = _active_files(module_id)
        for name in _FILES:
            d = "\n".join(unified_diff(
                active.get(name, "").splitlines(),
                staged.get(name, "").splitlines(),
                fromfile=f"actif/{name}", tofile=f"staging/{name}", lineterm="",
            ))
            diffs[name] = d
    return {"id": module_id, "files": staged, "diff": diffs, "meta": meta,
            "is_core": is_core(module_id)}


# ── Validation du staging ────────────────────────────────────────────────────

def validate_staging(module_id: str, run_tsc: bool = True) -> dict:
    """Valide router.py + Component.tsx. Met à jour le status (pending_review|draft)."""
    sdir = _staging_dir(module_id)
    meta = _read_meta(module_id) or {"id": module_id}
    errors: list[str] = []
    warnings: list[str] = []

    router_src = (sdir / "router.py").read_text(encoding="utf-8", errors="replace") if (sdir / "router.py").is_file() else ""
    comp_src = (sdir / "Component.tsx").read_text(encoding="utf-8", errors="replace") if (sdir / "Component.tsx").is_file() else ""
    manifest_raw = (sdir / "manifest.json").read_text(encoding="utf-8", errors="replace") if (sdir / "manifest.json").is_file() else ""

    # manifeste
    try:
        data = json.loads(manifest_raw) if manifest_raw else {}
        data.setdefault("id", module_id)
        ModuleManifest(**data)
    except Exception as exc:
        errors.append(f"manifest.json invalide : {exc}")

    if not router_src:
        errors.append("router.py manquant.")
    else:
        rr = validate_router_py(router_src)
        errors += rr.errors
        warnings += rr.warnings

    if not comp_src:
        errors.append("Component.tsx manquant.")
    else:
        cr = validate_component_tsx(comp_src, module_id, run_tsc=run_tsc)
        errors += cr.errors
        warnings += cr.warnings

    report = {"ok": not errors, "errors": errors, "warnings": warnings}
    meta["report"] = report
    meta["status"] = "pending_review" if not errors else "draft"
    _write_meta(module_id, meta)
    return {"status": meta["status"], "report": report}


# ── Approbation / rejet ──────────────────────────────────────────────────────

def _backup_existing(module_id: str) -> Optional[str]:
    """Sauvegarde horodatée du module existant (backend + composant). Retourne le chemin."""
    if not module_exists(module_id):
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS_DIR / module_id / ts
    dest.mkdir(parents=True, exist_ok=True)
    base = MODULES_DIR / module_id
    for name in ("manifest.json", "router.py"):
        f = base / name
        if f.is_file():
            shutil.copy2(f, dest / name)
    comp = _frontend_component_path(module_id, must_exist=True)
    if comp and comp.is_file():
        shutil.copy2(comp, dest / "Component.tsx")
    logger.info("Backup module %s → %s", module_id, dest)
    return str(dest)


def _drop_module_routes(app, module_id: str) -> None:
    modname = f"modules.{module_id}.router"
    app.router.routes = [
        r for r in app.router.routes
        if getattr(getattr(r, "endpoint", None), "__module__", None) != modname
    ]


def _remount(app, module_id: str) -> None:
    """(Re)monte le router du module dans l'app en cours (sans redémarrage)."""
    import importlib
    modname = f"modules.{module_id}.router"
    _drop_module_routes(app, module_id)
    mod = importlib.import_module(modname)
    mod = importlib.reload(mod)
    router = getattr(mod, "router", None)
    if router is None:
        raise RuntimeError(f"{modname} ne définit pas 'router'")
    manifest = module_registry.get_module(module_id) or {}
    prefix = (manifest.get("backend") or {}).get("prefix", "")
    app.include_router(router, prefix=prefix)
    app.openapi_schema = None  # invalide le schéma OpenAPI mis en cache


def approve(module_id: str, app=None) -> dict:
    """Active le module après revue : backup → déplacement → reload → activation.

    Refuse si la validation n'est pas passée (status != pending_review).
    """
    meta = _read_meta(module_id)
    if not meta:
        raise ValueError("Aucun staging à approuver.")
    # Revalidation (gate d'exécution) — on ne fait jamais confiance au status seul.
    res = validate_staging(module_id, run_tsc=False)
    if not res["report"]["ok"]:
        return {"ok": False, "report": res["report"], "status": "draft",
                "detail": "Validation échouée — activation refusée."}

    sdir = _staging_dir(module_id)
    backup = _backup_existing(module_id)

    # Backend : manifest.json + router.py → modules/<id>/
    dest = MODULES_DIR / module_id
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "router.py"):
        src = sdir / name
        if src.is_file():
            shutil.copy2(src, dest / name)

    # Frontend : Component.tsx → emplacement existant, sinon generated/<id>/
    comp_src = sdir / "Component.tsx"
    if comp_src.is_file():
        comp_dest = _frontend_component_path(module_id, must_exist=True) or (_FRONTEND_GENERATED / module_id / "Component.tsx")
        comp_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(comp_src, comp_dest)

    # Activation : ajout à modules_activés + (re)montage du router.
    cfg = instance_config.get()
    enabled = list(cfg.get("modules_activés", []))
    if module_id not in enabled:
        enabled.append(module_id)
        instance_config.update({"modules_activés": enabled})

    remounted = False
    remount_error = None
    if app is not None:
        try:
            _remount(app, module_id)
            remounted = True
        except Exception as exc:
            logger.exception("Remontage du module %s échoué", module_id)
            remount_error = str(exc)

    # Nettoyage du staging.
    shutil.rmtree(sdir, ignore_errors=True)

    return {
        "ok": True, "module_id": module_id, "backup": backup,
        "remounted": remounted, "remount_error": remount_error,
        "restart_required": not remounted,
    }


def reject(module_id: str) -> dict:
    """Supprime le staging (rien n'est activé)."""
    sdir = _staging_dir(module_id)
    if sdir.is_dir():
        shutil.rmtree(sdir, ignore_errors=True)
    return {"ok": True, "module_id": module_id}


def list_staging() -> list[dict]:
    """Modules actuellement en atelier (staging)."""
    out = []
    if STAGING_DIR.is_dir():
        for sub in sorted(STAGING_DIR.iterdir()):
            if sub.is_dir():
                meta = _read_meta(sub.name)
                if meta:
                    out.append(meta)
    return out
