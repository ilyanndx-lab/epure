#!/usr/bin/env python3
"""Confinement des identifiants de module de l'Atelier (durcissement v1, lot 3.1).

Le garde-fou de chemin (`_modules_safe_path`) ne voyait rien passer, et c'est
contre-intuitif : `(modules_dir() / "_staging/../chat").resolve()` vaut
`modules/chat`, dont `is_relative_to(modules_dir())` est **vrai**. La cible ne sort
jamais de `modules/` — elle change juste de module. Deux dégâts concrets :

  - `{"type":"generate","id":"../chat"}` → le LLM écrit dans un module core,
    hors staging, sans validation ni approbation ;
  - `{"type":"reject","id":"../hello"}` → `shutil.rmtree` du module en place.

Ces tests vérifient la validation à la source (`_staging_dir` et toute
construction `<dossier> / module_id`), et que le refus ressort en 400 côté HTTP
et en erreur typée côté WebSocket — pas en 500 opaque.

Usage :
    python test_workshop_paths.py
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# Permettre d'importer le package `core` depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core import module_workshop  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.codeagent import SecurityError  # noqa: E402

#: Ids refusés. `..`/`a/b` : traversée. `Chat`/`1a`/`a-b` : hors de _ID_RE.
#: `_staging` : le dossier de staging lui-même. `"; id; "` : injection shell
#: (l'id finit dans un .bat et dans une ligne tmux, cf. test_command_exec).
IDS_INVALIDES = (
    "../chat", "..", "../../evil", "_staging", "a/b", "a\\b", "", "   ",
    "Chat", "1abc", "a-b", "a.b", '"; id; "', "a" * 40, "é",
)


def _client() -> TestClient:
    """Même configuration que test_auth_surface (Host + IP source locale)."""
    return TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 54321))


def _ws(client: TestClient):
    """Connexion à /ws/workshop.

    Le `host` explicite est nécessaire : le TestClient n'envoie pas de Host
    exploitable sur une poignée de main WebSocket, et TrustedHostMiddleware
    (lot 1) répond alors 400 « Invalid host header ». Le token passe en query
    param — les navigateurs n'autorisent pas d'en-tête sur `new WebSocket()`.
    """
    return client.websocket_connect(
        f"/ws/workshop?token={get_api_token()}", headers={"host": "localhost"}
    )


class StagingDirTest(unittest.TestCase):
    """`_staging_dir` : le point de passage obligé de tous les appelants."""

    def test_id_valide(self):
        self.assertEqual(
            module_workshop._staging_dir("hello"),
            module_workshop.modules_dir() / "_staging" / "hello",
        )

    def test_id_valide_avec_espaces_autour(self):
        """L'id est strippé avant validation (le front peut envoyer du blanc)."""
        self.assertEqual(
            module_workshop._staging_dir("  hello  "),
            module_workshop.modules_dir() / "_staging" / "hello",
        )

    def test_ids_invalides(self):
        for mid in IDS_INVALIDES:
            with self.subTest(mid=mid), self.assertRaises(SecurityError):
                module_workshop._staging_dir(mid)

    def test_traversee_resterait_sous_modules(self):
        """Pourquoi _modules_safe_path ne suffisait pas — le cœur du problème.

        Si ce test échoue un jour, c'est que la sémantique de resolve() a changé
        et que la validation d'id n'est plus la seule protection.
        """
        cible = (module_workshop.modules_dir() / "_staging/../chat").resolve()
        self.assertTrue(cible.is_relative_to(module_workshop.modules_dir()))
        self.assertEqual(cible.name, "chat")


class ModulePathHelpersTest(unittest.TestCase):
    """Les autres constructions `<dossier> / module_id`, hors staging."""

    def test_module_exists(self):
        self.assertTrue(module_workshop.module_exists("chat"))
        for mid in ("../chat", "..", "a/b"):
            with self.subTest(mid=mid), self.assertRaises(SecurityError):
                module_workshop.module_exists(mid)

    def test_meta_path(self):
        with self.assertRaises(SecurityError):
            module_workshop._meta_path("../chat")

    def test_active_files(self):
        with self.assertRaises(SecurityError):
            module_workshop._active_files("../chat")

    def test_staging_files(self):
        with self.assertRaises(SecurityError):
            module_workshop._staging_files("../chat")

    def test_frontend_component_path(self):
        """Seule construction qui sorte de backend/ : approve() y écrit."""
        for mid in ("../../evil", "../generated", "a/b"):
            with self.subTest(mid=mid), self.assertRaises(SecurityError):
                module_workshop._frontend_component_path(mid)

    def test_backup_existing(self):
        with self.assertRaises(SecurityError):
            module_workshop._backup_existing("../chat")


class DestructionTest(unittest.TestCase):
    """Ce qu'un id non validé détruisait réellement."""

    def test_reject_ne_supprime_pas_un_module_en_place(self):
        chat = module_workshop.modules_dir() / "chat" / "manifest.json"
        self.assertTrue(chat.is_file(), "prérequis : le module chat existe")
        with self.assertRaises(SecurityError):
            module_workshop.reject("../chat")
        self.assertTrue(chat.is_file(), "le module chat a été supprimé !")

    def test_approve_refuse_avant_toute_ecriture(self):
        with mock.patch.object(module_workshop, "_backup_existing") as backup:
            with self.assertRaises(SecurityError):
                module_workshop.approve("../chat")
        backup.assert_not_called()

    def test_liste_de_staging_tolere_un_nom_de_dossier_inattendu(self):
        """Filtrage et non validation : un résidu de sonde de verrou
        (`hello.__lockprobe__`, cf. _staging_locked) ne doit pas faire tomber
        GET /workshop/modules en 500."""
        module_workshop.staging_root().mkdir(parents=True, exist_ok=True)
        residu = module_workshop.staging_root() / "zz_test.__lockprobe__"
        residu.mkdir(exist_ok=True)
        self.addCleanup(lambda: residu.exists() and residu.rmdir())
        module_workshop.list_staging()  # ne doit pas lever


class HttpStatusTest(unittest.TestCase):
    """Le refus ressort en 400, pas en 500 opaque ni en 404 trompeur.

    Les ids testés ne contiennent pas de séparateur : httpx et Starlette
    normalisent `..%2F` dans l'URL, donc un `../chat` ne parviendrait pas tel
    quel au handler. La traversée est couverte au niveau fonction ci-dessus ;
    ici on vérifie le mapping d'erreur.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.headers = {"Authorization": f"Bearer {get_api_token()}"}

    def test_routes_workshop_par_id(self):
        for mid in ("Chat", "_staging", "1abc", "a-b"):
            for methode, url in (
                ("post", f"/workshop/{mid}/reject"),
                ("post", f"/workshop/{mid}/validate"),
                ("post", f"/workshop/{mid}/approve"),
                ("get", f"/workshop/staging/{mid}"),
            ):
                with self.subTest(mid=mid, url=url):
                    res = getattr(self.client, methode)(url, headers=self.headers)
                    self.assertEqual(res.status_code, 400, res.text)

    def test_edit_et_generate(self):
        """Ces deux-là validaient déjà (ValueError → 400) : non-régression."""
        res = self.client.post("/workshop/Chat/edit", json={}, headers=self.headers)
        self.assertEqual(res.status_code, 400, res.text)
        res = self.client.post("/workshop/generate", json={"id": "Chat"}, headers=self.headers)
        self.assertEqual(res.status_code, 400, res.text)

    def test_le_message_ne_fuit_pas_de_chemin_absolu(self):
        """Un détail d'erreur renvoyé au client ne doit pas exposer l'arborescence."""
        res = self.client.post("/workshop/Chat/reject", headers=self.headers)
        detail = res.json().get("detail", "")
        self.assertNotIn(str(module_workshop.modules_dir()), detail)


class WebSocketErrorTest(unittest.TestCase):
    """Côté /ws/workshop : erreur typée, socket vivante.

    C'est le chemin réellement exploitable — l'id y arrive de `msg.get("id","")`
    sans passer par une route FastAPI, donc sans validation de type ni de forme.
    """

    def test_id_invalide_renvoie_une_erreur_typee(self):
        with _ws(_client()) as ws:
            ws.send_json({"type": "workshop_chat", "id": "../chat", "message": "x"})
            ev = ws.receive_json()
            self.assertEqual(ev["type"], "error")
            self.assertEqual(ev.get("code"), "invalid_id")
            self.assertIn("invalide", ev.get("content", ""))
            # Le protocole exige un "done" après l'erreur, sinon le front reste
            # bloqué sur le spinner de génération.
            self.assertEqual(ws.receive_json()["type"], "done")

    def test_terminal_avec_id_invalide_ne_lance_aucun_process(self):
        with mock.patch.object(module_workshop.subprocess, "Popen") as popen:
            with _ws(_client()) as ws:
                ws.send_json({
                    "type": "generate", "engine": "claude_sub", "mode": "terminal",
                    "id": "../chat", "description": "x",
                })
                ev = ws.receive_json()
                self.assertEqual(ev["type"], "error")
                self.assertEqual(ev.get("code"), "invalid_id")
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
