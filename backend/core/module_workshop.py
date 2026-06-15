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
import sys
import sysconfig
import time
import urllib.request
from difflib import unified_diff
from pathlib import Path
from threading import Thread
from typing import Generator, Optional

from pydantic import BaseModel, field_validator, model_validator

from core.codeagent import SecurityError, _make_exec_env
from core.instance import instance_config
from core.module_validate import (
    validate_component_tsx, validate_router_py, ui_component_exports,
)
from core import module_registry

logger = logging.getLogger(__name__)

# Évite l'ouverture de fenêtres console visibles (qui volent le focus) quand on
# lance claude/.cmd en subprocess sous Windows. 0 sur les autres OS.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ── Racines confinées ────────────────────────────────────────────────────────
MODULES_DIR = (Path(__file__).parent.parent / "modules").resolve()
STAGING_DIR = MODULES_DIR / "_staging"
BACKUPS_DIR = MODULES_DIR / "_backups"
_FRONTEND_MODULES = (Path(__file__).parent.parent.parent / "frontend" / "src" / "modules").resolve()
_FRONTEND_GENERATED = _FRONTEND_MODULES / "generated"

_FILES = ("manifest.json", "router.py", "Component.tsx")
# Ids impossibles : collisions avec des dossiers internes. 'settings' n'est PAS
# réservé — c'est un vrai module éditable (avec avertissement côté UI).
_RESERVED_IDS = {"_staging", "_backups", "generated"}
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
    nom: str = ""  # défaut : id capitalisé (cf. _default_nom)
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

    @model_validator(mode="after")
    def _defaults(self):
        if not (self.nom or "").strip():
            self.nom = self.id.capitalize()
        if not (self.frontend or {}).get("component"):
            self.frontend = {**(self.frontend or {}), "component": "Component"}
        if (self.backend or {}).get("prefix") is None:
            self.backend = {**(self.backend or {}), "prefix": ""}
        return self


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


def _staging_files(module_id: str) -> dict:
    """Contenu des 3 fichiers en staging (dict vide si le staging n'existe pas)."""
    sdir = STAGING_DIR / module_id
    if not sdir.is_dir():
        return {}
    out = {}
    for name in _FILES:
        f = sdir / name
        if f.is_file():
            out[name] = f.read_text(encoding="utf-8", errors="replace")
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
    gw = ((instance_config.get().get("atelier") or {}).get("gateway") or {})
    return {
        "base_url": gw.get("base_url", "http://localhost:4000"),
        "model": (gw.get("model") or "").strip(),
        "api_key": (gw.get("api_key") or "").strip(),
        "start_command": (gw.get("start_command") or "").strip(),
    }


def start_gateway() -> dict:
    """Lance la passerelle via atelier.gateway.start_command (process détaché).

    Commande fournie par l'utilisateur (ex. `litellm --config cfg.yaml`) : lancée
    via le shell, détachée du backend, sans fenêtre console. Ne bloque pas — le
    front re-teste la joignabilité après quelques secondes.
    """
    cfg = _gateway_cfg()
    cmd = cfg["start_command"]
    if not cmd:
        return {"ok": False, "raison": "Aucune commande de démarrage configurée (Réglages › Atelier)."}
    if gateway_reachable(cfg["base_url"]):
        return {"ok": True, "raison": "Passerelle déjà joignable."}
    try:
        flags = _NO_WINDOW
        if os.name == "nt":
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            cmd, shell=True, cwd=str(Path.home()),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=flags,
        )
    except Exception as exc:
        logger.exception("Lancement passerelle échoué")
        return {"ok": False, "raison": f"Échec du lancement : {exc}"}
    return {"ok": True, "raison": "Commande lancée — patientez quelques secondes puis re-testez."}


def _claude_bin() -> Optional[str]:
    """Localise le binaire `claude` : atelier.claude_path (nom PATH ou chemin complet)."""
    cp = ((instance_config.get().get("atelier") or {}).get("claude_path") or "claude").strip()
    # Chemin (contient un séparateur) → utilisé tel quel s'il existe.
    if os.sep in cp or (os.altsep and os.altsep in cp):
        return cp if Path(cp).exists() else None
    # Nom simple → résolution via le PATH.
    return shutil.which(cp) or shutil.which(cp + ".cmd")


def _script_dirs() -> list[Path]:
    """Dossiers où pip/uv/pipx déposent les console-scripts, hors PATH du backend.

    Couvre le cas fréquent : aider installé mais son dossier d'install n'est pas
    sur le PATH du process backend (lancé avant l'install, ou uv tool / pip --user).
    """
    dirs: list[Path] = []

    def _add(p) -> None:
        if p:
            dirs.append(Path(p))

    # Scripts de l'env Python courant + scheme utilisateur (fiable, versionné).
    try:
        _add(sysconfig.get_path("scripts"))
    except Exception:
        pass
    try:
        _add(sysconfig.get_path("scripts", "nt_user" if os.name == "nt" else "posix_user"))
    except Exception:
        pass
    # À côté de l'exécutable Python (venv/conda).
    exe_dir = Path(sys.executable).parent
    _add(exe_dir)
    _add(exe_dir / "Scripts")
    # Emplacements d'install courants (uv tool, pipx, pip --user POSIX).
    _add(Path.home() / ".local" / "bin")
    # Outils uv : ~/.local/share/uv/tools/<tool>/{Scripts,bin}
    uv_tools = Path.home() / ".local" / "share" / "uv" / "tools"
    try:
        if uv_tools.is_dir():
            for tool in uv_tools.iterdir():
                _add(tool / "Scripts")
                _add(tool / "bin")
    except Exception:
        pass
    return dirs


def _aider_bin() -> Optional[str]:
    """Localise le binaire `aider`.

    Ordre : atelier.aider_path (chemin complet) → PATH → à côté de l'env Python
    du backend → emplacements d'install courants (~/.local/bin, Scripts
    utilisateur, outils uv). Résout le cas « installé mais absent du PATH ».
    """
    ap = ((instance_config.get().get("atelier") or {}).get("aider_path") or "aider").strip()
    # Chemin complet fourni → tel quel s'il existe.
    if os.sep in ap or (os.altsep and os.altsep in ap):
        return ap if Path(ap).exists() else None
    # Nom simple → PATH d'abord.
    found = shutil.which(ap) or shutil.which(ap + ".cmd")
    if found:
        return found
    # Fallback : dossiers d'install hors PATH.
    exe_names = [ap, ap + ".exe", ap + ".cmd"]
    for d in _script_dirs():
        for name in exe_names:
            p = d / name
            try:
                if p.is_file():
                    return str(p)
            except Exception:
                continue
    return None


def _local_agent_env(extra: Optional[dict] = None) -> dict:
    """Env minimal pour moteurs locaux (aider, opencode) : PATH + variables home seulement.
    Aucune clé API cloud — local pur par défaut. extra = variables supplémentaires à injecter."""
    env = _make_exec_env()
    # CRUCIAL : aider/opencode embarquent leur PROPRE Python (uv tool / venv).
    # _make_exec_env injecte le PYTHONPATH du backend (stdlib de pythoncore-3.14) ;
    # imposé à un autre interpréteur, il lui fait charger une stdlib étrangère →
    # « AssertionError: SRE module mismatch » au démarrage. On retire donc toute
    # variable qui localise un runtime Python.
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONEXECUTABLE",
              "__PYVENV_LAUNCHER__", "VIRTUAL_ENV"):
        env.pop(k, None)
    for k in ("PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
              "OLLAMA_HOST", "OLLAMA_API_BASE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if extra:
        env.update(extra)
    return env


def gateway_reachable(url: Optional[str] = None) -> bool:
    url = url or _gateway_cfg()["base_url"]
    for path in ("/health", "/v1/models", "/"):
        try:
            req = urllib.request.Request(url.rstrip("/") + path, method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            continue
    return False


def _ollama_status() -> tuple[bool, str]:
    """Ping du serveur Ollama + au moins un modèle présent."""
    try:
        from core.models import get_ollama_installed
        models = get_ollama_installed()
    except Exception:
        return False, "Vérification Ollama impossible."
    if models is None:
        return False, "Serveur Ollama injoignable (localhost:11434)."
    if not models:
        return False, "Ollama actif mais aucun modèle installé (`ollama pull …`)."
    return True, ""


def _claude_version_ok(claude_bin: Optional[str]) -> bool:
    """`<claude_path> --version` exécutable (CLI présent et fonctionnel)."""
    if not claude_bin:
        return False
    try:
        r = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True,
            timeout=20, env=_claude_env("claude_sub"),
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def _bin_version_ok(bin_path: Optional[str]) -> bool:
    """Vérifie que bin_path --version s'exécute sans erreur."""
    if not bin_path:
        return False
    try:
        r = subprocess.run(
            [bin_path, "--version"], capture_output=True, text=True,
            timeout=15, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def _claude_auth_detected() -> bool:
    """Auth d'abonnement présente : jeton setup-token (env) ou credentials de login."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return True
    candidates: list[Path] = []
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if cfg_dir:
        candidates.append(Path(cfg_dir) / ".credentials.json")
    home = Path.home()
    candidates += [
        home / ".claude" / ".credentials.json",
        home / ".config" / "claude" / ".credentials.json",
    ]
    for c in candidates:
        try:
            if c.is_file() and c.stat().st_size > 2:
                return True
        except Exception:
            continue
    return False


def engines_status() -> dict:
    """Disponibilité des 3 moteurs : {disponible, raison} (+ infos utiles)."""
    claude_bin = _claude_bin()
    ver_ok = _claude_version_ok(claude_bin)
    no_cli = (
        "CLI `claude` introuvable/inexécutable — installez-le "
        "(npm i -g @anthropic-ai/claude-code) ou corrigez claude_path (Réglages › Atelier)."
    )

    o_ok, o_raison = _ollama_status()

    if not ver_ok:
        sub_ok, sub_raison = False, no_cli
    elif _claude_auth_detected():
        sub_ok, sub_raison = True, ""
    else:
        sub_ok, sub_raison = False, (
            "Pas d'auth d'abonnement détectée — lancez `claude setup-token` "
            "(ou `claude` puis /login), puis Re-tester."
        )

    gw = _gateway_cfg()
    if not ver_ok:
        gw_ok, gw_raison = False, no_cli
    elif not gateway_reachable(gw["base_url"]):
        gw_ok, gw_raison = False, (
            f"Passerelle injoignable : {gw['base_url']} (démarrez-la ou corrigez l'URL)."
        )
    else:
        gw_ok, gw_raison = True, ""

    # ── aider ────────────────────────────────────────────────────────────────
    aider_bin = _aider_bin()
    aider_ver_ok = _bin_version_ok(aider_bin)
    if not aider_ver_ok:
        aid_ok, aid_raison = False, (
            "Binaire `aider` introuvable — installez-le : "
            "pip install aider-chat (ou uv tool install --python 3.12 aider-chat), "
            "puis Re-tester."
        )
    elif not o_ok:
        aid_ok, aid_raison = False, f"Ollama requis pour aider local : {o_raison}"
    else:
        aid_ok, aid_raison = True, ""

    return {
        "ollama": {"disponible": o_ok, "raison": o_raison},
        "claude_sub": {"disponible": sub_ok, "raison": sub_raison, "bin": claude_bin or ""},
        "claude_gateway": {
            "disponible": gw_ok, "raison": gw_raison,
            "base_url": gw["base_url"], "model": gw["model"],
        },
        "aider": {"disponible": aid_ok, "raison": aid_raison, "bin": aider_bin or ""},
    }


# ── Génération : moteur ollama (LLMEngine.stream + balises) ───────────────────

_FILE_BLOCK_RE = re.compile(
    r"===FILE:(?P<name>[\w.]+)===\r?\n(?P<body>.*?)(?=\r?\n===FILE:|\r?\n===END===|\Z)",
    re.DOTALL,
)


def _ollama_prompt(module_id: str, spec: str, kind: str, current: dict,
                   feedback: Optional[str] = None) -> list[dict]:
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
        f"Depuis components/ui, tu ne peux importer QUE ces composants (aucun autre "
        f"n'existe — pas de Label, CardHeader, etc.) : {', '.join(ui_component_exports()) or 'Button, Card, Badge, Input, Textarea, Toggle, Select, Tooltip, Tabs, ProgressBar, Modal, ThemeToggle'}. "
        "Pour tout le reste (label, titre…), utilise des balises HTML standard. "
        "INTERDIT : dangerouslySetInnerHTML, eval. Appelle le backend via "
        "fetch('http://localhost:8000/...').\n"
        "- PERSISTANCE : pour tout état qui doit survivre à un rechargement de page "
        "(texte saisi, contenu généré, sélections, onglet courant), utilise "
        "`usePersistentState` au lieu de `useState` — même signature, premier "
        "argument = clé unique préfixée par l'id du module. "
        f"Import : `import {{ usePersistentState }} from '../../../usePersistentState'`. "
        f"Ex : `const [texte, setTexte] = usePersistentState('{module_id}.texte', '')`. "
        "Garde `useState` pour l'éphémère (chargement, flags, données re-fetchées au montage).\n"
    )
    # Fichiers existants à montrer : édition classique OU correction (la tentative
    # précédente est en staging). Indispensable pour une correction CIBLÉE — sinon
    # l'IA régénère à l'aveugle et refait la même erreur.
    have_current = any((current or {}).get(n, "").strip() for n in _FILES)
    if have_current:
        intro = (
            "\nVoici les fichiers de la TENTATIVE PRÉCÉDENTE (celle qui vient d'être "
            "rejetée). Pars de CE code et corrige uniquement ce qu'il faut :"
            if feedback else
            "\nC'est une MODIFICATION. Voici les fichiers actuels — modifie-les "
            "selon la demande en gardant ce qui marche :"
        )
        rules += (
            f"{intro}\n"
            f"===FILE:manifest.json===\n{current.get('manifest.json','')}\n"
            f"===FILE:router.py===\n{current.get('router.py','')}\n"
            f"===FILE:Component.tsx===\n{current.get('Component.tsx','')}\n===END===\n"
        )
    user = f"Demande pour le module « {module_id} » :\n{spec}"
    if feedback:
        # On renvoie l'erreur EXACTE du validateur en tête du message (pas noyée),
        # avec une consigne impérative de corriger précisément ces points.
        user = (
            "⚠️ CORRECTION. La version précédente (ci-dessus) a été REJETÉE par le "
            "validateur pour les raisons PRÉCISES suivantes. Corrige EXACTEMENT ces "
            "points, NE réintroduis PAS la même erreur, garde le reste identique, et "
            "renvoie les 3 fichiers complets dans le format imposé.\n"
            f"=== ERREURS À CORRIGER ===\n{feedback}\n=== FIN ERREURS ===\n\n"
            + user
        )
    return [
        {"role": "system", "content": rules + "\n" + fewshot},
        {"role": "user", "content": user},
    ]


def generate_ollama(module_id: str, spec: str, kind: str, model: Optional[str] = None,
                    feedback: Optional[str] = None) -> Generator:
    """Génère les 3 fichiers via LLMEngine.stream, écrit dans le staging confiné.
    `model` force le modèle Ollama ; `feedback` injecte les erreurs à corriger."""
    from core.runtime import llm  # import tardif (évite de charger les moteurs en test)

    # En correction d'erreur, on repart des fichiers en staging (ceux qui ont
    # échoué), sinon de l'actif pour une édition classique.
    if feedback:
        current = _staging_files(module_id) or (_active_files(module_id) if kind == "edit" else {})
    else:
        current = _active_files(module_id) if kind == "edit" else {}
    messages = _ollama_prompt(module_id, spec, kind, current, feedback)
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
        # ANTHROPIC_BASE_URL désactive l'OAuth abonnement → fournir clé/jeton, et
        # ne PAS laisser le jeton d'abonnement traîner.
        gw = _gateway_cfg()
        env["ANTHROPIC_BASE_URL"] = gw["base_url"]
        if gw["model"]:
            env["ANTHROPIC_MODEL"] = gw["model"]
        if gw["api_key"]:
            env["ANTHROPIC_AUTH_TOKEN"] = gw["api_key"]
            env["ANTHROPIC_API_KEY"] = gw["api_key"]
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    else:
        # claude_sub : surtout PAS d'ANTHROPIC_API_KEY (sinon bascule en
        # facturation API au lieu de l'abonnement). _make_exec_env l'a déjà
        # retirée ; on s'assure qu'aucune variable Anthropic ne subsiste.
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
            env.pop(k, None)
    return env


def _claude_cmd(staging_dir: Path, engine: str = "claude_sub") -> list[str]:
    """Commande claude headless confinée au staging (écriture seule là-dedans).

    Le PROMPT n'est PAS passé en argument : sous Windows, claude est un .CMD et
    un argument multi-lignes casse la ligne de commande cmd.exe (les flags après
    le prompt — dont --permission-mode acceptEdits — sont alors perdus, et claude
    ne peut plus écrire en headless). Le prompt est donc transmis via STDIN.
    """
    claude = _claude_bin() or "claude"
    cmd = [
        claude, "-p",
        "--output-format", "stream-json", "--verbose",
        "--add-dir", str(staging_dir),
        "--allowedTools", "Read,Edit,Write",
        "--permission-mode", "acceptEdits",
    ]
    if engine == "claude_gateway":
        model = _gateway_cfg()["model"]
        if model:
            cmd += ["--model", model]
    return cmd


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
    cmd = _claude_cmd(sdir, engine)
    prompt = _claude_prompt(module_id, spec, kind)
    env = _claude_env(engine)

    yield {"type": "engine", "engine": engine, "mode": "headless", "cwd": str(sdir)}
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(sdir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, text=True, creationflags=_NO_WINDOW,
        )
        # Prompt via STDIN (cf. _claude_cmd) puis on ferme l'entrée.
        try:
            proc.stdin.write(prompt)  # type: ignore[union-attr]
            proc.stdin.close()        # type: ignore[union-attr]
        except Exception:
            logger.exception("Écriture du prompt sur stdin claude échouée")
    except FileNotFoundError:
        yield {"type": "error", "content": "CLI `claude` introuvable dans le PATH."}
        return
    except Exception as exc:
        logger.exception("Lancement claude échoué")
        yield {"type": "error", "content": f"Lancement claude échoué : {exc}"}
        return

    # Watchdog : tue le process après _CLAUDE_TIMEOUT MÊME s'il ne sort rien
    # (le timeout dans la boucle ne se déclenche qu'à l'arrivée d'une ligne →
    # un claude muet pouvait figer indéfiniment).
    timed_out = {"flag": False}

    def _watchdog():
        try:
            proc.wait(timeout=_CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out["flag"] = True
            try:
                proc.kill()
            except Exception:
                pass

    Thread(target=_watchdog, daemon=True).start()

    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if line:
                yield {"type": "token", "content": line + "\n"}
        proc.wait(timeout=10)
    except Exception as exc:
        logger.exception("Streaming claude échoué")
        yield {"type": "error", "content": str(exc)}
        return

    if timed_out["flag"]:
        yield {"type": "error", "content": f"Timeout claude ({_CLAUDE_TIMEOUT}s) — process tué (aucune sortie)."}
        return

    # On ne lit QUE le staging confiné, quoi que claude ait pu tenter ailleurs.
    present = [n for n in _FILES if (sdir / n).is_file()]
    if not present:
        yield {"type": "error", "content": "claude n'a produit aucun des 3 fichiers attendus dans le staging."}
        return
    yield {"type": "generation_done", "files": present}


# ── Providers cloud supportés par aider (mapping Épure → aider) ──────────
# provider → (base_url, env_key_name, model_prefix)
# Tous routés via le chemin OpenAI-compatible de litellm (prefix "openai") :
# aider lit alors OPENAI_API_BASE + OPENAI_API_KEY (ce que l'on injecte). Les
# prefix natifs litellm ("mistral/", "groq/") liraient MISTRAL_API_KEY/GROQ_API_KEY
# et ignoreraient notre clé → échec d'auth même avec une clé valide.
_AIDER_CLOUD: dict[str, tuple[str, str, str]] = {
    "nvidia":   ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",  "openai"),
    "groq":     ("https://api.groq.com/openai/v1",      "GROQ_API_KEY",    "openai"),
    "cerebras": ("https://api.cerebras.ai/v1",          "CEREBRAS_API_KEY","openai"),
    "mistral":  ("https://api.mistral.ai/v1",           "MISTRAL_API_KEY", "openai"),
}


def generate_aider_headless(module_id: str, spec: str, kind: str, model: Optional[str] = None) -> Generator:
    """Lance aider en headless (--message), cwd=staging, streame stdout.

    model peut être :
    - None / nom Ollama simple  → local Ollama : --model ollama_chat/<model>
    - 'provider:model_id'       → cloud : --model <prefix>/<model_id> + OPENAI_API_BASE
    """
    aider_bin = _aider_bin()
    if not aider_bin:
        yield {"type": "error", "content": "aider introuvable. Installez : pip install aider-chat"}
        return

    sdir = _staging_dir(module_id)
    sdir.mkdir(parents=True, exist_ok=True)

    # Résolution du modèle et de l'env.
    # ATTENTION : un nom Ollama contient un ':' qui fait partie du TAG
    # (ex. « mistral-small:24b », « qwen2.5-coder:7b »). On ne traite donc le
    # préfixe comme un provider cloud QUE s'il appartient à _AIDER_CLOUD ;
    # sinon la chaîne entière (tag compris) est un nom de modèle Ollama.
    extra_env: dict = {"OLLAMA_API_BASE": os.environ.get("OLLAMA_API_BASE", "http://127.0.0.1:11434")}
    chosen = (model or (instance_config.get().get("providers") or {}).get("actif") or "").strip()
    provider, sep, rest = chosen.partition(":")
    if sep and provider in _AIDER_CLOUD:
        base_url, key_name, prefix = _AIDER_CLOUD[provider]
        api_key = os.environ.get(key_name, "").strip()
        if not api_key:
            yield {"type": "error", "content": f"{key_name} non configurée dans les Réglages."}
            return
        extra_env["OPENAI_API_BASE"] = base_url
        extra_env["OPENAI_API_KEY"] = api_key
        aider_model = f"{prefix}/{rest}"
    elif chosen:
        aider_model = f"ollama_chat/{chosen}"
    else:
        aider_model = "ollama_chat/qwen2.5-coder:7b"

    prompt = _claude_prompt(module_id, spec, kind)
    env = _local_agent_env(extra_env)

    cmd = [
        aider_bin,
        # --no-git est INDISPENSABLE : le staging est imbriqué dans le dépôt git
        # d'Épure ; sans ça aider s'attache au dépôt parent (repo-map sur tout le
        # code, lent/instable avec un petit modèle local) au lieu d'écrire juste
        # les 3 fichiers du module dans le cwd confiné.
        "--no-git",
        "--model", aider_model,
        "--message", prompt,
        "--yes-always",
        "--no-auto-commits",
        "--no-check-update",
        "--no-show-model-warnings",
        "manifest.json", "router.py", "Component.tsx",
    ]

    yield {"type": "engine", "engine": "aider", "model": aider_model}

    timed_out = {"flag": False}
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(sdir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        yield {"type": "error", "content": "aider introuvable dans le PATH."}
        return
    except Exception as exc:
        yield {"type": "error", "content": f"Lancement aider échoué : {exc}"}
        return

    def _watchdog():
        try:
            proc.wait(timeout=_CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out["flag"] = True
            try:
                proc.kill()
            except Exception:
                pass

    Thread(target=_watchdog, daemon=True).start()

    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                yield {"type": "token", "content": line + "\n"}
        proc.wait(timeout=10)
    except Exception as exc:
        yield {"type": "error", "content": str(exc)}
        return

    if timed_out["flag"]:
        yield {"type": "error", "content": f"Timeout aider ({_CLAUDE_TIMEOUT}s) — process tué."}
        return

    present = [n for n in _FILES if (sdir / n).is_file()]
    if not present:
        yield {"type": "error", "content": "aider n'a produit aucun des 3 fichiers dans le staging."}
        return
    yield {"type": "generation_done", "files": present}


def terminal_launch_spec(module_id: str, spec: str, kind: str, engine: str) -> dict:
    """Décrit la commande/cwd/env d'une session terminale confinée au staging.

    L'app lance un terminal réel pré-positionné ; à la fermeture (signal du
    frontend), on relance read_staging + validate_staging.
    """
    sdir = _staging_dir(module_id)
    sdir.mkdir(parents=True, exist_ok=True)
    # En mode terminal interactif on n'impose pas -p/stream-json : on ouvre claude
    # interactif dans le dossier confiné (l'utilisateur pilote).
    claude = _claude_bin() or "claude"
    interactive = [claude, "--add-dir", str(sdir), "--allowedTools", "Read,Write,Edit"]
    if engine == "claude_gateway" and _gateway_cfg()["model"]:
        interactive += ["--model", _gateway_cfg()["model"]]
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


def typecheck_staging(module_id: str) -> dict:
    """tsc sur le composant stagé, en tâche de fond (best-effort). Retourne ses
    warnings — ne change jamais le verdict d'activation (report.ok du gate)."""
    from core.module_validate import typecheck_component
    comp = _staging_dir(module_id) / "Component.tsx"
    if not comp.is_file():
        return {"warnings": []}
    rep = typecheck_component(comp.read_text(encoding="utf-8", errors="replace"), module_id)
    # tsc est best-effort : on remonte tout (warnings + éventuelles erreurs) en warnings.
    return {"warnings": rep.warnings + rep.errors}


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

    # Normalise le manifest sur disque : le LLM peut omettre/mal remplir des
    # champs. ModuleManifest applique les défauts (nom=id capitalisé, icon=Box,
    # prefix="", origin="workshop", removable=True, core_module=False si absents)
    # et on FORCE status="active" — le module doit ressortir actif dans /modules
    # quoi qu'ait produit le LLM.
    dest_manifest = dest / "manifest.json"
    try:
        raw = json.loads(dest_manifest.read_text(encoding="utf-8")) if dest_manifest.is_file() else {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    raw["id"] = module_id
    norm = ModuleManifest(**raw)
    norm.status = "active"
    dest_manifest.write_text(
        json.dumps(norm.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Override de status persistant (modules_state.json) — ceinture+bretelles.
    module_registry.set_status(module_id, "active")

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
