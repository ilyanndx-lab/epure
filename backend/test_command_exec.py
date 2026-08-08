#!/usr/bin/env python3
"""Tests des lancements de process pilotables depuis l'API (durcissement v1, lot 2).

Trois `shell=True` étaient atteignables par requête :

  - ``GET /admin/open?path=…``            → ``explorer /select,"{path}"``
  - ``POST /settings/gateway/start``      → ``atelier.gateway.start_command``
  - ``/ws/workshop`` en mode terminal     → ``start "Atelier {id}" cmd /K …``

Ce qui est vérifiable en CI (Linux comme Windows) : le confinement de chemin,
l'allowlist de binaires, la validation de l'identifiant de module, et surtout
**la forme de l'argv transmis** — aucun de ces appels ne doit repasser par un
shell, et aucune entrée client ne doit être concaténée dans une ligne de
commande.

Ce qui ne l'est PAS et reste à vérifier à la main sous Windows : que
l'explorateur sélectionne bien le fichier (cf. ExplorerArgvTest, qui verrouille
la forme de l'argument mais ne peut pas juger du comportement de la GUI).

Usage :
    python test_command_exec.py
"""

import os
import sys
import tempfile
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
from core.codeagent import SecurityError  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from modules.admin import router as admin_router  # noqa: E402


def _client() -> TestClient:
    """Même configuration que test_auth_surface (Host + IP source locale)."""
    return TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 54321))


class SplitCommandTest(unittest.TestCase):
    """Découpage d'une ligne de commande utilisateur en argv, sans shell."""

    def test_commande_simple(self):
        self.assertEqual(
            module_workshop._split_command("litellm --config cfg.yaml"),
            ["litellm", "--config", "cfg.yaml"],
        )

    @unittest.skipUnless(os.name == "nt", "règles de citation propres à Windows")
    def test_chemin_windows_avec_espaces(self):
        """Les guillemets doivent disparaître du jeton, pas rester dedans.

        shlex en mode non-POSIX (obligatoire sous Windows, sinon les antislashs
        sont mangés) conserve les guillemets : sans retrait explicite, litellm
        recevrait un chemin encadré de guillemets littéraux.
        """
        argv = module_workshop._split_command(r'litellm --config "C:\Mes Configs\cfg.yaml"')
        self.assertEqual(argv, ["litellm", "--config", r"C:\Mes Configs\cfg.yaml"])

    @unittest.skipUnless(os.name == "nt", "règles de citation propres à Windows")
    def test_antislashs_preserves(self):
        argv = module_workshop._split_command(r"C:\Python\python.exe -m litellm")
        self.assertEqual(argv[0], r"C:\Python\python.exe")

    def test_metacaracteres_deviennent_des_arguments(self):
        """Sans shell, « & calc.exe » n'enchaîne plus rien : c'est du texte."""
        argv = module_workshop._split_command("litellm & calc.exe")
        self.assertEqual(argv[0], "litellm")
        self.assertIn("calc.exe", argv[1:])


class StartGatewayTest(unittest.TestCase):
    """Allowlist de binaires + absence de shell sur start_command."""

    def setUp(self):
        self.lances: list = []
        self._popen = mock.patch.object(
            module_workshop.subprocess, "Popen",
            side_effect=lambda *a, **k: self.lances.append((a, k)),
        )
        self._popen.start()
        self.addCleanup(self._popen.stop)
        # Passerelle jamais joignable, sinon start_gateway court-circuite.
        self._reach = mock.patch.object(module_workshop, "gateway_reachable", return_value=False)
        self._reach.start()
        self.addCleanup(self._reach.stop)

    def _avec_commande(self, cmd: str):
        return mock.patch.object(
            module_workshop, "_gateway_cfg",
            return_value={"base_url": "http://localhost:4000", "model": "",
                          "api_key": "", "start_command": cmd},
        )

    def test_commande_vide_refusee(self):
        with self._avec_commande(""):
            res = module_workshop.start_gateway()
        self.assertFalse(res["ok"])
        self.assertEqual(self.lances, [])

    def test_binaire_hors_allowlist_refuse(self):
        with self._avec_commande("calc.exe"):
            res = module_workshop.start_gateway()
        self.assertFalse(res["ok"])
        self.assertIn("non autorisé", res["raison"])
        self.assertEqual(self.lances, [], "rien ne doit être lancé")

    def test_injection_par_metacaractere_refusee(self):
        """« litellm & calc.exe » : le binaire de tête passe, mais plus de shell.

        L'allowlist ne voit qu'un `litellm` légitime — c'est l'absence de shell
        qui neutralise la suite, transmise comme simples arguments.
        """
        with self._avec_commande("litellm & calc.exe"):
            res = module_workshop.start_gateway()
        self.assertTrue(res["ok"])
        (args, kwargs), = self.lances
        self.assertEqual(args[0], ["litellm", "&", "calc.exe"])
        self.assertFalse(kwargs.get("shell", False))

    def test_commande_autorisee_lancee_en_liste(self):
        with self._avec_commande("litellm --config cfg.yaml"):
            res = module_workshop.start_gateway()
        self.assertTrue(res["ok"], res)
        (args, kwargs), = self.lances
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0], ["litellm", "--config", "cfg.yaml"])
        self.assertFalse(kwargs.get("shell", False))

    def test_chemin_complet_vers_python_autorise(self):
        """L'allowlist compare le *stem* : C:\\...\\python.exe → « python »."""
        exe = r"C:\Python\python.exe" if os.name == "nt" else "/usr/bin/python3"
        with self._avec_commande(f"{exe} -m litellm"):
            res = module_workshop.start_gateway()
        self.assertTrue(res["ok"], res)


class ModuleIdValidationTest(unittest.TestCase):
    """L'identifiant qui finit dans un .bat / une ligne tmux."""

    def test_ids_valides(self):
        for mid in ("hello", "mon_module", "a1"):
            self.assertEqual(module_workshop._check_module_id(mid), mid)

    def test_ids_refuses(self):
        # ../chat : traversée qui reste sous modules/, donc invisible pour le
        # confinement de chemin. "; id; " : injection shell sous POSIX.
        for mid in ("../chat", "..", "a/b", "", "Chat", "_staging", '"; id; "', "a" * 40):
            with self.subTest(mid=mid), self.assertRaises(SecurityError):
                module_workshop._check_module_id(mid)

    def test_open_terminal_refuse_un_id_invalide(self):
        with mock.patch.object(module_workshop.subprocess, "Popen") as popen:
            with self.assertRaises(SecurityError):
                module_workshop.open_terminal("../chat", "spec", "new", "claude_sub")
        popen.assert_not_called()


class AdminOpenTest(unittest.TestCase):
    """Confinement de GET /admin/open, vu depuis le réseau."""

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.headers = {"Authorization": f"Bearer {get_api_token()}"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self._tmp.name).resolve()
        self.fichier = self.racine / "fiche.pdf"
        self.fichier.write_text("x", encoding="utf-8")
        (self.racine.parent / "hors-perimetre").mkdir(exist_ok=True)
        self.dehors = self.racine.parent / "hors-perimetre" / "secret.txt"
        self.dehors.write_text("x", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

        self.lances: list = []
        p = mock.patch.object(
            admin_router.subprocess, "Popen",
            side_effect=lambda *a, **k: self.lances.append((a, k)),
        )
        p.start()
        self.addCleanup(p.stop)
        r = mock.patch.object(admin_router, "_openable_roots", return_value=[self.racine])
        r.start()
        self.addCleanup(r.stop)

    def _open(self, path):
        return self.client.get("/admin/open", params={"path": str(path)}, headers=self.headers)

    def test_fichier_autorise(self):
        r = self._open(self.fichier)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.lances), 1)

    def test_fichier_hors_des_racines(self):
        r = self._open(self.dehors)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.lances, [])

    def test_traversee_relative(self):
        r = self._open(self.racine / ".." / "hors-perimetre" / "secret.txt")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.lances, [])

    def test_fichier_inexistant_sous_une_racine(self):
        r = self._open(self.racine / "absent.pdf")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(self.lances, [])

    def test_injection_de_commande(self):
        """Le payload historique : ?path=x" & calc.exe & " → 403, rien lancé."""
        r = self._open('x" & calc.exe & "')
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.lances, [])


class OpenableRootsTest(unittest.TestCase):
    """Les vraies racines autorisées (hors patch, contrairement à AdminOpenTest).

    Le détail des racines est vérifié dans test_upload_paths.py, avec le reste de
    `core.paths.user_data_roots()` — ici on vérifie seulement que /admin/open
    utilise bien cette liste partagée, et pas une copie locale.
    """

    def test_delegue_aux_racines_partagees(self):
        from core.paths import user_data_roots

        self.assertEqual(admin_router._openable_roots(), user_data_roots())

    def test_racine_systeme_non_couverte(self):
        """Un exécutable système ne doit être sous aucune racine autorisée."""
        cible = Path(r"C:\Windows\System32\calc.exe") if os.name == "nt" else Path("/etc/passwd")
        self.assertFalse(
            any(cible.is_relative_to(r.resolve()) for r in admin_router._openable_roots())
        )


class ExplorerArgvTest(unittest.TestCase):
    """La forme de l'argument passé à explorer — le point NON testable en CI.

    ``explorer`` exige « /select,<chemin> » en UN SEUL argument. Ce test
    verrouille cette forme pour qu'un futur « nettoyage » en trois arguments
    (["explorer", "/select,", chemin]) soit rattrapé ici. Il ne dit rien du
    comportement réel de l'explorateur, qui demande une vérification manuelle
    sous Windows — en particulier avec un chemin contenant une espace.
    """

    @unittest.skipUnless(os.name == "nt", "branche Windows")
    def test_select_en_un_seul_argument(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        racine = Path(tmp.name).resolve()
        cible = racine / "Mon Dossier"
        cible.mkdir()
        fichier = cible / "ma fiche.pdf"
        fichier.write_text("x", encoding="utf-8")

        lances: list = []
        with mock.patch.object(admin_router.subprocess, "Popen",
                               side_effect=lambda *a, **k: lances.append(a)), \
             mock.patch.object(admin_router, "_openable_roots", return_value=[racine]):
            client = _client()
            r = client.get("/admin/open", params={"path": str(fichier)},
                           headers={"Authorization": f"Bearer {get_api_token()}"})
        self.assertEqual(r.status_code, 200, r.text)
        (argv,), = lances
        self.assertEqual(len(argv), 2, f"attendu 2 arguments, reçu {argv!r}")
        self.assertEqual(argv[0], "explorer")
        self.assertEqual(argv[1], f"/select,{fichier}")
        # Le chemin ne doit pas être ré-encadré de guillemets par nos soins :
        # c'est list2cmdline qui s'en charge au moment du CreateProcess.
        self.assertNotIn('"', argv[1])


if __name__ == "__main__":
    unittest.main()
