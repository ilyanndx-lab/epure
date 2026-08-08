"""Worker autonome d'exécution d'un module généré isolé — process séparé.

JAMAIS importé pour son exécution par le backend principal (comme
smoke_runner.py) : il est lancé en sous-processus avec un environnement
EXPURGÉ (allowlist stricte — aucune clé API, aucun token d'instance, pas de
PYTHONPATH héritant du backend), monte le router du module sur une app FastAPI
minimale, et écoute sur 127.0.0.1 (bind explicite, jamais 0.0.0.0).

Frontière de sécurité (cf. docs/isolation_modules.md) : le module ne voit ni la
mémoire du process hôte, ni ses variables d'environnement, ni le token d'API.
Deux mécanismes complémentaires posés ici :

  1. ENV EXPURGÉ — l'appelant lance le worker via ``spawn_worker`` qui construit
     l'environnement avec ``build_worker_env`` (allowlist). Les secrets du
     principal ne sont jamais transmis.
  2. ISOLATION D'IMPORT — avant de charger le router, on installe un faux
     package ``core`` en mémoire n'exposant QUE ``core.runtime`` (shim : llm +
     SSE_HEADERS relayés en IPC vers le principal). Tout autre ``core.*``
     (core.instance, core.memory, core.auth…) lève un ImportError CLAIR À
     L'IMPORT du module — bruyamment, visible en revue, pas silencieusement au
     premier appel. Le vrai ``backend/core`` n'est de toute façon plus sur le
     sys.path du worker.

Le backend principal (ModuleHost, phase suivante) réutilise ``build_worker_env``,
``spawn_worker`` et ``wait_healthy`` — les seules fonctions sûres à importer
depuis ce module (aucun effet de bord à l'import ; tout est sous fonctions ou
le garde ``__main__``).
"""

import argparse
import hmac
import importlib.abc
import importlib.util
import json
import os
import socket
import subprocess
import sys
import traceback
import types
from pathlib import Path

# ── En-têtes / variables de contrat worker ───────────────────────────────────
WORKER_KEY_HEADER = "X-Epure-Worker-Key"
ENV_WORKER_KEY = "EPURE_WORKER_KEY"            # clé partagée (injectée hors allowlist)
ENV_CAPABILITIES_URL = "EPURE_CAPABILITIES_URL"  # base http du principal (capabilities)
HEALTH_PATH = "/__worker__/health"

# Évite une fenêtre console volée sous Windows (comme module_workshop).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── Environnement expurgé (allowlist — c'est la frontière, pas une denylist) ──
# Une allowlist ne peut pas « rater » un secret au motif d'un nom inattendu :
# tout ce qui n'est pas explicitement autorisé disparaît. On ne garde que le
# strict nécessaire pour démarrer Python + ouvrir un socket loopback, multi-OS.
_ALLOWED_ENV_UPPER = {
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "OS", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
}


def build_worker_env(parent_env: dict, worker_key: str,
                     capabilities_url: str | None = None) -> dict:
    """Environnement du worker : allowlist du parent + variables de contrat.

    Retire TOUT le reste (clés API, token d'instance, PYTHONPATH/PYTHONHOME,
    OLLAMA_*, USERPROFILE/APPDATA…). ``worker_key`` et ``capabilities_url`` sont
    des variables PROPRES au worker (pas des secrets du principal) : la clé
    n'autorise que le dialogue proxy⇄worker, l'URL pointe vers les capabilities.
    """
    env = {
        k: v for k, v in parent_env.items()
        if k.upper() in _ALLOWED_ENV_UPPER and v
    }
    env["PYTHONIOENCODING"] = "utf-8"
    # Ne pas hériter d'un PYTHONPATH/PYTHONHOME : le worker ne doit pas pouvoir
    # atteindre backend/core par le sys.path (défense en profondeur au cas où
    # le garde d'import serait contourné).
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__"):
        env.pop(k, None)
    env[ENV_WORKER_KEY] = worker_key
    if capabilities_url:
        env[ENV_CAPABILITIES_URL] = capabilities_url
    return env


# ── Shim core.runtime (LLM + SSE) relayé en IPC vers le principal ─────────────

class _LLMShim:
    """Relaie llm.generate / llm.stream vers /capabilities/llm/* du principal.

    Le principal applique quotas et résolution de modèle ; le worker n'a aucun
    accès direct aux moteurs ni aux clés. Sans URL de capabilities configurée,
    tout appel lève une RuntimeError claire (le module fonctionne tant qu'il
    n'appelle pas le LLM — cas de pong, qui importe llm sans l'utiliser)."""

    def __init__(self, base_url: str | None, worker_key: str):
        self._base = (base_url or "").rstrip("/")
        self._key = worker_key
        self._model = None  # résolu côté principal ; exposé pour compat lecture

    def _require_base(self) -> str:
        if not self._base:
            raise RuntimeError(
                "LLM indisponible : ce module isolé n'a pas d'URL de capabilities "
                "configurée (le principal ne l'a pas fournie)."
            )
        return self._base

    def generate(self, messages, model=None) -> str:
        import httpx
        url = self._require_base() + "/capabilities/llm/generate"
        r = httpx.post(url, headers={WORKER_KEY_HEADER: self._key},
                       json={"messages": messages, "model": model}, timeout=120)
        r.raise_for_status()
        return r.json().get("text", "")

    def stream(self, messages, model=None):
        """Génère les tokens (str) depuis le SSE de capabilities."""
        import httpx
        url = self._require_base() + "/capabilities/llm/stream"
        with httpx.stream("POST", url, headers={WORKER_KEY_HEADER: self._key},
                          json={"messages": messages, "model": model}, timeout=None) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    ev = json.loads(payload)
                except ValueError:
                    continue
                if ev.get("type") == "token" and "content" in ev:
                    yield ev["content"]


class _CoreImportGuard(importlib.abc.MetaPathFinder):
    """Refuse tout ``core.*`` hors ``core`` / ``core.runtime`` (déjà en cache).

    Lève un ImportError explicite DÈS l'import (le finder est consulté avant
    tout chargement) : un module qui tente ``from core.instance import …``
    échoue bruyamment au chargement, visible en revue — jamais un stub muet."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "core" or fullname.startswith("core."):
            raise ImportError(
                f"Import interdit dans un module isolé : « {fullname} ». "
                "Un module généré accède au LLM et au stockage UNIQUEMENT via "
                "core.runtime (llm, SSE_HEADERS) — voir backend/modules/_atelier/"
                "CONVENTIONS.md. Tout autre core.* est refusé par isolation."
            )
        return None  # laisse les autres finders gérer le reste (fastapi, stdlib…)


def _install_core_shim(capabilities_url: str | None, worker_key: str) -> None:
    """Installe le faux package ``core`` (runtime seul) + le garde d'import."""
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = []  # package sans chemin : aucun sous-module par le disque
    runtime = types.ModuleType("core.runtime")
    runtime.llm = _LLMShim(capabilities_url, worker_key)
    runtime.SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    core_pkg.runtime = runtime
    sys.modules["core"] = core_pkg
    sys.modules["core.runtime"] = runtime
    # En tête de meta_path : intercepte core.* avant tout autre finder.
    sys.meta_path.insert(0, _CoreImportGuard())


def _harden_sys_path() -> None:
    """Retire du sys.path le dossier du script (backend/core) et backend/, pour
    que ni ``core.x`` ni ``import x`` (x=instance, memory…) ne puisse résoudre
    vers le vrai backend/core. Le worker ne charge le router que par chemin."""
    here = Path(__file__).resolve().parent          # backend/core
    backend = here.parent                            # backend
    banned = {str(here), str(backend), ""}
    sys.path[:] = [p for p in sys.path if p not in banned and Path(p).resolve() != here]


# ── Chargement du router + app minimale ───────────────────────────────────────

def _load_router(router_path: Path, module_id: str):
    spec = importlib.util.spec_from_file_location(f"_isolated_{module_id}_router", router_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # un core.* interdit lève ImportError ICI (bruyant)
    router = getattr(mod, "router", None)
    if router is None:
        raise RuntimeError(f"{router_path} ne définit pas de variable `router`.")
    return router


def build_app(module_dir: Path, module_id: str, prefix: str, worker_key: str):
    """App FastAPI minimale : middleware clé worker + health + router du module."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.middleware("http")
    async def _require_worker_key(request: Request, call_next):
        got = request.headers.get(WORKER_KEY_HEADER, "")
        if not hmac.compare_digest(got, worker_key):
            return JSONResponse({"detail": "clé worker manquante ou invalide"}, status_code=401)
        return await call_next(request)

    @app.get(HEALTH_PATH)
    async def _health():
        return {"ok": True, "module": module_id}

    router = _load_router(module_dir / "router.py", module_id)
    app.include_router(router, prefix=prefix)
    return app


def _read_manifest(module_dir: Path) -> dict:
    try:
        return json.loads((module_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# ── Lancement (réutilisé par le ModuleHost du principal + les tests) ──────────

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_worker(module_dir: Path, port: int, worker_key: str,
                 capabilities_url: str | None = None,
                 parent_env: dict | None = None, **popen_kwargs) -> subprocess.Popen:
    """Lance le worker en sous-processus avec l'ENV EXPURGÉ. Bind 127.0.0.1:port."""
    env = build_worker_env(parent_env if parent_env is not None else dict(os.environ),
                           worker_key, capabilities_url)
    cmd = [sys.executable, str(Path(__file__).resolve()),
           "--module", str(Path(module_dir).resolve()), "--port", str(port)]
    return subprocess.Popen(
        cmd, env=env, stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW, **popen_kwargs,
    )


def wait_healthy(port: int, worker_key: str, proc: subprocess.Popen | None = None,
                 timeout: float = 15.0) -> bool:
    """Attend que le worker réponde 200 sur /__worker__/health. False au timeout
    ou si le process meurt avant (démarrage échoué → ne pas boucler pour rien)."""
    import time
    import httpx
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False  # worker mort (import interdit, crash au boot…)
        try:
            r = httpx.get(url, headers={WORKER_KEY_HEADER: worker_key}, timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def main() -> int:
    _harden_sys_path()
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True, help="dossier du module (router.py + manifest.json)")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    worker_key = os.environ.get(ENV_WORKER_KEY, "")
    if not worker_key:
        print(f"{ENV_WORKER_KEY} absent de l'environnement — refus de démarrer.", file=sys.stderr)
        return 2
    capabilities_url = os.environ.get(ENV_CAPABILITIES_URL) or None

    _install_core_shim(capabilities_url, worker_key)

    module_dir = Path(args.module).resolve()
    module_id = module_dir.name
    manifest = _read_manifest(module_dir)
    prefix = (manifest.get("backend") or {}).get("prefix") or ""

    app = build_app(module_dir, module_id, prefix, worker_key)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # Échec de démarrage (ex. import core.* interdit) : traceback complet sur
        # stderr pour être VISIBLE (le parent lit stderr) puis code non nul.
        traceback.print_exc()
        raise SystemExit(1)
