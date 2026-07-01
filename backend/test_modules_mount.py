#!/usr/bin/env python3
"""Smoke test : tous les modules actifs s'importent et se montent sur FastAPI.

register_routers() avale les erreurs (log + continue) pour ne pas empêcher le
démarrage ; ce test refait le montage en ÉCHOUANT au premier module cassé, et
signale en prime les collisions de chemins entre modules montés à la racine
(backend.prefix == "").

Charge core.runtime (moteurs partagés) — lent au premier import.

Usage :
    python test_modules_mount.py
"""

import os
import sys
import unittest

os.environ.setdefault("HF_HUB_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class ModulesMountTest(unittest.TestCase):
    def test_active_modules_import_and_mount(self):
        import importlib
        from fastapi import FastAPI
        from core.module_registry import list_modules, _MODULES_DIR

        app = FastAPI()
        mounted, failures = [], []
        # (méthode, chemin) → module qui l'a enregistré : une collision INTER-module
        # = un module qui masque la route d'un autre (le vrai risque). Deux méthodes
        # sur le même chemin dans un même module (GET+DELETE /x) sont légitimes.
        owner: dict[tuple, str] = {}
        collisions = []

        def _route_keys(route) -> list[tuple]:
            path = getattr(route, "path", None)
            if not path:
                return []
            methods = getattr(route, "methods", None)
            return [(mth, path) for mth in methods] if methods else [("WS", path)]

        for m in list_modules():
            if m.get("status") != "active":
                continue
            mid = str(m.get("id"))
            if not (_MODULES_DIR / mid / "router.py").is_file():
                continue  # core non migré (décoré sur app dans main.py)
            try:
                mod = importlib.import_module(f"modules.{mid}.router")
                router = getattr(mod, "router", None)
                self.assertIsNotNone(router, f"{mid}: router.py ne définit pas 'router'")
                prefix = (m.get("backend") or {}).get("prefix", "")
                before = set(id(r) for r in app.routes)
                app.include_router(router, prefix=prefix)
                mounted.append(mid)
                for r in app.routes:
                    if id(r) in before:
                        continue
                    for key in _route_keys(r):
                        if key in owner and owner[key] != mid:
                            collisions.append(f"{key[0]} {key[1]} ({owner[key]} vs {mid})")
                        owner.setdefault(key, mid)
            except Exception as exc:
                failures.append(f"{mid}: {type(exc).__name__}: {exc}")

        self.assertEqual(failures, [], f"Modules en échec d'import/montage : {failures}")
        self.assertGreater(len(mounted), 0, "Aucun module monté — configuration suspecte")
        self.assertEqual(collisions, [], f"Collisions inter-modules (masquage de route) : {collisions}")
        print(f"\n{len(mounted)} module(s) monté(s) sans erreur : {', '.join(sorted(mounted))}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
