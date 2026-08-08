#!/usr/bin/env python3
"""Tests d'isolation des modules générés (Phase 1 : worker + shim + garde d'import).

Vérifie la frontière de sécurité posée par core/module_worker.py :
  1. Cas canonique — le module `hello` répond via un worker isolé.
  2. Étanchéité mémoire — depuis un router isolé, os.environ ne contient NI les
     clés API du parent NI le token d'instance ; `import core.instance` lève
     ImportError.
  3. Échec bruyant — un router qui importe core.* interdit AU NIVEAU MODULE fait
     échouer le démarrage du worker (ImportError visible sur stderr), pas au
     premier appel runtime.

Chaque worker tourne dans un vrai sous-processus avec l'environnement expurgé
produit par build_worker_env. Aucun mock : on interroge le worker en HTTP.

Usage :
    python test_module_isolation.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

import httpx
import secrets as _secrets

from core.module_worker import (
    WORKER_KEY_HEADER, HEALTH_PATH,
    build_worker_env, spawn_worker, wait_healthy, find_free_port,
)

_BACKEND = Path(__file__).resolve().parent
_HELLO_DIR = _BACKEND / "modules" / "hello"


def _write_module(tmp: Path, prefix: str, router_src: str) -> Path:
    """Crée un module jetable (router.py + manifest.json) dans tmp."""
    (tmp / "router.py").write_text(router_src, encoding="utf-8")
    (tmp / "manifest.json").write_text(json.dumps({
        "id": tmp.name, "version": "1.0.0", "nom": tmp.name, "icon": "Box",
        "description": "probe test", "frontend": {"component": "Component"},
        "backend": {"prefix": prefix}, "core_module": False,
        "origin": "workshop", "status": "active", "removable": True,
    }), encoding="utf-8")
    return tmp


class ModuleIsolationTest(unittest.TestCase):
    def setUp(self):
        self._procs: list[subprocess.Popen] = []
        self._tmps: list[tempfile.TemporaryDirectory] = []

    def tearDown(self):
        for p in self._procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        for t in self._tmps:
            t.cleanup()

    def _tmpdir(self) -> Path:
        t = tempfile.TemporaryDirectory(prefix="epure-iso-")
        self._tmps.append(t)
        return Path(t.name)

    def _start(self, module_dir: Path, *, parent_env=None, capture=False):
        """Lance un worker, renvoie (proc, port, key). capture=True → stderr en PIPE."""
        port = find_free_port()
        key = _secrets.token_urlsafe(24)
        kw = {}
        if capture:
            kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
        else:
            kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        proc = spawn_worker(module_dir, port, key, parent_env=parent_env, **kw)
        self._procs.append(proc)
        return proc, port, key

    def _get(self, port: int, key: str, path: str) -> httpx.Response:
        return httpx.get(f"http://127.0.0.1:{port}{path}",
                         headers={WORKER_KEY_HEADER: key}, timeout=5.0)

    # ── 1. Cas canonique : hello ──────────────────────────────────────────────
    def test_hello_via_worker(self):
        proc, port, key = self._start(_HELLO_DIR)
        self.assertTrue(wait_healthy(port, key, proc), "le worker hello n'a pas démarré")
        # health
        h = self._get(port, key, HEALTH_PATH)
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.json()["module"], "hello")
        # route réelle du module
        r = self._get(port, key, "/hello/ping")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["message"], "pong")

    def test_worker_key_required(self):
        """Sans la clé worker, le worker refuse (401) — défense loopback."""
        proc, port, key = self._start(_HELLO_DIR)
        self.assertTrue(wait_healthy(port, key, proc))
        r = httpx.get(f"http://127.0.0.1:{port}/hello/ping", timeout=5.0)  # pas de clé
        self.assertEqual(r.status_code, 401)

    # ── 2. Étanchéité mémoire ─────────────────────────────────────────────────
    def test_env_and_core_import_tightness(self):
        probe_src = (
            "import os\n"
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/env')\n"
            "def env():\n"
            "    return {'keys': sorted(os.environ.keys()), 'values': list(os.environ.values())}\n"
            "@router.get('/import-core')\n"
            "def import_core():\n"
            "    try:\n"
            "        import core.instance  # doit lever ImportError\n"
            "        return {'import_ok': True, 'error': None}\n"
            "    except ImportError as e:\n"
            "        return {'import_ok': False, 'error': str(e)}\n"
        )
        probe = _write_module(self._tmpdir(), "/probe", probe_src)

        # Parent avec un secret factice + le vrai token d'instance : on prouve
        # que NI l'un NI l'autre n'atteint le worker.
        from core.auth import get_api_token
        api_token = get_api_token()
        parent = dict(os.environ)
        parent["GEMINI_API_KEY"] = "sk-parent-secret-should-not-leak"
        parent["ANTHROPIC_API_KEY"] = "sk-ant-should-not-leak"

        proc, port, key = self._start(probe, parent_env=parent)
        self.assertTrue(wait_healthy(port, key, proc), "worker probe non démarré")

        env = self._get(port, key, "/probe/env").json()
        keys, values = set(env["keys"]), env["values"]
        # Aucune clé API (ni par nom, ni par valeur).
        self.assertNotIn("GEMINI_API_KEY", keys)
        self.assertNotIn("ANTHROPIC_API_KEY", keys)
        joined = "\n".join(values)
        self.assertNotIn("sk-parent-secret-should-not-leak", joined)
        self.assertNotIn("sk-ant-should-not-leak", joined)
        # Le token d'instance n'est ni en clé ni en valeur.
        self.assertNotIn(api_token, joined)
        self.assertFalse(any("TOKEN" in k.upper() or "SECRET" in k.upper()
                             or "_API_KEY" in k.upper() for k in keys),
                         f"variable sensible fuitée : {keys}")

        # import core.instance depuis le router isolé → ImportError.
        imp = self._get(port, key, "/probe/import-core").json()
        self.assertFalse(imp["import_ok"], "core.instance ne devrait PAS être importable")
        self.assertIn("core.instance", imp["error"])

    def test_env_contains_only_allowlist_plus_contract(self):
        """L'env du worker se limite à l'allowlist + variables de contrat worker."""
        env = build_worker_env({"PATH": "/x", "GEMINI_API_KEY": "sk", "PYTHONPATH": "/backend",
                                "USERPROFILE": "C:/Users/x", "OLLAMA_HOST": "0.0.0.0"},
                               worker_key="k", capabilities_url="http://127.0.0.1:8000")
        self.assertIn("PATH", env)
        self.assertEqual(env["EPURE_WORKER_KEY"], "k")
        self.assertEqual(env["EPURE_CAPABILITIES_URL"], "http://127.0.0.1:8000")
        for banned in ("GEMINI_API_KEY", "PYTHONPATH", "USERPROFILE", "OLLAMA_HOST"):
            self.assertNotIn(banned, env, f"{banned} ne doit pas être transmis")

    # ── 3. Échec bruyant à l'import (core.* interdit au niveau module) ────────
    def test_toplevel_forbidden_import_fails_loudly(self):
        bad = _write_module(
            self._tmpdir(), "/bad",
            "from core.memory import MemoryEngine  # interdit au niveau module\n"
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n",
        )
        proc, port, key = self._start(bad, capture=True)
        self.assertFalse(wait_healthy(port, key, proc, timeout=10.0),
                         "le worker n'aurait PAS dû démarrer (import core.memory interdit)")
        _, stderr = proc.communicate(timeout=10)
        self.assertIn("ImportError", stderr)
        self.assertIn("core.memory", stderr)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
