#!/usr/bin/env python3
"""Smoke test : tous les modules actifs s'importent et se montent sur FastAPI.

register_routers() avale les erreurs (log + continue) pour ne pas empêcher le
démarrage ; ce test refait le montage en ÉCHOUANT au premier module cassé, et
signale en prime les collisions de chemins entre modules montés à la racine
(backend.prefix == "").

Charge core.runtime (moteurs partagés) — lent au premier import.

Nommé `integration_` et NON `test_` volontairement : le job backend de la CI
tourne en `unittest discover -p 'test_*.py'`, et ce fichier n'importe PAS
`_test_env` — il monte le vrai arbre de modules et construit le vrai store
vectoriel, ce que le job backend (dépendances minimales) ne peut pas porter. Le
renommer suffit à l'exclure de la découverte, sans `skipUnless` ni variable
d'environnement à se rappeler. Il est lancé par le job `integration` (manuel,
workflow_dispatch).

Usage :
    python integration_modules_mount.py

RÉSERVE — lancé tel quel, ce script ÉCRIT DANS LES VRAIES DONNÉES de
l'instance. C'est la contrepartie directe de ce qui précède : ne pas importer
`_test_env` est ce qui lui donne le vrai arbre de modules, et c'est aussi ce
qui laisse `core.runtime` construire ses moteurs sur `backend/memory/`. Au
passage, `MemoryEngine.__init__` **réinitialise `context_session.json`** —
modèle actif, mode strict, raisonnement, tout revient au défaut — et crée
`profile.json` / `memory_sessions.json` / `instance_config.json` s'ils
manquent. Le lancer pendant une séance de travail efface les réglages de cette
séance, sans rien annoncer et sans que le test échoue pour autant : c'est un
effet de bord de l'import, pas une assertion.

Le job `integration` de la CI part d'un clone neuf, où il n'y a rien à perdre :
c'est là que ce script est prévu pour tourner, et c'est pourquoi la réserve
n'apparaissait nulle part.

Pour le lancer sur un poste de travail sans y toucher, poser `EPURE_DATA_DIR`
sur un temporaire (vérifié : les six fichiers partent là-bas et
`backend/memory/` n'est pas modifié) :

    $env:EPURE_DATA_DIR = "$env:TEMP/epure-mount"; python integration_modules_mount.py

Ça change ce qui est mesuré, et dans le sens large : `modules_activés` y est
vide, donc TOUS les modules installés comptent pour actifs (§3.3), au lieu des
seuls activés. Le smoke test est plus couvrant, pas plus étroit. **Ne PAS
détourner `EPURE_MODULES_DIR` en même temps** : l'arbre réel est précisément ce
que ce script vient éprouver, le rediriger ne testerait plus rien.
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
        from core.module_registry import list_modules, _modules_dir

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
            if not (_modules_dir() / mid / "router.py").is_file():
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
