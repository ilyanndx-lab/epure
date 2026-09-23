#!/usr/bin/env python3
"""Les modules se chargent au démarrage, et nulle part ailleurs — 2026-09-23.

Le démontage à chaud filtrait ``app.router.routes`` : un interne de fastapi,
qui a changé en 0.137 (``include_router`` n'y range plus qu'une entrée
``_IncludedRouter``) et gelait la dépendance en 0.136.3
(``docs/limite-demontage.md``). Il est remplacé par l'option D de
``docs/demontage-option-d.md`` : un changement de module prend effet au
redémarrage du backend, que l'interface demande au tray par une sentinelle.

Ce fichier verrouille, dans cet ordre d'importance :

  1. **Aucun code ne réécrit les routes d'une app** ni ne touche aux internes de
     routage de fastapi/starlette, et ``include_router`` ne sert qu'au montage
     de démarrage (et aux apps jetables isolées). Lecture statique (AST) — le
     jour où quelqu'un réintroduit un démontage « juste pour ce cas », c'est ici
     que ça casse, pas en production sur une version de fastapi.
  2. **Le flux complet** : approuver → rien n'est monté dans l'app en cours →
     ``GET /instance/redemarrage`` dit qu'il faut redémarrer → la sentinelle est
     écrite (ou « manuel » sans tray) → au démarrage suivant, le module sert.
  3. **« Redémarrage requis » est un écart calculé**, pas un drapeau : il suit
     le disque (désactivation, modification, suppression) et s'efface quand
     l'app a été construite sur l'état courant.
  4. **Un module cassé ne bloque pas le démarrage**, ``SystemExit`` compris —
     mais ``KeyboardInterrupt`` n'est pas avalé.
  5. **Le smoke test de l'Atelier n'est pas vert par vacuité** : il lit les
     routes du router du module, et zéro route est un échec.

SIMULER UN REDÉMARRAGE DANS LE PROCESS DE TEST. Un vrai redémarrage est un
processus neuf : ``sys.modules`` vide, app neuve, ``register_routers`` une fois.
``_demarrer`` reproduit exactement ça pour les ids concernés — purge de leurs
entrées ``sys.modules``, ``FastAPI()`` neuve, ``register_routers``. Sans la
purge, ``import_module`` rendrait l'ancien objet et le test prouverait le
contraire de ce qu'il affirme. Le vrai processus est éprouvé par
``integration_redemarrage.py`` (job manuel).

Usage :
    python test_redemarrage_modules.py
"""

import ast
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401,E402  — isole données et arbre de modules AVANT core.*

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core import module_registry, module_workshop, redemarrage  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.paths import resolve_generated_dir, resolve_modules_dir  # noqa: E402

_BACKEND = Path(__file__).resolve().parent
_REPO = _BACKEND.parent


def _client() -> TestClient:
    return TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 54321))


def _auth() -> dict:
    return {"Authorization": f"Bearer {get_api_token()}"}


def _demarrer(*ids: str) -> FastAPI:
    """Ce que fait un processus neuf, pour les modules ``ids`` (cf. l'en-tête)."""
    for mid in ids:
        for nom in [n for n in list(sys.modules)
                    if n == f"modules.{mid}" or n.startswith(f"modules.{mid}.")]:
            del sys.modules[nom]
    importlib.invalidate_caches()
    app = FastAPI()
    module_registry.register_routers(app)
    return app


def _router_py(mid: str, reponse: str) -> str:
    return ("from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
            f"@router.get('/{mid}/ping')\n"
            f"async def ping():\n    return {{'reponse': '{reponse}'}}\n")


def _manifeste(mid: str) -> dict:
    return {"id": mid, "version": "1.0.0", "nom": mid.capitalize(), "icon": "Box",
            "description": f"module {mid}", "frontend": {"component": "Component"},
            "backend": {"prefix": ""}, "core_module": False, "origin": "workshop",
            "status": "active", "removable": True}


class _BaseModule(unittest.TestCase):
    """Pose/retire un module jetable dans l'arbre temporaire de _test_env."""

    ID = "zz_redemarrage"

    def setUp(self):
        self.addCleanup(self._retirer)

    def _poser(self, reponse="A", router_src=None):
        d = resolve_modules_dir() / self.ID
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(json.dumps(_manifeste(self.ID)), encoding="utf-8")
        (d / "router.py").write_text(router_src or _router_py(self.ID, reponse), encoding="utf-8")
        module_registry.set_status(self.ID, "active")
        return d

    def _retirer(self):
        try:
            module_registry.set_status(self.ID, "disabled")
        except Exception:
            pass
        shutil.rmtree(resolve_modules_dir() / self.ID, ignore_errors=True)
        shutil.rmtree(resolve_generated_dir() / self.ID, ignore_errors=True)
        shutil.rmtree(module_workshop._staging_dir(self.ID), ignore_errors=True)
        for nom in [n for n in list(sys.modules) if n.startswith(f"modules.{self.ID}")]:
            del sys.modules[nom]


# ── 1. Aucun démontage, aucun interne de routage ────────────────────────────

#: Fichiers où `include_router` est légitime : le montage de DÉMARRAGE, et des
#: apps jetables dans un processus isolé (smoke test, worker, intégration).
_INCLUDE_ROUTER_AUTORISE = {
    "backend/core/module_registry.py",
    "backend/core/smoke_runner.py",
    "backend/core/module_worker.py",
    "backend/integration_modules_mount.py",
}

#: Noms d'internes de routage fastapi/starlette : les manipuler, c'est parier
#: sur une disposition que la prochaine version peut changer (0.137 l'a fait).
_INTERNES = {"_IncludedRouter", "_mark_routes_changed", "_get_routes_version",
             "original_router", "effective_candidates", "_effective_candidates"}

_MUTATEURS = {"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse"}


def _fichiers_python() -> list[Path]:
    exclus = {"__pycache__", "_staging", "_backups", "node_modules", ".venv", "_atelier"}
    fichiers = [_REPO / "epure_tray.py", _REPO / "lanceur.py"]
    for racine in (_BACKEND, _REPO / "tools", _REPO / "modules-catalogue"):
        for f in racine.rglob("*.py"):
            if not exclus & set(f.relative_to(_REPO).parts):
                fichiers.append(f)
    return fichiers


def _est_routes(noeud) -> bool:
    """``x.routes`` ou ``x.routes[...]``."""
    if isinstance(noeud, ast.Subscript):
        noeud = noeud.value
    return isinstance(noeud, ast.Attribute) and noeud.attr == "routes"


def _violations(source: str, rel: str) -> list[str]:
    out = []
    arbre = ast.parse(source)
    for n in ast.walk(arbre):
        cibles = []
        if isinstance(n, (ast.Assign, ast.Delete)):
            cibles = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            cibles = [n.target]
        for c in cibles:
            if _est_routes(c):
                out.append(f"{rel}:{n.lineno} écrit dans `.routes`")
            if isinstance(c, ast.Attribute) and c.attr == "openapi_schema":
                out.append(f"{rel}:{n.lineno} invalide `openapi_schema` (reste de montage à chaud)")
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in _MUTATEURS and _est_routes(n.func.value)):
            out.append(f"{rel}:{n.lineno} `.routes.{n.func.attr}()`")
        noms = ([n.attr] if isinstance(n, ast.Attribute) else [n.id] if isinstance(n, ast.Name)
                else [a.name for a in n.names] if isinstance(n, (ast.Import, ast.ImportFrom)) else [])
        for nom in set(noms) & _INTERNES:
            out.append(f"{rel}:{n.lineno} interne de routage `{nom}`")
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "include_router"
                and rel not in _INCLUDE_ROUTER_AUTORISE
                and not Path(rel).name.startswith("test_")):
            out.append(f"{rel}:{n.lineno} `include_router` hors du montage de démarrage")
    return out


class PasDeDemontageTest(unittest.TestCase):

    def test_le_detecteur_attrape_ce_qu_il_doit(self):
        """Sans ce cas de contrôle, un détecteur cassé serait vert par vacuité."""
        for code in ("app.router.routes[:] = []", "app.router.routes = x",
                     "del app.router.routes[0]", "app.router.routes.remove(r)",
                     "app.openapi_schema = None", "from fastapi.routing import _IncludedRouter",
                     "app.router._mark_routes_changed()", "app.include_router(r)"):
            with self.subTest(code=code):
                self.assertTrue(_violations(code, "backend/core/x.py"), code)
        # Lire n'est pas écrire : le smoke test parcourt `router.routes`.
        self.assertEqual(_violations("for r in router.routes: pass", "backend/core/x.py"), [])

    def test_aucun_code_ne_reecrit_les_routes(self):
        fichiers = _fichiers_python()
        self.assertGreater(len(fichiers), 50, "balayage suspect : trop peu de fichiers")
        trouvees = []
        for f in fichiers:
            rel = f.relative_to(_REPO).as_posix()
            if rel == "backend/test_redemarrage_modules.py":
                continue  # ses propres exemples de contrôle
            trouvees += _violations(f.read_text(encoding="utf-8-sig"), rel)
        self.assertEqual(
            trouvees, [],
            "démontage/montage à chaud ou interne de routage réintroduit — les modules "
            "ne se chargent qu'au démarrage (docs/demontage-option-d.md)",
        )

    def test_register_routers_n_est_appele_qu_au_demarrage(self):
        """Au niveau MODULE de main.py, jamais depuis une fonction ou une route."""
        appels = []
        for f in _fichiers_python():
            rel = f.relative_to(_REPO).as_posix()
            if Path(rel).name.startswith("test_") or rel.startswith("backend/integration_"):
                continue
            arbre = ast.parse(f.read_text(encoding="utf-8-sig"))
            niveau_module = {id(s.value) for s in arbre.body if isinstance(s, ast.Expr)}
            for n in ast.walk(arbre):
                if not isinstance(n, ast.Call):
                    continue
                nom = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                if nom in ("register_routers", "_register_routers"):
                    appels.append((rel, n.lineno, id(n) in niveau_module))
        self.assertEqual([(r, top) for r, _, top in appels], [("backend/main.py", True)], appels)


# ── 2. Le flux : approuver → redémarrage demandé → chargé au démarrage ──────

class FluxApprobationTest(_BaseModule):
    ID = "zz_approuve"

    def _stager(self, reponse):
        kind = "edit" if module_workshop.module_exists(self.ID) else "new"
        module_workshop.prepare(self.ID, kind, "ollama", "normal")
        s = module_workshop._staging_dir(self.ID)
        (s / "manifest.json").write_text(json.dumps(_manifeste(self.ID)), encoding="utf-8")
        (s / "router.py").write_text(_router_py(self.ID, reponse), encoding="utf-8")
        (s / "Component.tsx").write_text(
            "export default function Component() { return null }\n", encoding="utf-8")

    def test_approuver_puis_redemarrer(self):
        client, auth = _client(), _auth()
        self._stager("A")

        r = client.post(f"/workshop/{self.ID}/approve?force=true", headers=auth)
        self.assertEqual(r.status_code, 200, r.text)
        corps = r.json()
        self.assertNotIn("remounted", corps)

        # Rien n'est monté dans l'app en cours…
        self.assertEqual(client.get(f"/{self.ID}/ping", headers=auth).status_code, 404)
        # …et c'est dit, dans la réponse comme dans l'état.
        self.assertTrue(corps["redémarrage"]["requis"])
        etat = client.get("/instance/redemarrage", headers=auth).json()
        self.assertIn({"id": self.ID, "changement": "à charger"}, etat["écarts"])

        # Sans tray : « manuel », jamais d'erreur.
        with mock.patch.dict(os.environ, {redemarrage.ENV_SENTINELLE: ""}):
            r = client.post("/instance/redemarrage", headers=auth, json={"raison": "test"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["déclenché"], False)
        self.assertEqual(r.json()["automatique"], False)
        self.assertIn("manuel", r.json()["message"])

        # Avec tray : la sentinelle est écrite là où il l'attend.
        tmp = Path(tempfile.mkdtemp(prefix="epure-sentinelle-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sentinelle = tmp / ".epure-redemarrage"
        with mock.patch.dict(os.environ, {redemarrage.ENV_SENTINELLE: str(sentinelle)}):
            self.assertTrue(client.get("/instance/redemarrage", headers=auth).json()["automatique"])
            r = client.post("/instance/redemarrage", headers=auth, json={"raison": "approbation"})
        self.assertEqual(r.json()["déclenché"], True)
        self.assertTrue(sentinelle.is_file())
        self.assertEqual(json.loads(sentinelle.read_text(encoding="utf-8"))["raison"], "approbation")

        # Démarrage suivant : le module sert, et plus rien n'est requis.
        neuve = _demarrer(self.ID)
        with TestClient(neuve) as c:
            r = c.get(f"/{self.ID}/ping")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"reponse": "A"})
        self.assertFalse(module_registry.ecart_redemarrage(neuve)["requis"])

    def test_reapprobation_sert_la_nouvelle_version_apres_redemarrage(self):
        """Le cas que `limite-demontage.md` §9 déduisait sans l'avoir mesuré.

        « A » puis « B-2 » et non « A » puis « B » : deux router.py de MÊME
        taille écrits dans la même seconde sont indiscernables pour le cache
        de bytecode (``__pycache__`` valide par mtime + taille), et le test
        mesurerait ce cache au lieu du redémarrage. Hors test, une
        réapprobation ne tombe pas dans la même seconde que la précédente.
        """
        self._stager("A")
        module_workshop.approve(self.ID, force=True)
        premiere = _demarrer(self.ID)

        self._stager("B-2")
        module_workshop.approve(self.ID, force=True)
        self.assertIn({"id": self.ID, "changement": "modifié"},
                      module_registry.ecart_redemarrage(premiere)["écarts"])
        with TestClient(premiere) as c:  # l'ancien code sert jusqu'au redémarrage
            self.assertEqual(c.get(f"/{self.ID}/ping").json(), {"reponse": "A"})

        with TestClient(_demarrer(self.ID)) as c:
            self.assertEqual(c.get(f"/{self.ID}/ping").json(), {"reponse": "B-2"})

    def test_health_porte_le_boot_id(self):
        r = _client().get("/health")
        self.assertEqual(r.json()["boot_id"], redemarrage.BOOT_ID)

    def test_la_route_exige_le_token(self):
        c = _client()
        self.assertEqual(c.get("/instance/redemarrage").status_code, 401)
        self.assertEqual(c.post("/instance/redemarrage").status_code, 401)

    def test_ecriture_impossible_ne_leve_pas(self):
        tmp = Path(tempfile.mkdtemp(prefix="epure-sentinelle-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.dict(os.environ, {redemarrage.ENV_SENTINELLE: str(tmp / "absent" / "x")}), \
                mock.patch.object(redemarrage, "write_json", side_effect=PermissionError("refusé")):
            res = redemarrage.demander("test")
        self.assertFalse(res["déclenché"])
        self.assertIn("message", res)


# ── 3. L'écart suit le disque ───────────────────────────────────────────────

class EcartCalculeTest(_BaseModule):

    def test_desactiver_modifier_supprimer(self):
        self._poser("A")
        app = _demarrer(self.ID)
        self.assertEqual(module_registry.ecart_redemarrage(app)["écarts"], [])

        # Modifié hors de l'Atelier (fichier réécrit à la main).
        (resolve_modules_dir() / self.ID / "router.py").write_text(
            _router_py(self.ID, "B"), encoding="utf-8")
        self.assertEqual(module_registry.ecart_redemarrage(app)["écarts"],
                         [{"id": self.ID, "changement": "modifié"}])

        module_registry.set_status(self.ID, "disabled")
        self.assertEqual(module_registry.ecart_redemarrage(app)["écarts"],
                         [{"id": self.ID, "changement": "à décharger"}])

        # Réactivé ET remis à l'identique : l'écart disparaît sans redémarrer —
        # c'est ce qu'un drapeau posé ne saurait pas faire.
        module_registry.set_status(self.ID, "active")
        (resolve_modules_dir() / self.ID / "router.py").write_text(
            _router_py(self.ID, "A"), encoding="utf-8")
        self.assertFalse(module_registry.ecart_redemarrage(app)["requis"])

    def test_app_jamais_demarree_aucun_ecart(self):
        self.assertFalse(module_registry.ecart_redemarrage(FastAPI())["requis"])


# ── 4. Un module cassé ne bloque pas le démarrage ───────────────────────────

class ModuleCasseTest(_BaseModule):
    ID = "zz_casse"

    def test_systemexit_a_l_import_n_arrete_pas_le_demarrage(self):
        self._poser(router_src="import sys\nsys.exit(3)\n")
        app = _demarrer(self.ID)  # ne doit pas lever
        ecart = module_registry.ecart_redemarrage(app)
        self.assertIn(self.ID, ecart["échecs"])
        self.assertIn("SystemExit", ecart["échecs"][self.ID])
        # Un échec inchangé n'est pas un écart : redémarrer rejouerait l'échec.
        self.assertFalse(ecart["requis"])
        # Les autres modules sont montés quand même.
        with TestClient(app) as c:
            self.assertEqual(c.get("/hello/ping").status_code, 200)

    def test_keyboardinterrupt_n_est_pas_avale(self):
        self._poser(router_src="raise KeyboardInterrupt\n")
        with self.assertRaises(KeyboardInterrupt):
            _demarrer(self.ID)

    def test_exception_a_l_import_isolee(self):
        self._poser(router_src="raise RuntimeError('cassé')\n")
        app = _demarrer(self.ID)
        self.assertIn("RuntimeError", module_registry.ecart_redemarrage(app)["échecs"][self.ID])


# ── 5. Smoke test de l'Atelier : pas de vert par vacuité ────────────────────

class SmokeNonVacuiteTest(unittest.TestCase):

    def _lancer(self, router_src: str) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="epure-smoke-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "router.py").write_text(router_src, encoding="utf-8")
        (tmp / "manifest.json").write_text(json.dumps(_manifeste("zz_smoke")), encoding="utf-8")
        p = subprocess.run(
            [sys.executable, str(_BACKEND / "core" / "smoke_runner.py"), str(tmp), "zz_smoke"],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_les_routes_du_module_sont_appelees(self):
        res = self._lancer(_router_py("zz_smoke", "ok"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["tested"], ["GET /zz_smoke/ping → 200"])

    def test_zero_route_est_un_echec(self):
        res = self._lancer("from fastapi import APIRouter\nrouter = APIRouter()\n")
        self.assertFalse(res["ok"], res)
        self.assertIn("aucune route", res["error"])


# ── Tray ↔ backend : un seul nom de variable ────────────────────────────────

class CanalTrayTest(unittest.TestCase):

    def test_meme_variable_des_deux_cotes(self):
        sys.path.insert(0, str(_REPO))
        import lanceur
        self.assertEqual(lanceur.ENV_SENTINELLE, redemarrage.ENV_SENTINELLE)

    def test_consommer_sentinelle(self):
        sys.path.insert(0, str(_REPO))
        import lanceur
        tmp = Path(tempfile.mkdtemp(prefix="epure-sentinelle-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        s = lanceur.sentinelle_redemarrage(tmp)
        self.assertIsNone(lanceur.consommer_sentinelle(s))
        # Tel que core.jsonstore l'écrit : indenté, sur plusieurs lignes.
        s.write_text(json.dumps({"raison": "approbation", "boot_id": "b1"}, indent=2),
                     encoding="utf-8")
        self.assertEqual(lanceur.consommer_sentinelle(s), "approbation")
        self.assertFalse(s.exists(), "la sentinelle doit être effacée AVANT le redémarrage")
        self.assertIsNone(lanceur.consommer_sentinelle(s))
        s.write_text("pas du json", encoding="utf-8")  # contenu brut en repli
        self.assertEqual(lanceur.consommer_sentinelle(s), "pas du json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
