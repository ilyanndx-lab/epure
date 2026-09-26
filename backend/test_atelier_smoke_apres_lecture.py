#!/usr/bin/env python3
"""Le smoke test de l'Atelier n'exécute le code généré qu'après « j'ai lu ».

Le smoke test EXÉCUTE ``router.py`` (sous-processus). Il partait tout seul
après chaque génération : le code tournait avant que quiconque l'ait lu, ce qui
annulait le principe « relu avant exécution » de la politique retenue
(``docs/etude-isolation-modules.md`` §1.1, ``docs/feuille-de-route.md`` §5).

Verrouille, par ``/ws/workshop`` réel (TestClient), moteur ``ollama`` simulé :

  1. une génération ne lance AUCUN sous-processus tant que ``smoke_confirm``
     n'est pas arrivé (``subprocess.Popen`` piégé ; tsc, qui lit sans exécuter,
     est neutralisé à part — décision d'Ilyann, 2026-09-26) ;
  2. ``smoke_confirm`` avec l'empreinte de la version affichée lance le test,
     une seule fois ;
  3. une empreinte périmée (le code a changé depuis la lecture) est refusée
     (``stale_review``) sans rien exécuter ;
  4. un échec déclenche UNE passe de correction, qui n'est pas ré-exécutée :
     le code réparé est neuf donc non lu (``repaired_unread``).

Usage :
    python test_atelier_smoke_apres_lecture.py
"""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401 — isole les dossiers AVANT tout import de core.*/main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("EPURE_CORS_ORIGINS", "http://localhost:5173")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core import module_workshop  # noqa: E402
from core.auth import get_api_token  # noqa: E402

#: URL ABSOLUE — cf. test_chat_ws_conversation.py, piège TrustedHostMiddleware.
_WS = "ws://localhost/ws/workshop?token={t}"
_MID = "zzlecture"

_ROUTER = f'''from fastapi import APIRouter

router = APIRouter()


@router.get("/{_MID}/ping")
async def ping():
    return {{"ok": True}}
'''
_COMPONENT = "export default function Component() {\n  return <div>ok</div>\n}\n"
_MANIFEST = json.dumps({
    "id": _MID, "version": "1.0.0", "nom": "Lecture", "icon": "Box",
    "description": "test", "frontend": {"component": "Component"},
    "backend": {"prefix": ""}, "core_module": False, "origin": "workshop",
    "status": "active", "removable": True,
})


def _faux_generate(module_id, spec, kind, model=None, feedback=None):
    """Moteur simulé : écrit 3 fichiers valides (le router change en réparation,
    pour que l'empreinte change comme dans la vraie vie)."""
    sdir = module_workshop._staging_dir(module_id)
    (sdir / "manifest.json").write_text(_MANIFEST, encoding="utf-8")
    router = _ROUTER + ("\n# réparé\n" if feedback else "")
    (sdir / "router.py").write_text(router, encoding="utf-8")
    (sdir / "Component.tsx").write_text(_COMPONENT, encoding="utf-8")
    yield {"type": "token", "content": "ok"}


class SmokeApresLectureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, base_url="http://localhost",
                                client=("127.0.0.1", 54323))
        cls.token = get_api_token()

    def setUp(self):
        module_workshop.prepare(_MID, "new", "ollama", "headless")
        self.addCleanup(module_workshop.reject, _MID)
        self.generations: list = []
        self.smokes: list = []

        def generate(*a, **kw):
            self.generations.append(kw.get("feedback"))
            return _faux_generate(*a, **kw)

        for cible, valeur in (
            ("generate_ollama", generate),
            ("typecheck_staging", lambda mid: {"warnings": []}),
        ):
            p = mock.patch.object(module_workshop, cible, valeur)
            p.start()
            self.addCleanup(p.stop)

    def _smoke(self, resultat: dict):
        def faux(mid):
            self.smokes.append(mid)
            return resultat
        p = mock.patch.object(module_workshop, "smoke_test_staging", faux)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _jusqua(ws, predicat) -> list[dict]:
        evs = []
        while True:
            ev = json.loads(ws.receive_text())
            evs.append(ev)
            if predicat(ev):
                return evs

    def _generer(self, ws):
        ws.send_text(json.dumps({"type": "generate", "id": _MID, "kind": "new",
                                 "description": "un ping", "engine": "ollama"}))
        evs = self._jusqua(ws, lambda e: e.get("type") == "done")
        valide = [e for e in evs if e.get("type") == "validated"]
        self.assertTrue(valide and valide[-1]["report"]["ok"], evs)

    def test_generation_ne_lance_aucun_sous_processus(self):
        self._smoke({"ok": True})
        piege = mock.patch.object(subprocess, "Popen",
                                  side_effect=AssertionError("sous-processus lancé"))
        with piege as popen, self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._generer(ws)
            # Aller-retour de synchronisation : toute tâche de fond créée à la fin
            # de la génération a eu la main avant que ce refus revienne.
            ws.send_text(json.dumps({"type": "smoke_confirm", "id": _MID, "empreinte": "x"}))
            evs = self._jusqua(ws, lambda e: e.get("type") == "error")
        popen.assert_not_called()
        self.assertEqual(self.smokes, [])
        self.assertFalse([e for e in evs if e.get("type") == "smoke"], evs)

    def test_confirmation_lance_le_test_une_fois(self):
        self._smoke({"ok": True, "tested": ["GET /zzlecture/ping → 200"],
                     "failures": [], "skipped": []})
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._generer(ws)
            empreinte = module_workshop.read_staging(_MID)["empreinte"]
            ws.send_text(json.dumps({"type": "smoke_confirm", "id": _MID,
                                     "empreinte": empreinte}))
            evs = self._jusqua(ws, lambda e: e.get("type") == "smoke" and e.get("phase") == "done")
        self.assertEqual(self.smokes, [_MID])
        self.assertEqual(evs[-1]["status"], "ok")

    def test_empreinte_perimee_refusee(self):
        self._smoke({"ok": True})
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._generer(ws)
            lue = module_workshop.read_staging(_MID)["empreinte"]
            # Le code change APRÈS la lecture (moteur encore actif, édition…).
            router = module_workshop._staging_dir(_MID) / "router.py"
            router.write_text(router.read_text(encoding="utf-8") + "\n# ajout\n",
                              encoding="utf-8")
            ws.send_text(json.dumps({"type": "smoke_confirm", "id": _MID, "empreinte": lue}))
            evs = self._jusqua(ws, lambda e: e.get("type") == "error")
        self.assertEqual(evs[-1].get("code"), "stale_review")
        self.assertEqual(self.smokes, [])

    def test_reparation_unique_et_non_executee(self):
        self._smoke({"ok": False, "tested": [], "failures": [],
                     "skipped": [], "error": "boom"})
        with self.client.websocket_connect(_WS.format(t=self.token)) as ws:
            self._generer(ws)
            empreinte = module_workshop.read_staging(_MID)["empreinte"]
            ws.send_text(json.dumps({"type": "smoke_confirm", "id": _MID,
                                     "empreinte": empreinte}))
            evs = self._jusqua(ws, lambda e: e.get("type") == "smoke" and e.get("phase") == "done")
        self.assertEqual(self.smokes, [_MID], "le code réparé ne doit pas être exécuté")
        self.assertEqual(len(self.generations), 2)  # génération + UNE réparation
        self.assertIsNotNone(self.generations[1])
        self.assertEqual(evs[-1]["status"], "repaired_unread")
        self.assertTrue([e for e in evs if e.get("type") == "validated"], evs)
        # La version réparée a une autre empreinte : l'ancienne « lecture » ne vaut plus.
        self.assertNotEqual(module_workshop.read_staging(_MID)["empreinte"], empreinte)


if __name__ == "__main__":
    unittest.main()
