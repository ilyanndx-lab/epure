"""Smoke test d'un router de module stagé — exécuté UNIQUEMENT en sous-processus.

Lancé par module_workshop.smoke_test_staging, jamais importé par le backend :
importer un router généré (non revu) dans le process FastAPI reviendrait à
exécuter ses effets de bord dans l'app en cours. Ici un crash, une boucle
infinie ou un import toxique ne touche que ce process jetable (le parent
applique un timeout et lit une ligne JSON sur stdout).

Mode dégradé : core.runtime est remplacé AVANT tout import par un module stub
(PEP 562) dont chaque attribut est un « trou noir » inerte — le smoke test ne
charge ni torch, ni les modèles, ni aucun moteur (_LazyEngine ne suffit pas :
l'import de core.runtime construit déjà LLMEngine/MemoryEngine et lance le
thread de préchauffage RAG). Une route qui échoue PARCE QUE le stub remplace
un vrai moteur est classée « skipped », pas en échec — seuls les 5xx propres
au code du module comptent.

Sortie (dernière ligne stdout) :
  {"ok": bool, "tested": [...], "failures": [{"route","status","error"}],
   "skipped": [...], "error": str|null}
"""

import json
import sys
import traceback
import types
from pathlib import Path

_MARKER = "EPURE_SMOKE_STUB"
_TB_LIMIT = 4000  # caractères max par traceback remonté


class _SmokeStub:
    """Trou noir inerte : tout attribut/appel renvoie un stub, itération vide,
    str '' — un handler qui streame/join le LLM stubé « réussit » avec une
    sortie vide au lieu de crasher. Le repr porte le marqueur : un traceback
    qui le contient signe une dépendance moteur, pas un bug du module."""

    def __init__(self, path: str = "core.runtime"):
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name):
        return _SmokeStub(f"{self._path}.{name}")

    def __call__(self, *args, **kwargs):
        return _SmokeStub(self._path + "()")

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    def __str__(self):
        return ""

    def __repr__(self):
        return f"<{_MARKER} {self._path}>"


def _stub_touched(text: str) -> bool:
    return _MARKER in text or "_SmokeStub" in text


def _install_runtime_stub() -> None:
    stub = types.ModuleType("core.runtime")
    stub.__getattr__ = lambda name: _SmokeStub(f"core.runtime.{name}")  # PEP 562
    sys.modules["core.runtime"] = stub


def main() -> dict:
    staging = Path(sys.argv[1]).resolve()
    module_id = sys.argv[2]
    # backend/ sur sys.path (sys.path[0] = backend/core quand lancé par chemin) :
    # nécessaire pour que le package `core` (vide) se résolve lors des imports
    # `from core.runtime import …` du router — interceptés par le stub.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    _install_runtime_stub()

    result: dict = {"ok": False, "tested": [], "failures": [], "skipped": [], "error": None}

    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(f"_smoke_{module_id}_router", staging / "router.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        result["error"] = "Import de router.py échoué :\n" + traceback.format_exc()[-_TB_LIMIT:]
        return result
    router = getattr(mod, "router", None)
    if router is None:
        result["error"] = "router.py ne définit pas de variable `router`."
        return result

    from fastapi import FastAPI
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    prefix = ""
    try:
        manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        prefix = (manifest.get("backend") or {}).get("prefix") or ""
    except Exception:
        pass

    app = FastAPI()
    try:
        app.include_router(router, prefix=prefix)
    except Exception:
        result["error"] = "Montage du router échoué :\n" + traceback.format_exc()[-_TB_LIMIT:]
        return result

    # raise_server_exceptions=True : une exception du handler remonte ici avec
    # son traceback complet (plus exploitable pour la réparation qu'un 500 nu).
    with TestClient(app, raise_server_exceptions=True) as client:
        for route in app.routes:
            if not isinstance(route, APIRoute) or "GET" not in (route.methods or ()):
                continue
            if "{" in route.path:
                result["skipped"].append(f"GET {route.path} (paramètre de chemin requis)")
                continue
            try:
                resp = client.get(route.path)
            except Exception:
                tb = traceback.format_exc()
                if _stub_touched(tb):
                    result["skipped"].append(f"GET {route.path} (dépend d'un moteur stubé — non testable hors app)")
                else:
                    result["failures"].append({"route": f"GET {route.path}", "status": None,
                                               "error": tb[-_TB_LIMIT:]})
                continue
            if resp.status_code >= 500:
                body = resp.text[:_TB_LIMIT]
                if _stub_touched(body):
                    result["skipped"].append(f"GET {route.path} (dépend d'un moteur stubé — non testable hors app)")
                else:
                    result["failures"].append({"route": f"GET {route.path}", "status": resp.status_code,
                                               "error": body})
            else:
                result["tested"].append(f"GET {route.path} → {resp.status_code}")

    result["ok"] = not result["failures"] and result["error"] is None
    return result


if __name__ == "__main__":
    # stdout en UTF-8 quoi qu'il arrive (console cp1252 sous Windows) : le JSON
    # contient des tracebacks/flèches non encodables sinon.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    out: dict = {"ok": False, "tested": [], "failures": [], "skipped": [], "error": None}
    try:
        out = main()
    except Exception:
        out["error"] = traceback.format_exc()[-_TB_LIMIT:]
    print(json.dumps(out, ensure_ascii=False))
