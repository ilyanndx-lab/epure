#!/usr/bin/env python3
"""Tests du gate de sécurité des routers générés (core/module_validate.py).

Écrits AVANT le durcissement du validateur (TDD) : les cas de contournement
ci-dessous passaient tous la validation d'origine — aucun ne doit survivre.

Couvre :
  1. from os import system ; system(...)          (import ciblé)
  2. import os as o ; o.system(...)               (alias de module)
  3. getattr(os, "system")(...)                   (résolution dynamique)
  4. import urllib.request / requests / httpx...  (réseau hors core.runtime)
  5. os.environ["DEEPSEEK" + "_API" + "_KEY"]     (secret par concaténation)
  + préfixe de routes : chaque route doit commencer par /<id> quand
    backend.prefix est vide (sinon un module généré masque une route core)
  + non-régression : un router légitime (style hello) reste accepté, et les
    motifs utilisés par les modules core (chat: urllib ; settings: getattr,
    environ à clé variable) restent tolérés en mode core (is_core=True).

Usage :
    python test_module_validate.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

from core.module_validate import validate_router_py

# Prélude minimal valide : isole le verdict sur le fragment testé.
_PRELUDE = "from fastapi import APIRouter\nrouter = APIRouter()\n"


def _check(fragment: str, **kwargs):
    return validate_router_py(_PRELUDE + fragment, **kwargs)


class BypassTest(unittest.TestCase):
    """Contournements connus : chacun doit être rejeté (report.ok == False)."""

    def assertRejected(self, fragment: str, **kwargs):
        rep = _check(fragment, **kwargs)
        self.assertFalse(
            rep.ok,
            f"Aurait dû être rejeté :\n{fragment}\n(aucune erreur relevée)",
        )

    def assertAccepted(self, fragment: str, **kwargs):
        rep = _check(fragment, **kwargs)
        self.assertTrue(rep.ok, f"Aurait dû passer :\n{fragment}\n{rep.errors}")

    # 1 — import ciblé d'une fonction dangereuse de os
    def test_from_os_import_system(self):
        self.assertRejected('from os import system\nsystem("echo pwned")\n')

    def test_from_os_import_system_aliased(self):
        self.assertRejected('from os import system as run\nrun("echo pwned")\n')

    def test_from_os_import_popen(self):
        self.assertRejected('from os import popen\npopen("id")\n')

    # 2 — alias de module
    def test_import_os_as_alias_system(self):
        self.assertRejected('import os as o\no.system("echo pwned")\n')

    def test_import_subprocess_as_alias(self):
        # déjà refusé à l'import, mais l'appel via alias ne doit pas survivre non plus
        self.assertRejected('import subprocess as sp\nsp.run(["id"])\n')

    # 3 — getattr sur un module
    def test_getattr_os_system(self):
        self.assertRejected('import os\ngetattr(os, "system")("echo pwned")\n')

    def test_getattr_os_concat(self):
        self.assertRejected('import os\ngetattr(os, "sys" + "tem")("echo pwned")\n')

    def test_getattr_os_via_alias(self):
        self.assertRejected('import os as o\ngetattr(o, "system")("echo pwned")\n')

    # 4 — réseau direct (doit passer par core.runtime)
    def test_import_urllib_request(self):
        self.assertRejected("import urllib.request\n")

    def test_from_urllib_import(self):
        self.assertRejected("from urllib.request import urlopen\n")

    def test_import_http_client(self):
        self.assertRejected("import http.client\n")

    def test_import_requests(self):
        self.assertRejected("import requests\n")

    def test_import_httpx(self):
        self.assertRejected("import httpx\n")

    def test_import_aiohttp(self):
        self.assertRejected("import aiohttp\n")

    # 5 — secrets par clé non littérale
    def test_environ_concat_key(self):
        self.assertRejected(
            'import os\nk = os.environ["DEEPSEEK" + "_API" + "_KEY"]\n'
        )

    def test_environ_get_concat_key(self):
        self.assertRejected('import os\nk = os.environ.get("GROQ" + "_API_KEY")\n')

    def test_environ_variable_key(self):
        self.assertRejected('import os\nname = "X"\nk = os.environ[name]\n')

    def test_getenv_non_constant(self):
        self.assertRejected('import os\nname = "X"\nk = os.getenv(name)\n')

    def test_environ_via_from_import(self):
        self.assertRejected('from os import environ\nk = environ["A" + "B"]\n')


class RoutePrefixTest(unittest.TestCase):
    """backend.prefix vide → toute route doit être préfixée par /<id>."""

    def test_unprefixed_route_rejected(self):
        rep = _check(
            '@router.get("/models")\nasync def masque():\n    return {}\n',
            module_id="demo", backend_prefix="",
        )
        self.assertFalse(rep.ok, f"route /models non préfixée acceptée : {rep.errors}")

    def test_prefixed_route_accepted(self):
        rep = _check(
            '@router.get("/demo/items")\nasync def items():\n    return {}\n'
            '@router.post("/demo")\nasync def create():\n    return {}\n',
            module_id="demo", backend_prefix="",
        )
        self.assertTrue(rep.ok, rep.errors)

    def test_websocket_route_checked(self):
        rep = _check(
            '@router.websocket("/ws/chat")\nasync def ws(ws):\n    pass\n',
            module_id="demo", backend_prefix="",
        )
        self.assertFalse(rep.ok, "websocket non préfixé accepté")

    def test_nonempty_prefix_skips_check(self):
        # prefix non vide : le mount préfixe déjà, chemins relatifs légitimes
        rep = _check(
            '@router.get("/ping")\nasync def ping():\n    return {}\n',
            module_id="demo", backend_prefix="/demo",
        )
        self.assertTrue(rep.ok, rep.errors)

    def test_dynamic_route_path_rejected(self):
        rep = _check(
            'P = "/demo"\n@router.get(P + "/x")\nasync def x():\n    return {}\n',
            module_id="demo", backend_prefix="",
        )
        self.assertFalse(rep.ok, "chemin de route non littéral accepté")


class LegitimateTest(unittest.TestCase):
    """Non-régression : le style de module documenté reste accepté."""

    def test_hello_style_router(self):
        src = (
            "from fastapi import APIRouter\n"
            "from pydantic import BaseModel\n"
            "from core.runtime import llm\n"
            "router = APIRouter()\n"
            '@router.get("/demo/ping")\n'
            "async def ping():\n"
            '    return {"pong": True}\n'
        )
        rep = validate_router_py(src, module_id="demo", backend_prefix="")
        self.assertTrue(rep.ok, rep.errors)

    def test_safe_constant_env_key(self):
        # constante littérale NON sensible : toléré
        rep = _check('import os\nv = os.environ.get("EPURE_LOG_LEVEL", "")\n')
        self.assertTrue(rep.ok, rep.errors)

    def test_getattr_on_object_constant_attr(self):
        # getattr sur un objet quelconque avec attribut littéral : toléré
        rep = _check('class A: pass\nv = getattr(A(), "x", None)\n')
        self.assertTrue(rep.ok, rep.errors)

    def test_core_mode_tolerates_urllib_and_dynamic(self):
        # Mode core (réédition atelier de chat/settings) : urllib, getattr à
        # attribut variable et environ à clé variable restent tolérés…
        src = (
            "import urllib.request\n"
            "import os\n"
            "def f(req, k, key_name):\n"
            "    v = getattr(req, k, None)\n"
            '    return os.environ.get(key_name, "")\n'
        )
        rep = _check(src, is_core=True)
        self.assertTrue(rep.ok, rep.errors)

    def test_core_mode_still_blocks_os_system(self):
        # … mais les contournements d'exécution restent bloqués même en core.
        rep = _check('import os as o\no.system("id")\n', is_core=True)
        self.assertFalse(rep.ok)
        rep = _check('import os\ngetattr(os, "system")("id")\n', is_core=True)
        self.assertFalse(rep.ok)


if __name__ == "__main__":
    unittest.main()
