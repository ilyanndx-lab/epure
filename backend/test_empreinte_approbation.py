#!/usr/bin/env python3
"""Un module de l'Atelier n'est chargé que dans la version approuvée.

``approve()`` enregistre l'empreinte des trois fichiers (manifest, router,
composant) ; au démarrage, un module ``origin: workshop`` dont l'empreinte a
changé depuis — ou qui n'en a jamais eu — n'est pas monté, et ``GET /modules``
le dit (``approbation``) pour que l'interface ne rende pas son composant.
Avant, tout ``router.py`` présent sur disque tournait au redémarrage, relu ou
non (``docs/etude-isolation-modules.md`` §3 d, étape 3 de la recommandation).

Verrouille :
  1. approuvé → chargé ;
  2. router OU composant modifié après approbation → non chargé, « modifié »,
     et pas réclamé par l'écart de redémarrage (le redémarrage ne le
     chargerait pas) ;
  3. ré-approbation → chargé ;
  4. jamais approuvé → non chargé, « non_approuvé » ;
  5. réécrire ``origin`` dans le manifeste ne sort pas du contrôle ;
  6. modules du cœur et du catalogue non soumis (pas de champ, chargés), et
     une approbation d'un module d'origine autre que ``workshop`` (réédition
     du cœur) n'enregistre pas d'empreinte.

Usage :
    python test_empreinte_approbation.py
"""

import importlib
import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401,E402  — isole données et arbre de modules AVANT core.*

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import module_registry, module_workshop  # noqa: E402
from core.jsonstore import read_json  # noqa: E402
from core.paths import resolve_generated_dir, resolve_modules_dir  # noqa: E402

_ID = "zz_empreinte"


def _router_py(reponse: str) -> str:
    return ("from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
            f"@router.get('/{_ID}/ping')\n"
            f"async def ping():\n    return {{'reponse': '{reponse}'}}\n")


def _manifeste(origin: str = "workshop") -> dict:
    return {"id": _ID, "version": "1.0.0", "nom": "Empreinte", "icon": "Box",
            "description": "test", "frontend": {"component": "Component"},
            "backend": {"prefix": ""}, "core_module": False, "origin": origin,
            "status": "active", "removable": True}


def _demarrer() -> FastAPI:
    """Ce que fait un processus neuf (cf. test_redemarrage_modules._demarrer)."""
    for nom in [n for n in list(sys.modules) if n.startswith(f"modules.{_ID}")]:
        del sys.modules[nom]
    importlib.invalidate_caches()
    app = FastAPI()
    module_registry.register_routers(app)
    return app


def _ping(app: FastAPI) -> int:
    with TestClient(app) as c:
        return c.get(f"/{_ID}/ping").status_code


class EmpreinteApprobationTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._retirer)

    def _retirer(self):
        module_registry.set_status(_ID, "disabled")
        module_registry.oublier_approbation(_ID)
        shutil.rmtree(resolve_modules_dir() / _ID, ignore_errors=True)
        shutil.rmtree(resolve_generated_dir() / _ID, ignore_errors=True)
        shutil.rmtree(module_workshop._staging_dir(_ID), ignore_errors=True)
        for nom in [n for n in list(sys.modules) if n.startswith(f"modules.{_ID}")]:
            del sys.modules[nom]

    def _approuver(self, reponse: str) -> None:
        kind = "edit" if module_workshop.module_exists(_ID) else "new"
        module_workshop.prepare(_ID, kind, "ollama", "normal")
        s = module_workshop._staging_dir(_ID)
        (s / "manifest.json").write_text(json.dumps(_manifeste()), encoding="utf-8")
        (s / "router.py").write_text(_router_py(reponse), encoding="utf-8")
        (s / "Component.tsx").write_text(
            "export default function Component() { return null }\n", encoding="utf-8")
        self.assertTrue(module_workshop.approve(_ID)["ok"])

    def _etat(self):
        return (module_registry.get_module(_ID) or {}).get("approbation")

    def test_approuve_est_charge(self):
        self._approuver("A")
        self.assertEqual(self._etat(), "approuvé")
        self.assertEqual(_ping(_demarrer()), 200)

    def test_router_modifie_apres_approbation_non_charge_puis_reapprouve(self):
        self._approuver("A")
        app = _demarrer()
        (resolve_modules_dir() / _ID / "router.py").write_text(_router_py("B"), encoding="utf-8")

        self.assertEqual(self._etat(), "modifié")
        self.assertNotIn(_ID, module_registry.modules_a_charger())
        self.assertEqual(module_registry.ecart_redemarrage(app)["écarts"],
                         [{"id": _ID, "changement": "à décharger"}])
        self.assertEqual(_ping(_demarrer()), 404)

        # « B-2 » et non « B » : même piège de cache de bytecode que
        # test_redemarrage_modules (même taille, même seconde que « A »).
        self._approuver("B-2")
        self.assertEqual(self._etat(), "approuvé")
        with TestClient(_demarrer()) as c:
            self.assertEqual(c.get(f"/{_ID}/ping").json(), {"reponse": "B-2"})

    def test_composant_modifie_apres_approbation_non_charge(self):
        self._approuver("A")
        comp = resolve_generated_dir() / _ID / "Component.tsx"
        comp.write_text(comp.read_text(encoding="utf-8") + "// ajout\n", encoding="utf-8")
        self.assertEqual(self._etat(), "modifié")
        self.assertEqual(_ping(_demarrer()), 404)

    def test_jamais_approuve_non_charge(self):
        d = resolve_modules_dir() / _ID
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(_manifeste()), encoding="utf-8")
        (d / "router.py").write_text(_router_py("A"), encoding="utf-8")
        module_registry.set_status(_ID, "active")
        self.assertEqual(self._etat(), "non_approuvé")
        self.assertEqual(_ping(_demarrer()), 404)

    def test_reecrire_origin_ne_sort_pas_du_controle(self):
        self._approuver("A")
        (resolve_modules_dir() / _ID / "manifest.json").write_text(
            json.dumps(_manifeste(origin="catalogue")), encoding="utf-8")
        self.assertEqual(self._etat(), "modifié")
        self.assertEqual(_ping(_demarrer()), 404)

    def test_coeur_et_catalogue_non_soumis(self):
        mods = {m["id"]: m for m in module_registry.list_modules()}
        for mid in ("hello", "settings"):
            self.assertIn(mid, mods)
            self.assertNotIn("approbation", mods[mid], mid)
        with TestClient(_demarrer()) as c:
            self.assertEqual(c.get("/hello/ping").status_code, 200)

    def test_approbation_hors_atelier_n_enregistre_pas_d_empreinte(self):
        """Un module du cœur réédité dans l'Atelier (origine ``builtin``) : pas
        d'empreinte, sinon une mise à jour du dépôt le déclarerait « modifié ».
        Module jetable plutôt que ``hello`` réel, pour ne rien réécrire de
        partagé entre les tests."""
        module_workshop.prepare(_ID, "new", "ollama", "normal")
        s = module_workshop._staging_dir(_ID)
        (s / "manifest.json").write_text(json.dumps(_manifeste(origin="builtin")), encoding="utf-8")
        (s / "router.py").write_text(_router_py("A"), encoding="utf-8")
        (s / "Component.tsx").write_text(
            "export default function Component() { return null }\n", encoding="utf-8")
        self.assertTrue(module_workshop.approve(_ID)["ok"])
        self.assertNotIn(_ID, read_json(module_registry._approbations_file(), {}))
        self.assertNotIn("approbation", module_registry.get_module(_ID))
        self.assertEqual(_ping(_demarrer()), 200)

if __name__ == "__main__":
    unittest.main()
