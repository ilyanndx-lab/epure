"""Catalogue : installation, et surtout SUPPRESSION — un endpoint destructif.

`DELETE /settings/modules/{id}` prend un identifiant venant du client et fait un
`rmtree` avec. C'est la forme exacte de la faille du lot 3 : `_staging_dir`
acceptait `"../chat"`, et `(modules_dir() / "_staging/../chat").resolve()` vaut
`modules/chat` — dont `is_relative_to(modules_dir())` est **vrai**. Le confinement
de chemin ne voit rien passer ; seule la validation d'identifiant protège.

Ces tests s'exécutent sur un arbre de modules temporaire ET importent depuis ce
même arbre : `_test_env` rebranche `modules.__path__` dessus. Sans ce
rebranchement, deux arbres coexistaient — on supprimait dans le temporaire mais
on importait depuis le vrai — et un test « ce module n'est plus montable »
passait pour la mauvaise raison (mesuré avant d'écrire ce fichier).

Usage :
    python test_catalogue.py
"""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les arbres AVANT tout import de core.* / main

os.environ["EPURE_ALLOWED_HOSTS"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from core import catalogue, module_registry  # noqa: E402
from core.auth import get_api_token  # noqa: E402
from core.codeagent import SecurityError  # noqa: E402
from core.paths import resolve_generated_dir, resolve_modules_dir  # noqa: E402

#: Mêmes ids refusés que test_workshop_paths — la surface d'attaque est la même.
IDS_INVALIDES = (
    "../chat", "..", "../../evil", "_staging", "a/b", "a\\b", "", "   ",
    "Chat", "1abc", "a-b", "a.b", '"; id; "', "a" * 40, "é",
)


def _client() -> TestClient:
    return TestClient(main.app, base_url="http://localhost", client=("127.0.0.1", 54321))


def _manifeste(mid: str, **surcharges) -> dict:
    d = {
        "id": mid, "version": "1.0.0", "nom": mid.capitalize(), "icon": "Box",
        "description": f"module {mid}", "frontend": {"component": "Component"},
        "backend": {"prefix": ""}, "core_module": False, "origin": "catalogue",
        "status": "active", "removable": True,
    }
    d.update(surcharges)
    return d


class BaseCatalogue(unittest.TestCase):
    """Installe un module jetable dans l'arbre temporaire."""

    ID = "zz_jetable"

    def setUp(self):
        self.modules = resolve_modules_dir()
        self.generated = resolve_generated_dir()
        self.addCleanup(self._nettoyer)

    def _nettoyer(self):
        shutil.rmtree(self.modules / self.ID, ignore_errors=True)
        shutil.rmtree(self.generated / self.ID, ignore_errors=True)

    def _poser_module(self, mid=None, **surcharges):
        mid = mid or self.ID
        d = self.modules / mid
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(
            json.dumps(_manifeste(mid, **surcharges), ensure_ascii=False), encoding="utf-8"
        )
        (d / "router.py").write_text(
            "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
            f"@router.get('/{mid}/ping')\n"
            f"async def ping():\n    return {{'module': '{mid}'}}\n",
            encoding="utf-8",
        )
        comp = self.generated / mid
        comp.mkdir(parents=True, exist_ok=True)
        (comp / "Component.tsx").write_text("export default function C(){return null}\n", encoding="utf-8")
        return d


class SuppressionConfinementTest(BaseCatalogue):
    """La classe de faille du lot 3 : un id client dans un rmtree."""

    def test_ids_invalides_leves_avant_toute_ecriture(self):
        for mid in IDS_INVALIDES:
            with self.subTest(mid=mid), self.assertRaises((SecurityError, catalogue.CatalogueError)):
                catalogue.uninstall(mid)

    def test_traversee_ne_supprime_pas_un_module_du_coeur(self):
        chat = self.modules / "chat" / "manifest.json"
        self.assertTrue(chat.is_file(), "prérequis : chat est installé dans l'arbre de test")
        with self.assertRaises(SecurityError):
            catalogue.uninstall("../chat")
        self.assertTrue(chat.is_file(), "le module chat a été supprimé !")

    def test_module_du_coeur_refuse(self):
        for mid in ("chat", "admin", "history", "settings"):
            with self.subTest(mid=mid):
                with self.assertRaises(catalogue.CatalogueError):
                    catalogue.uninstall(mid)
                self.assertTrue((self.modules / mid / "manifest.json").is_file())

    def test_removable_false_refuse(self):
        self._poser_module(core_module=False, removable=False)
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.uninstall(self.ID)
        self.assertTrue((self.modules / self.ID / "manifest.json").is_file())

    def test_id_inconnu_refuse(self):
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.uninstall("zz_nexistepas")


class SuppressionOrdreTest(BaseCatalogue):
    """La sauvegarde doit exister AVANT que quoi que ce soit soit effacé."""

    def test_sauvegarde_creee_et_complete(self):
        self._poser_module()
        res = catalogue.uninstall(self.ID)
        sauv = Path(res["sauvegarde"])
        self.assertTrue(sauv.is_dir(), "aucune sauvegarde créée")
        self.assertTrue((sauv / "manifest.json").is_file())
        self.assertTrue((sauv / "router.py").is_file())

    def test_la_sauvegarde_existe_avant_la_suppression(self):
        """Vérifié par observation pendant l'opération, pas après coup.

        `_backup_existing` est instrumenté pour constater, au moment précis où
        la sauvegarde vient d'être écrite, que les dossiers d'origine sont
        ENCORE là. Un ordre inversé (effacer puis sauvegarder) produirait une
        sauvegarde vide sans que le test final le voie.
        """
        self._poser_module()
        vu = {}
        vrai_backup = catalogue._backup_existing

        def espion(mid):
            chemin = vrai_backup(mid)
            vu["sauvegarde_ecrite"] = chemin is not None and Path(chemin).is_dir()
            vu["source_encore_presente"] = (self.modules / mid).is_dir()
            vu["contenu"] = sorted(p.name for p in Path(chemin).iterdir()) if chemin else []
            return chemin

        catalogue._backup_existing = espion
        self.addCleanup(setattr, catalogue, "_backup_existing", vrai_backup)
        catalogue.uninstall(self.ID)

        self.assertTrue(vu["sauvegarde_ecrite"], "la sauvegarde n'existait pas à cet instant")
        self.assertTrue(vu["source_encore_presente"], "la source était déjà supprimée")
        self.assertIn("manifest.json", vu["contenu"])
        self.assertIn("router.py", vu["contenu"])

    def test_les_deux_dossiers_sont_supprimes(self):
        self._poser_module()
        catalogue.uninstall(self.ID)
        self.assertFalse((self.modules / self.ID).exists(), "backend/modules/<id> subsiste")
        self.assertFalse((self.generated / self.ID).exists(), "generated/<id> subsiste")

    def test_retire_de_modules_actives(self):
        self._poser_module()
        module_registry.set_status(self.ID, "active")
        self.assertIn(self.ID, module_registry.active_ids())
        catalogue.uninstall(self.ID)
        self.assertNotIn(self.ID, module_registry.active_ids())

    def test_module_supprime_n_est_plus_importable(self):
        """Vaut grâce au rebranchement de modules.__path__ par _test_env.

        Sans lui, l'import réussirait depuis le vrai arbre et ce test passerait
        en donnant l'illusion d'une vérification.
        """
        import importlib
        self._poser_module()
        importlib.import_module(f"modules.{self.ID}.router")  # doit réussir
        catalogue.uninstall(self.ID)
        for m in [m for m in sys.modules if m.startswith(f"modules.{self.ID}")]:
            del sys.modules[m]
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module(f"modules.{self.ID}.router")


class InstallationTest(BaseCatalogue):
    def test_catalogue_liste_les_six_avec_installe(self):
        mods = catalogue.list_catalogue()
        ids = sorted(m["id"] for m in mods)
        self.assertEqual(ids, ["code", "docs", "flashcards", "kholle", "rangement", "reviseur"])
        for m in mods:
            self.assertIn("installé", m)
            self.assertFalse(m["installé"], "aucun installable ne doit l'être par défaut")

    def test_install_copie_les_trois_fichiers(self):
        catalogue.install("flashcards")
        self.addCleanup(shutil.rmtree, self.modules / "flashcards", True)
        self.addCleanup(shutil.rmtree, self.generated / "flashcards", True)
        self.assertTrue((self.modules / "flashcards" / "manifest.json").is_file())
        self.assertTrue((self.modules / "flashcards" / "router.py").is_file())
        self.assertTrue((self.generated / "flashcards" / "Component.tsx").is_file())
        self.assertIn("flashcards", module_registry.active_ids())

    def test_install_refuse_un_id_deja_installe(self):
        catalogue.install("flashcards")
        self.addCleanup(shutil.rmtree, self.modules / "flashcards", True)
        self.addCleanup(shutil.rmtree, self.generated / "flashcards", True)
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.install("flashcards")

    def test_install_refuse_un_id_hors_catalogue(self):
        with self.assertRaises(catalogue.CatalogueError):
            catalogue.install("zz_pas_au_catalogue")

    def test_install_refuse_les_ids_invalides(self):
        for mid in IDS_INVALIDES:
            with self.subTest(mid=mid), self.assertRaises((SecurityError, catalogue.CatalogueError)):
                catalogue.install(mid)

    def test_install_monte_le_routeur_a_chaud(self):
        """Ne vaut que parce que modules.__path__ pointe l'arbre temporaire."""
        client = _client()
        headers = {"Authorization": f"Bearer {get_api_token()}"}
        catalogue.install("flashcards", app=main.app)
        self.addCleanup(shutil.rmtree, self.modules / "flashcards", True)
        self.addCleanup(shutil.rmtree, self.generated / "flashcards", True)
        res = client.get("/flashcards/decks", headers=headers)
        self.assertNotEqual(res.status_code, 404, "le routeur n'a pas été monté")


class HttpTest(BaseCatalogue):
    """Le refus ressort en 400, pas en 500 opaque."""

    @classmethod
    def setUpClass(cls):
        cls.client = _client()
        cls.headers = {"Authorization": f"Bearer {get_api_token()}"}

    def test_get_catalogue(self):
        res = self.client.get("/settings/catalogue", headers=self.headers)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(len(res.json()["modules"]), 6)

    def test_delete_id_invalide_400(self):
        for mid in ("Chat", "_staging", "1abc", "a-b"):
            with self.subTest(mid=mid):
                res = self.client.delete(f"/settings/modules/{mid}", headers=self.headers)
                self.assertEqual(res.status_code, 400, res.text)

    def test_delete_module_du_coeur_400(self):
        res = self.client.delete("/settings/modules/chat", headers=self.headers)
        self.assertEqual(res.status_code, 400, res.text)
        self.assertTrue((self.modules / "chat" / "manifest.json").is_file())

    def test_delete_id_inconnu_400(self):
        res = self.client.delete("/settings/modules/zz_inconnu", headers=self.headers)
        self.assertEqual(res.status_code, 400, res.text)

    def test_install_id_invalide_400(self):
        res = self.client.post("/settings/catalogue/Chat/install", headers=self.headers)
        self.assertEqual(res.status_code, 400, res.text)

    def test_le_detail_ne_fuit_pas_de_chemin_absolu(self):
        res = self.client.delete("/settings/modules/zz_inconnu", headers=self.headers)
        self.assertNotIn(str(self.modules), res.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
