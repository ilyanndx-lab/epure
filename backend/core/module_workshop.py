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
import shlex
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.error
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


class SessionLockedError(ValueError):
    """Le staging est tenu par une session terminale VIVANTE (fenêtre ouverte).

    Sous-classe de ValueError (rien ne casse si un appelant fait `except
    ValueError`), mais l'API la distingue pour répondre 409 plutôt que 400 :
    le front propose alors « Reprendre la session » (re-scan NON destructif du
    staging existant) au lieu d'afficher un cul-de-sac qui force à fermer la
    fenêtre — ou à passer par un humain — pour repartir."""


# Évite l'ouverture de fenêtres console visibles (qui volent le focus) quand on
# lance claude/.cmd en subprocess sous Windows. 0 sur les autres OS.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ── Racines confinées ────────────────────────────────────────────────────────
MODULES_DIR = (Path(__file__).parent.parent / "modules").resolve()
STAGING_DIR = MODULES_DIR / "_staging"
BACKUPS_DIR = MODULES_DIR / "_backups"
_ATELIER_DIR = MODULES_DIR / "_atelier"
_FRONTEND_MODULES = (Path(__file__).parent.parent.parent / "frontend" / "src" / "modules").resolve()
_FRONTEND_GENERATED = _FRONTEND_MODULES / "generated"

_FILES = ("manifest.json", "router.py", "Component.tsx")
# Ids impossibles : collisions avec des dossiers internes. 'settings' n'est PAS
# réservé — c'est un vrai module éditable (avec avertissement côté UI).
_RESERVED_IDS = {"_staging", "_backups", "generated"}
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
_CLAUDE_TIMEOUT = 600  # secondes (génération headless)


# ── Contexte atelier : conventions + index + reads autorisés ──────────────────

def _write_module_index() -> Path:
    """Génère _atelier/MODULE_INDEX.md : id, nom, description de chaque module actif."""
    _ATELIER_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Modules existants dans cette instance Épure\n"]
    for m in module_registry.list_modules():
        lines.append(f"- **{m.get('id')}** ({m.get('nom','')}) — {m.get('description','') or 'sans description'}")
    p = _ATELIER_DIR / "MODULE_INDEX.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p

# Fichiers/dossiers JAMAIS exposés en lecture (fuite de secrets / données perso).
_READ_DENY = ("\\.env", "credential", "secret", "/memory/", "/chroma_db/", "/history/", "\\.key")

def _read_is_safe(path: Path) -> bool:
    s = str(path).replace("\\", "/").lower()
    return not any(re.search(d, s) for d in _READ_DENY)

def _atelier_read_files(extra: Optional[list[str]] = None, minimal: bool = False) -> list[str]:
    """Chemins --read (lecture seule) : conventions + index + exemple hello + extras autorisés.
    minimal=True (modèle local, contexte limité) : CONVENTIONS seul (pas d'index ni d'exemple)."""
    out: list[str] = []
    conv = _ATELIER_DIR / "CONVENTIONS.md"
    if conv.is_file():
        out.append(str(conv))
    if not minimal:                                  # local minimal : CONVENTIONS seul
        out.append(str(_write_module_index()))
        for name in ("manifest.json", "router.py"):
            f = MODULES_DIR / "hello" / name
            if f.is_file():
                out.append(str(f))
    for e in (extra or []):
        p = Path(e).expanduser()
        if not p.is_absolute():
            p = (MODULES_DIR.parent.parent / e).resolve()  # racine projet
        if p.exists() and _read_is_safe(p):
            if p.is_dir():
                for sub in list(p.rglob("*.py"))[:20] + list(p.rglob("*.tsx"))[:20] + list(p.rglob("*.md"))[:10]:
                    if _read_is_safe(sub):
                        out.append(str(sub))
            else:
                out.append(str(p))
    return out


def grant_read(module_id: str, path: str) -> bool:
    """Autorise un dossier/fichier en lecture pour l'atelier (ajout à meta.extra_reads).
    Refuse si inexistant ou non sûr (secrets / données perso). Renvoie True si accordé."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (MODULES_DIR.parent.parent / path).resolve()
    if not (p.exists() and _read_is_safe(p)):
        return False
    meta = _read_meta(module_id) or {}
    a = meta.setdefault("aider", {})
    a["extra_reads"] = list({*(a.get("extra_reads") or []), str(p)})
    _write_meta(module_id, meta)
    return True


def _modules_safe_path(relative: str) -> Path:
    """Résout un chemin et refuse toute sortie de backend/modules/.

    Même garde-fou que codeagent._safe_path, mais raciné sur modules/ :
    comparaison de chemins résolus via ``is_relative_to`` (et non un
    ``startswith`` de chaînes, contournable par un dossier frère du type
    ``modules-autre/`` ou une traversée ``..``/symlink).
    """
    target = (MODULES_DIR / relative).resolve()
    if not target.is_relative_to(MODULES_DIR):
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

def _check_module_id(module_id: str) -> str:
    """Valide un identifiant de module venant du client, ou lève SecurityError.

    ``_modules_safe_path`` ne suffit pas : ``_staging/../chat`` reste sous
    ``modules/``, donc le confinement ne voit rien passer. Contraindre l'id à
    ``[a-z][a-z0-9_]{1,30}`` est ce qui rend sûres les constructions de chemin
    ET la ligne de commande du terminal Atelier (le dossier de staging finit
    dans un .bat).

    Posé ici en attendant le lot 3, qui le déplacera dans ``_staging_dir`` pour
    couvrir d'un coup tous les appelants (reject, read_staging, grant_read…).
    """
    mid = (module_id or "").strip()
    if not _ID_RE.match(mid):
        raise SecurityError(f"Identifiant de module invalide : {module_id!r}")
    return mid


def _staging_dir(module_id: str) -> Path:
    return _modules_safe_path(f"_staging/{module_id}")


def _staging_locked(sdir: Path) -> bool:
    """True si une SESSION tient encore le dossier de staging (terminal dont le cwd
    est le staging, ou process aider en cours).

    Test NON destructif par renommage atomique : sous Windows, renommer un dossier
    dont un process a fait son cwd lève une OSError ; si le renommage réussit,
    personne ne le tient (on le restaure aussitôt). Sert à distinguer « session en
    cours » (refuser, sinon on détruirait des modifications en cours) d'un simple
    résidu à nettoyer. Sans état persistant : impossible de rester « bloqué » sur
    un drapeau périmé après un crash de la session.
    """
    if not sdir.exists():
        return False
    probe = sdir.with_name(sdir.name + ".__lockprobe__")
    try:
        if probe.exists():
            shutil.rmtree(probe, ignore_errors=True)
        sdir.rename(probe)
    except OSError:
        return True  # verrouillé : session vivante
    try:
        probe.rename(sdir)
    except OSError:
        logger.warning("Sonde de verrou : %s déplacé mais non restauré", sdir)
    return False


def cleanup_orphan_staging() -> list[str]:
    """Supprime les dossiers de staging orphelins : sans .workshop.json ET non
    verrouillés. Ce sont les restes d'un rmtree partiel (approve/reject pendant
    qu'un terminal tenait encore le dossier sous Windows → fichiers retirés mais
    dossier vide laissé). Renvoie la liste des ids nettoyés."""
    removed: list[str] = []
    if not STAGING_DIR.is_dir():
        return removed
    for sub in STAGING_DIR.iterdir():
        if not sub.is_dir() or (sub / ".workshop.json").is_file():
            continue                      # staging réel : on garde
        if _staging_locked(sub):
            continue                      # session vivante : on ne touche pas
        shutil.rmtree(sub, ignore_errors=True)
        if not sub.exists():
            removed.append(sub.name)
    return removed


def _meta_path(module_id: str) -> Path:
    return _staging_dir(module_id) / ".workshop.json"


def _read_meta(module_id: str) -> Optional[dict]:
    p = _meta_path(module_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
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
        # GARDE-FOU FENÊTRES MULTIPLES : si une session est déjà ouverte sur ce
        # module (terminal dont le cwd = staging), on REFUSE au lieu de wiper en
        # silence. L'ancien code faisait rmtree(ignore_errors) → il détruisait les
        # modifications en cours d'une autre fenêtre (et laissait des dossiers vides).
        # Le test de verrou est non destructif : il n'efface rien avant d'avoir
        # confirmé que le dossier est libre.
        if _staging_locked(sdir):
            raise SessionLockedError(
                f"Une session est déjà ouverte sur le module « {module_id} » "
                "(fenêtre terminal active). Inutile de la fermer : clique "
                "« Reprendre la session » pour re-scanner le travail en cours "
                "et passer à la revue — rien n'est perdu. (Repartir de zéro "
                "exige de fermer d'abord la fenêtre.)"
            )
        try:
            shutil.rmtree(sdir)
        except OSError:
            # Pas verrouillé mais rmtree échoue (résidu partiel, antivirus, handle
            # bref) : nettoyage best-effort — aucune session vivante à protéger ici.
            shutil.rmtree(sdir, ignore_errors=True)
            for f in (*_FILES, ".aider.chat.history.md", ".workshop.json"):
                try:
                    (sdir / f).unlink()
                except Exception:
                    pass
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
    # `raw()` et non `get()` : depuis le durcissement v1, `get()` expurge
    # `api_key` (elle sortait en clair par GET /instance/config). Le moteur
    # claude_gateway, lui, a besoin de la vraie valeur.
    gw = ((instance_config.raw().get("atelier") or {}).get("gateway") or {})
    return {
        "base_url": gw.get("base_url", "http://localhost:4000"),
        "model": (gw.get("model") or "").strip(),
        "api_key": (gw.get("api_key") or "").strip(),
        "start_command": (gw.get("start_command") or "").strip(),
    }


#: Binaires acceptés en tête de `atelier.gateway.start_command`.
#: Garde-fou anti-accident, PAS une frontière de sécurité : `python`/`npx`/`uvx`
#: permettent par construction d'exécuter du code arbitraire. Ce qui est
#: réellement fermé ici, c'est l'injection par métacaractères de shell.
_GATEWAY_ALLOWED_BINS = {"litellm", "python", "python3", "py", "npx", "uv", "uvx"}


def _split_command(cmd: str) -> list[str]:
    """Découpe une ligne de commande utilisateur en argv, sans shell.

    Sous Windows on ne peut pas utiliser le mode POSIX de shlex : il traite
    l'antislash comme un échappement et transformerait ``C:\\Users\\x`` en
    ``C:Usersx``. En mode non-POSIX, shlex conserve en revanche les guillemets
    *dans* les jetons (``'"C:\\Mes Configs\\cfg.yaml"'``) — on les retire donc,
    sinon le chemin arriverait au programme avec ses guillemets littéraux.
    """
    argv = shlex.split(cmd, posix=(os.name != "nt"))
    if os.name == "nt":
        argv = [
            a[1:-1] if len(a) >= 2 and a[0] == a[-1] == '"' else a
            for a in argv
        ]
    return argv


def start_gateway() -> dict:
    """Lance la passerelle via atelier.gateway.start_command (process détaché).

    Commande fournie par l'utilisateur (ex. `litellm --config cfg.yaml`),
    détachée du backend, sans fenêtre console. Ne bloque pas — le front re-teste
    la joignabilité après quelques secondes.

    Elle était passée telle quelle à ``shell=True``. Or ``start_command`` est une
    chaîne libre écrite par ``PUT /instance/config`` (core.instance ne protège
    que ``instance_id`` et ``auth``) : deux requêtes suffisaient à poser une
    commande puis à la faire exécuter. Elle est maintenant découpée en argv et
    lancée sans shell, et son binaire de tête doit figurer dans
    :data:`_GATEWAY_ALLOWED_BINS`.
    """
    cfg = _gateway_cfg()
    cmd = cfg["start_command"]
    if not cmd:
        return {"ok": False, "raison": "Aucune commande de démarrage configurée (Réglages › Atelier)."}
    if gateway_reachable(cfg["base_url"]):
        return {"ok": True, "raison": "Passerelle déjà joignable."}
    try:
        argv = _split_command(cmd)
    except ValueError as exc:  # guillemet non fermé
        return {"ok": False, "raison": f"Commande de démarrage illisible : {exc}"}
    if not argv:
        return {"ok": False, "raison": "Commande de démarrage vide."}
    head = Path(argv[0]).stem.lower()
    if head not in _GATEWAY_ALLOWED_BINS:
        return {
            "ok": False,
            "raison": f"Binaire non autorisé : {argv[0]}. "
                      f"Autorisés : {', '.join(sorted(_GATEWAY_ALLOWED_BINS))}.",
        }
    try:
        flags = _NO_WINDOW
        if os.name == "nt":
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            argv, shell=False, cwd=str(Path.home()),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=flags,
        )
    except FileNotFoundError:
        return {"ok": False, "raison": f"Binaire introuvable : {argv[0]}"}
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
        full = url.rstrip("/") + path
        try:
            req = urllib.request.Request(full, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError as e:
            # Le serveur a RÉPONDU (401/403/404…) → joignable, même si un simple
            # GET sans auth est refusé (cas des endpoints Anthropic comme DeepSeek).
            if e.code < 500:
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
            timeout=8, env=_claude_env("claude_sub"),
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
            timeout=8, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
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


_engines_cache: dict = {"data": None, "at": 0.0}
_ENGINES_TTL = 60  # s — les checks (subprocess --version, réseau) coûtent ~10 s ;
                   # on les met en cache pour ne pas les rejouer à chaque ouverture.


def engines_status(force: bool = False) -> dict:
    """Disponibilité des moteurs : {disponible, raison} (+ infos utiles).

    Résultat mis en cache _ENGINES_TTL s (force=True pour « Re-tester »). Les
    checks lents (claude/aider --version, ping passerelle, ollama) sont lancés en
    PARALLÈLE — sinon ~13 s en séquentiel à chaque chargement des Réglages/Atelier.
    """
    now = time.time()
    if not force and _engines_cache["data"] is not None and now - _engines_cache["at"] < _ENGINES_TTL:
        return _engines_cache["data"]

    claude_bin = _claude_bin()
    aider_bin = _aider_bin()
    gw = _gateway_cfg()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as _ex:
        _f_claude = _ex.submit(_claude_version_ok, claude_bin)
        _f_ollama = _ex.submit(_ollama_status)
        _f_gw = _ex.submit(gateway_reachable, gw["base_url"])
        _f_aider = _ex.submit(_bin_version_ok, aider_bin)
        ver_ok = _f_claude.result()
        o_ok, o_raison = _f_ollama.result()
        gw_reach = _f_gw.result()
        aider_ver_ok = _f_aider.result()

    no_cli = (
        "CLI `claude` introuvable/inexécutable — installez-le "
        "(npm i -g @anthropic-ai/claude-code) ou corrigez claude_path (Réglages › Atelier)."
    )

    if not ver_ok:
        sub_ok, sub_raison = False, no_cli
    elif _claude_auth_detected():
        sub_ok, sub_raison = True, ""
    else:
        sub_ok, sub_raison = False, (
            "Pas d'auth d'abonnement détectée — lancez `claude setup-token` "
            "(ou `claude` puis /login), puis Re-tester."
        )

    if not ver_ok:
        gw_ok, gw_raison = False, no_cli
    elif not gw_reach:
        gw_ok, gw_raison = False, (
            f"Passerelle injoignable : {gw['base_url']} (démarrez-la ou corrigez l'URL)."
        )
    else:
        gw_ok, gw_raison = True, ""

    # ── aider ────────────────────────────────────────────────────────────────
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

    result = {
        "ollama": {"disponible": o_ok, "raison": o_raison},
        "claude_sub": {"disponible": sub_ok, "raison": sub_raison, "bin": claude_bin or ""},
        "claude_gateway": {
            "disponible": gw_ok, "raison": gw_raison,
            "base_url": gw["base_url"], "model": gw["model"],
        },
        "aider": {"disponible": aid_ok, "raison": aid_raison, "bin": aider_bin or ""},
    }
    _engines_cache["data"] = result
    _engines_cache["at"] = time.time()
    return result


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
        f"accès aux clés API. backend.prefix vaut \"\" : le router est monté À LA "
        f"RACINE, donc préfixe TOI-MÊME chaque route par /{module_id} pour éviter "
        f"les collisions entre modules (ex. `@router.post(\"/{module_id}/action\")`).\n"
        "- Component.tsx : composant React par défaut. Imports : "
        "`../../../components/ui` pour l'UI, `../../registry` pour SharedModuleProps. "
        f"Depuis components/ui, tu ne peux importer QUE ces composants (aucun autre "
        f"n'existe — pas de Label, CardHeader, etc.) : {', '.join(ui_component_exports()) or 'Button, Card, Badge, Input, Textarea, Toggle, Select, Tooltip, Tabs, ProgressBar, Modal, ThemeToggle'}. "
        "Pour tout le reste (label, titre…), utilise des balises HTML standard. "
        "INTERDIT : dangerouslySetInnerHTML, eval. Appelle le backend sur EXACTEMENT "
        f"les mêmes chemins que le router (préfixe /{module_id} INCLUS) : "
        f"fetch('http://localhost:8000/{module_id}/...'). Les chemins frontend et "
        "router DOIVENT être identiques, sinon 404.\n"
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
        # Certains modèles recopient les balises placeholder du prompt
        # (`<python>`, `<json>`, `<tsx>`) en tête/queue de bloc → fichier
        # syntaxiquement invalide alors que le contenu est bon. On les retire.
        body = re.sub(r"^<(?:python|json|tsx)>\s*\n?", "", body)
        body = re.sub(r"\n?\s*</(?:python|json|tsx)>\s*$", "", body)
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
            # Claude Code appelle des sous-modèles selon la tâche (haiku = tâches de fond,
            # sonnet/opus = principal). Sans ces alias, il enverrait un nom de modèle
            # Claude que l'endpoint tiers (DeepSeek) ne connaît pas → tâches en échec.
            fast = gw["model"].replace("v4-pro", "v4-flash") or gw["model"]
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = gw["model"]
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = gw["model"]
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = fast
            env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
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
        "dangerouslySetInnerHTML ni eval. backend.prefix vaut \"\" (router monté à la "
        f"racine) : préfixe CHAQUE route par /{module_id} dans router.py ET appelle "
        f"ces mêmes chemins /{module_id}/... depuis le frontend (identiques, sinon "
        "404). N'écris QUE dans le dossier courant.\n\n"
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
    # DeepSeek : aider le gère NATIVEMENT (prefix "deepseek/" + DEEPSEEK_API_KEY),
    # donc on n'utilise PAS le chemin OpenAI-compatible (cf. branche dédiée plus bas).
    "deepseek": ("https://api.deepseek.com",            "DEEPSEEK_API_KEY","deepseek"),
}


def _aider_timeout() -> int:
    """Délai (secondes) avant pause d'une génération aider, depuis la config."""
    mn = ((instance_config.get().get("atelier") or {}).get("aider_timeout_min") or 15)
    try:
        return max(60, int(mn) * 60)
    except Exception:
        return 900


def _aider_resolve(model: Optional[str]) -> tuple[str, dict]:
    """(modèle aider, env supplémentaire) à partir d'un id Épure.

    ATTENTION : un nom Ollama contient un ':' (tag, ex. « mistral-small:24b »).
    On ne traite le préfixe comme provider cloud que s'il est dans _AIDER_CLOUD ;
    sinon toute la chaîne (tag compris) est un nom Ollama. Lève ValueError si la
    clé du provider cloud est absente.
    """
    extra_env = {"OLLAMA_API_BASE": os.environ.get("OLLAMA_API_BASE", "http://127.0.0.1:11434")}
    chosen = (model or (instance_config.get().get("providers") or {}).get("actif") or "").strip()
    provider, sep, rest = chosen.partition(":")
    if sep and provider in _AIDER_CLOUD:
        base_url, key_name, prefix = _AIDER_CLOUD[provider]
        api_key = os.environ.get(key_name, "").strip()
        if not api_key:
            raise ValueError(f"{key_name} non configurée dans les Réglages.")
        if provider == "deepseek":
            extra_env["DEEPSEEK_API_KEY"] = api_key
        else:
            extra_env["OPENAI_API_BASE"] = base_url
            extra_env["OPENAI_API_KEY"] = api_key
        return f"{prefix}/{rest}", extra_env
    if chosen:
        return f"ollama_chat/{chosen}", extra_env
    return "ollama_chat/qwen2.5-coder:7b", extra_env


def _aider_cmd(aider_bin, aider_model, message, edit_fmt, architect, restore,
               chat_mode=None, read_files=None):
    """Commande aider headless confinée (--no-git), avec flags de fiabilité.

    --no-git est INDISPENSABLE : le staging est imbriqué dans le dépôt git d'Épure ;
    sans ça aider s'attache au dépôt parent (repo-map sur tout le code) au lieu
    d'écrire juste les 3 fichiers dans le cwd. L'historique de chat est persisté
    dans le staging pour permettre la reprise (--restore-chat-history).
    chat_mode="ask" → mode Plan (discute, n'édite pas). read_files → fichiers
    en lecture seule (--read) : conventions, index des modules, exemple…
    """
    cmd = [aider_bin, "--no-git", "--chat-history-file", ".aider.chat.history.md", "--model", aider_model]
    if restore:
        cmd += ["--restore-chat-history"]
    if chat_mode == "ask":
        cmd += ["--chat-mode", "ask"]                      # Plan : discute, n'édite pas
    elif architect:
        # Modèle éditeur moins cher pour DeepSeek (pro raisonne, flash applique).
        editor_model = aider_model
        if aider_model.startswith("deepseek/") and "v4-pro" in aider_model:
            editor_model = "deepseek/deepseek-v4-flash"
        cmd += ["--architect", "--editor-model", editor_model, "--editor-edit-format", edit_fmt]
    else:
        cmd += ["--edit-format", edit_fmt]
    for rf in (read_files or []):
        cmd += ["--read", rf]
    cmd += ["--message", message, "--yes-always", "--no-auto-commits", "--no-check-update",
            "--no-show-model-warnings", "--no-detect-urls", "--no-pretty", "--chat-language", "French",
            "--map-tokens", "0", "manifest.json", "router.py", "Component.tsx"]
    return cmd


def _run_aider_proc(module_id, sdir, cmd, env) -> Generator:
    """Exécute aider, streame stdout. Sur timeout → PAUSE (status 'paused',
    travail conservé) au lieu de tuer + erreur — permet la reprise."""
    timed_out = {"flag": False}
    proc = subprocess.Popen(cmd, cwd=str(sdir), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)

    def _watchdog():
        try:
            proc.wait(timeout=_aider_timeout())
        except subprocess.TimeoutExpired:
            timed_out["flag"] = True
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass

    Thread(target=_watchdog, daemon=True).start()
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            yield {"type": "token", "content": line + "\n"}
    proc.wait(timeout=10)
    if timed_out["flag"]:
        meta = _read_meta(module_id) or {}
        meta["status"] = "paused"
        _write_meta(module_id, meta)
        yield {"type": "paused", "content": f"Délai atteint ({_aider_timeout()//60} min) — travail conservé. Cliquez « Continuer » pour reprendre."}
        return
    present = [n for n in _FILES if (sdir / n).is_file()]
    if not present:
        yield {"type": "error", "content": "aider n'a produit aucun des 3 fichiers."}
        return
    yield {"type": "generation_done", "files": present}


def aider_converse(module_id: str, message: str, mode: str = "plan", restore: bool = False,
                   fresh: bool = False, model: Optional[str] = None, architect: bool = False,
                   kind: str = "new", extra_reads: Optional[list[str]] = None) -> Generator:
    """Un tour de conversation aider (Plan ou Construire), multi-tours via l'historique.

    mode="plan" → --chat-mode ask (discute, n'édite pas) ; mode="build" → édite.
    extra_reads cumulés dans meta.aider pour rester disponibles aux tours suivants.
    fresh=True (1er tour d'un « Générer ») → repart de zéro : supprime l'historique
    de chat et force restore=False (sinon aider rejouerait/hallucinerait un passé).
    reads adaptatifs : minimal (CONVENTIONS seul) pour un modèle local (contexte
    limité), complet (index + exemple) pour le cloud.
    """
    aider_bin = _aider_bin()
    if not aider_bin:
        yield {"type": "error", "content": "aider introuvable. pip install aider-chat"}
        return
    sdir = _staging_dir(module_id)
    sdir.mkdir(parents=True, exist_ok=True)
    try:
        aider_model, extra_env = _aider_resolve(model)
    except ValueError as e:
        yield {"type": "error", "content": str(e)}
        return
    hist = sdir / ".aider.chat.history.md"
    if fresh and hist.exists():
        hist.unlink()
        restore = False                              # session propre : pas de confabulation
    meta = _read_meta(module_id) or {}
    prev = meta.get("aider") or {}
    reads = list({*(prev.get("extra_reads") or []), *(extra_reads or [])})
    meta["aider"] = {"model": model, "architect": bool(architect), "kind": kind, "extra_reads": reads}
    _write_meta(module_id, meta)
    is_local = aider_model.startswith("ollama_chat/")
    edit_fmt = "whole" if (kind == "new" and not any((sdir / n).is_file() and (sdir / n).stat().st_size > 2 for n in _FILES)) else "diff"
    chat_mode = "ask" if mode == "plan" else None
    cmd = _aider_cmd(aider_bin, aider_model, message, edit_fmt, architect, restore,
                     chat_mode=chat_mode, read_files=_atelier_read_files(reads, minimal=is_local))
    yield {"type": "engine", "engine": "aider", "model": aider_model, "mode": mode,
           "architect": bool(architect), "local": is_local}
    yield from _run_aider_proc(module_id, sdir, cmd, _local_agent_env(extra_env))


def generate_aider_headless(module_id: str, spec: str, kind: str, model: Optional[str] = None,
                            architect: bool = False, mode: str = "build") -> Generator:
    """Compat : délègue à aider_converse. mode='build' génère les fichiers ;
    mode='plan' demande d'abord un plan + questions sans rien créer."""
    if mode == "build":
        message = _claude_prompt(module_id, spec, kind)
    else:
        message = (spec + "\n\nProduis d'abord un PLAN détaillé (fichiers, routes, modèle LLM "
                   "via core.runtime.llm) et la LISTE de tes questions et des accès dont tu as besoin. "
                   "Ne crée AUCUN fichier pour l'instant.")
    yield from aider_converse(module_id, message, mode=mode, restore=False, model=model,
                              architect=architect, kind=kind, fresh=True)


def resume_aider_headless(module_id: str) -> Generator:
    """Compat : reprend une génération aider en pause via aider_converse (restore)."""
    a = (_read_meta(module_id) or {}).get("aider") or {}
    cont = ("Reprends EXACTEMENT là où tu t'es arrêté le travail précédent (voir l'historique). "
            "Les fichiers déjà écrits sont dans le dossier courant. Termine manifest.json, "
            "router.py et Component.tsx selon la demande initiale, sans tout réécrire de zéro.")
    yield from aider_converse(module_id, cont, mode="build", restore=True,
                              model=a.get("model"), architect=bool(a.get("architect")),
                              kind=a.get("kind", "new"))


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
    """Ouvre une vraie fenêtre terminal : cd staging + claude interactif, avec l'env
    du moteur (ANTHROPIC_* pour claude_gateway/DeepSeek). La clé n'est PAS écrite sur
    disque — elle est héritée via l'environnement du process lancé.

    ``module_id`` arrive de ``/ws/workshop`` sans validation (``msg.get("id")``)
    et se retrouvait interpolé dans une ligne de commande passée au shell : sous
    POSIX un nom de dossier contenant ``"; id; "`` est parfaitement légal. D'où
    la validation en entrée, et plus aucun shell des deux côtés.
    """
    module_id = _check_module_id(module_id)
    info = terminal_launch_spec(module_id, spec, kind, engine)
    sdir = info["cwd"]
    env = info["env"]
    try:
        if os.name == "nt":
            # .bat = titre + cd + claude (pas de clé dedans). list2cmdline est la
            # bonne mise en forme ici : c'est cmd.exe qui relira cette ligne.
            bat = Path(sdir) / "_launch.bat"
            bat.write_text(
                "@echo off\r\n"
                f"title Atelier {module_id}\r\n"
                f'cd /d "{sdir}"\r\n'
                f"call {subprocess.list2cmdline(info['cmd'])}\r\n",
                encoding="utf-8",
            )
            # CREATE_NEW_CONSOLE remplace `start` : c'est ce que `start` faisait
            # de toute façon, mais sans passer par cmd pour l'obtenir — donc
            # sans ligne de commande à assembler, et sans shell.
            subprocess.Popen(
                ["cmd", "/K", str(bat)],
                env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        else:
            # shlex.join et non list2cmdline : ces deux branches passent par un
            # sh POSIX, dont les règles de citation ne sont pas celles de cmd.
            posix_cmdline = shlex.join(info["cmd"])
            if shutil.which("tmux"):
                subprocess.Popen(
                    ["tmux", "new-window", "-c", sdir, posix_cmdline], env=env,
                )
            else:
                subprocess.Popen(
                    ["x-terminal-emulator", "-e",
                     f"bash -c 'cd {shlex.quote(sdir)}; {posix_cmdline}; exec bash'"],
                    env=env,
                )
        return {"opened": True, "cwd": sdir, "cmd": info["cmd"], "prompt": info["prompt"]}
    except Exception as exc:
        logger.exception("Ouverture terminal échouée")
        return {"opened": False, "error": str(exc), "cwd": sdir, "cmd": info["cmd"], "prompt": info["prompt"]}


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
    data: dict = {}
    try:
        data = json.loads(manifest_raw) if manifest_raw else {}
        data.setdefault("id", module_id)
        ModuleManifest(**data)
    except Exception as exc:
        errors.append(f"manifest.json invalide : {exc}")

    if not router_src:
        errors.append("router.py manquant.")
    else:
        # backend.prefix vide → routes forcément préfixées par /<id> (anti-collision).
        # is_core → tolère les motifs réseau/env des modules core ré-édités.
        mprefix = (data.get("backend") or {}).get("prefix", "")
        rr = validate_router_py(
            router_src, module_id=module_id, backend_prefix=mprefix, is_core=is_core(module_id)
        )
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


# ── Smoke test (sous-processus isolé) ────────────────────────────────────────

_SMOKE_TIMEOUT = 90  # s — import du router + requêtes TestClient (aucun moteur lourd)


def smoke_test_staging(module_id: str) -> dict:
    """Smoke test du router stagé dans un SOUS-PROCESSUS jetable — jamais dans
    le process FastAPI : importer du code généré non revu ici exécuterait ses
    effets de bord dans l'app en cours. Le runner (core/smoke_runner.py) stubbe
    core.runtime avant tout import (ni torch ni modèles), monte le router sur
    une app minimale et appelle chaque GET sans paramètre de chemin ; échec =
    import raté ou au moins un 5xx non imputable au stub.

    Best-effort, comme typecheck_staging : ne modifie JAMAIS le verdict du
    gate AST (report.ok). Retourne {"ok","tested","failures","skipped","error"}.
    """
    empty = {"ok": False, "tested": [], "failures": [], "skipped": []}
    sdir = _staging_dir(module_id)
    if not (sdir / "router.py").is_file():
        return {**empty, "error": "router.py absent du staging."}
    runner = Path(__file__).parent / "smoke_runner.py"
    try:
        r = subprocess.run(
            [sys.executable, str(runner), str(sdir), module_id],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_SMOKE_TIMEOUT, cwd=str(MODULES_DIR.parent),
            env=_make_exec_env(), stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {**empty, "error": f"Timeout du smoke test ({_SMOKE_TIMEOUT}s) — "
                "le router bloque à l'import ou sur une route (boucle/attente infinie)."}
    except Exception as exc:
        logger.exception("Lancement du smoke test %s échoué", module_id)
        return {**empty, "error": f"Lancement du smoke test échoué : {exc}"}
    # Le runner imprime son résultat en DERNIÈRE ligne JSON (des libs peuvent
    # polluer stdout avant, ex. warnings à l'import).
    for line in reversed((r.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                break
    tail = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()[-2000:]
    return {**empty, "error": f"Sortie du smoke test illisible (code {r.returncode}) :\n{tail}"}


def smoke_feedback(smoke: dict) -> str:
    """Formate un smoke test échoué en consigne de correction pour le moteur de
    génération actif — même canal que les erreurs du validateur AST."""
    parts = ["Le smoke test d'exécution du router a échoué (import de router.py "
             "puis appel des routes GET sur une app FastAPI minimale, core.runtime stubé)."]
    if smoke.get("error"):
        parts.append(str(smoke["error"])[:3000])
    for f in (smoke.get("failures") or [])[:5]:
        st = f" (HTTP {f['status']})" if f.get("status") else ""
        # Queue du traceback : l'exception réelle est à la FIN, pas au début.
        parts.append(f"- {f.get('route', '?')}{st} :\n{str(f.get('error') or '')[-1500:]}")
    parts.append("Corrige router.py pour que l'import réussisse et qu'aucune route GET "
                 "ne renvoie de 5xx. Ne change rien d'autre au comportement du module.")
    return "\n\n".join(parts)


def record_smoke(module_id: str, result: dict) -> None:
    """Persiste le résultat smoke dans la meta du staging (read_staging le
    renvoie → l'écran de revue le retrouve après un F5). No-op si le staging
    a disparu entre-temps (approuvé/rejeté pendant la tâche de fond)."""
    meta = _read_meta(module_id)
    if meta is None:
        return
    meta["smoke"] = result
    _write_meta(module_id, meta)


def remember_spec(module_id: str, spec: str) -> None:
    """Mémorise la description de génération dans la meta : les passes de
    réparation (smoke) et les reprises en ont besoin après coup."""
    if not (spec or "").strip():
        return
    meta = _read_meta(module_id)
    if meta is None or meta.get("spec") == spec:
        return
    meta["spec"] = spec
    _write_meta(module_id, meta)


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


def approve(module_id: str, app=None, force: bool = False) -> dict:
    """Active le module après revue : backup → déplacement → reload → activation.

    Refuse si la validation n'est pas passée (status != pending_review), SAUF si
    force=True (activation manuelle explicite par l'utilisateur, malgré des
    erreurs de validation — code potentiellement cassé/non sûr, à ses risques).
    """
    meta = _read_meta(module_id)
    if not meta:
        raise ValueError("Aucun staging à approuver.")
    # Revalidation (gate d'exécution) — on ne fait jamais confiance au status seul.
    res = validate_staging(module_id, run_tsc=False)
    if not res["report"]["ok"] and not force:
        return {"ok": False, "report": res["report"], "status": "draft",
                "detail": "Validation échouée — activation refusée."}
    if not res["report"]["ok"]:
        logger.warning("Activation FORCÉE de %s malgré des erreurs : %s",
                       module_id, res["report"]["errors"])

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
        raw = json.loads(dest_manifest.read_text(encoding="utf-8-sig")) if dest_manifest.is_file() else {}
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
    """Modules actuellement en atelier (staging). Balaie d'abord les dossiers
    orphelins (restes de rmtree partiel) pour qu'ils ne traînent pas."""
    cleanup_orphan_staging()
    out = []
    if STAGING_DIR.is_dir():
        for sub in sorted(STAGING_DIR.iterdir()):
            if sub.is_dir():
                meta = _read_meta(sub.name)
                if meta:
                    out.append(meta)
    return out
