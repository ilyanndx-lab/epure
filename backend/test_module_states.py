"""Modèle d'état des modules : deux états, `modules_activés` seule source.

Ce fichier couvre la bascule décrite dans CLAUDE.md §3.3 et
docs/catalogue-modules.md §1 : suppression de `memory/modules_state.json` au
profit de la seule liste ordonnée `instance_config.modules_activés`.

Pourquoi ces tests existent : les deux stockages avaient divergé en silence
(9 entrées fantômes d'un côté, 4 de l'autre, `reviseur` installé et monté mais
absent de la barre). Rien ne l'avait signalé parce que rien ne le vérifiait.

Isolation — aucun test ne touche la vraie configuration :
  * `_test_env` pose `EPURE_DATA_DIR` sur un temporaire avant tout import ;
  * `_MODULES_DIR` pointe un dossier temporaire de faux manifestes ;
  * `instance_config` est remplacé par une `InstanceConfig` sur un JSON temporaire ;
  * `_legacy_state_file` (fonction) renvoie le temporaire lui aussi ;
  * `register_routers` reçoit une fausse app et un faux `import_module`, pour ne
    pas dépendre du package `modules` réellement importable.

Usage :
    python test_module_states.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

from core import module_registry  # noqa: E402
from core.instance import InstanceConfig  # noqa: E402


def _manifeste(mid: str, core: bool = False) -> dict:
    return {
        "id": mid,
        "version": "1.0.0",
        "nom": mid.capitalize(),
        "icon": "Box",
        "description": f"module {mid}",
        "frontend": {"component": f"{mid}/Component.tsx"},
        "backend": {"prefix": ""},
        "core_module": core,
        "origin": "builtin",
        "status": "active",
        "removable": not core,
    }


class _FauxRouter:
    def __init__(self, mid: str):
        self.mid = mid


class _FauxApp:
    """Enregistre les montages au lieu de les faire."""

    def __init__(self):
        self.montés: list[tuple[str, str]] = []

    def include_router(self, router, prefix=""):
        self.montés.append((router.mid, prefix))


class BaseEtatsModules(unittest.TestCase):
    """Monte un faux dossier `modules/` et une config isolée."""

    #: Ordre alphabétique = ordre de discover_manifests (sorted(iterdir())).
    MODULES = ["admin", "chat", "hello", "settings", "zeta"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="epure-etats-"))
        self.modules_dir = self.tmp / "modules"
        self.modules_dir.mkdir()
        for mid in self.MODULES:
            self._installer(mid)

        self.config_file = self.tmp / "instance_config.json"
        self.legacy_file = self.tmp / "modules_state.json"
        self.cfg = InstanceConfig(path=self.config_file)

        patches = [
            mock.patch.object(module_registry, "_MODULES_DIR", self.modules_dir),
            # _legacy_state_file est une FONCTION depuis la bascule EPURE_DATA_DIR
            # (un chemin figé à l'import ignorerait la variable d'environnement) :
            # on remplace la fonction, pas une constante.
            mock.patch.object(
                module_registry, "_legacy_state_file", lambda: self.legacy_file
            ),
            mock.patch.object(module_registry, "instance_config", self.cfg),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _installer(self, mid: str, avec_router: bool = True, core: bool = False):
        d = self.modules_dir / mid
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(
            json.dumps(_manifeste(mid, core), ensure_ascii=False), encoding="utf-8"
        )
        if avec_router:
            (d / "router.py").write_text("router = None\n", encoding="utf-8")

    def _poser_liste(self, ids):
        """Écrit la liste DIRECTEMENT dans le JSON, sans passer par update().

        `InstanceConfig.update` réinjecte `settings` dans toute liste non vide
        (garde-fou volontaire). Pour rejouer un état hérité d'une version
        antérieure — y compris un état incohérent —, il faut écrire le fichier
        tel quel. C'est le point de départ réaliste d'une migration.
        """
        doc = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
        doc["modules_activés"] = list(ids)
        self.config_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self.cfg._cache = None  # force la relecture disque

    def _liste(self):
        return self.cfg.enabled_modules()

    def _poser_legacy(self, etat: dict):
        self.legacy_file.write_text(json.dumps(etat), encoding="utf-8")


class ConfigViergeTest(BaseEtatsModules):
    """Installation neuve : tout est actif, dans l'ordre de discover_manifests."""

    def test_config_vierge_active_tous_les_modules_installes(self):
        self.assertEqual(self._liste(), [], "la config neuve doit partir d'une liste vide")
        self.assertEqual(module_registry.active_ids(), self.MODULES)

    def test_config_vierge_status_tous_actifs(self):
        statuts = {m["id"]: m["status"] for m in module_registry.list_modules()}
        self.assertEqual(statuts, {mid: "active" for mid in self.MODULES})

    def test_migration_ecrit_la_liste_complete_dans_l_ordre(self):
        rapport = module_registry.migrate_module_state()
        self.assertEqual(self._liste(), self.MODULES)
        self.assertEqual(rapport["ajoutés"], self.MODULES)
        self.assertTrue(rapport["écrit"])


class MigrationTest(BaseEtatsModules):
    def test_id_fantome_purge(self):
        self._poser_liste(["chat", "snake", "admin", "astral"])
        rapport = module_registry.migrate_module_state()
        self.assertEqual(rapport["fantômes_purgés"], ["snake", "astral"])
        self.assertNotIn("snake", self._liste())
        self.assertNotIn("astral", self._liste())

    def test_module_installe_absent_ajoute_en_fin(self):
        self._poser_liste(["chat", "admin"])
        self._poser_legacy({})  # bascule : un ancien fichier d'état existe
        module_registry.migrate_module_state()
        # Les deux déjà présents gardent leur ordre ; les autres suivent en fin,
        # dans l'ordre de discover_manifests.
        self.assertEqual(self._liste(), ["chat", "admin", "hello", "settings", "zeta"])

    def test_ordre_utilisateur_preserve(self):
        """La migration ne réordonne pas ce qui est déjà là."""
        self._poser_liste(["zeta", "settings", "chat"])
        self._poser_legacy({})
        module_registry.migrate_module_state()
        self.assertEqual(self._liste()[:3], ["zeta", "settings", "chat"])

    def test_hors_bascule_un_installe_absent_reste_absent(self):
        """Régime établi : « absent » = désactivé par l'utilisateur, pas oublié.

        C'est ce qui empêche un redémarrage d'annuler toute désactivation.
        """
        self._poser_liste(["chat", "admin", "settings"])  # zeta et hello volontairement hors liste
        rapport = module_registry.migrate_module_state()
        self.assertEqual(rapport["ajoutés"], [])
        self.assertNotIn("zeta", self._liste())
        self.assertNotIn("hello", self._liste())
        self.assertFalse(rapport["écrit"])

    def test_hors_bascule_les_fantomes_sont_quand_meme_purges(self):
        """(b) tourne à chaque démarrage : un module effacé du disque doit sortir."""
        self._poser_liste(["chat", "admin", "settings", "disparu"])
        rapport = module_registry.migrate_module_state()
        self.assertEqual(rapport["fantômes_purgés"], ["disparu"])
        self.assertTrue(rapport["écrit"])
        self.assertEqual(self._liste(), ["chat", "admin", "settings"])

    def test_entree_disabled_honoree_puis_fichier_supprime(self):
        self._poser_liste(["chat", "zeta", "admin", "settings"])
        self._poser_legacy({"zeta": {"status": "disabled"}, "chat": {"status": "active"}})
        rapport = module_registry.migrate_module_state()

        self.assertEqual(rapport["désactivés_hérités"], ["zeta"])
        self.assertNotIn("zeta", self._liste(), "un module désactivé ne doit pas revenir")
        self.assertIn("chat", self._liste())
        self.assertFalse(self.legacy_file.exists(), "modules_state.json doit être supprimé")
        self.assertTrue(rapport["état_legacy_supprimé"])

    def test_disabled_absent_de_la_liste_n_est_pas_reajoute(self):
        """(a) exclut de (c) : sinon (c) réajouterait ce que (a) vient d'ôter."""
        self._poser_liste(["chat", "admin", "settings"])
        self._poser_legacy({"zeta": {"status": "disabled"}})
        module_registry.migrate_module_state()
        self.assertNotIn("zeta", self._liste())
        self.assertIn("hello", self._liste(), "les autres installés sont bien ajoutés")

    def test_settings_reinjecte_meme_si_desactive_dans_l_ancien_etat(self):
        self._poser_liste(["chat"])
        self._poser_legacy({"settings": {"status": "disabled"}})
        module_registry.migrate_module_state()
        self.assertIn("settings", self._liste())

    def test_doublons_dedoublonnes(self):
        self._poser_liste(["chat", "admin", "chat"])
        module_registry.migrate_module_state()
        self.assertEqual(self._liste().count("chat"), 1)

    def test_migration_idempotente(self):
        """Deux démarrages consécutifs : le second ne change rien et n'écrit pas.

        Le piège que ce test a réellement attrapé : (a) exclut `zeta` grâce à
        modules_state.json, que (d) supprime ensuite. Au second passage, plus
        rien ne disait que zeta était désactivé, et (c) le réintégrait.
        """
        self._poser_liste(["chat", "snake", "admin"])
        self._poser_legacy({"zeta": {"status": "disabled"}})

        premier = module_registry.migrate_module_state()
        apres_1 = self._liste()
        self.assertTrue(premier["écrit"])
        self.assertNotIn("zeta", apres_1)

        second = module_registry.migrate_module_state()
        apres_2 = self._liste()

        self.assertEqual(apres_1, apres_2, "la liste doit être stable au second passage")
        self.assertNotIn("zeta", apres_2, "un module désactivé ne doit pas revenir au reboot")
        self.assertFalse(second["écrit"], "le second passage ne doit rien écrire")
        self.assertEqual(second["fantômes_purgés"], [])
        self.assertEqual(second["ajoutés"], [])
        self.assertFalse(second["état_legacy_supprimé"])

    def test_desactivation_utilisateur_survit_au_redemarrage(self):
        """Le cas d'usage réel derrière le test d'idempotence."""
        module_registry.migrate_module_state()          # 1er démarrage
        module_registry.set_status("zeta", "disabled")  # l'utilisateur désactive
        self.assertNotIn("zeta", self._liste())

        module_registry.migrate_module_state()          # redémarrage
        self.assertNotIn("zeta", self._liste())
        self.assertEqual(module_registry.get_module("zeta")["status"], "disabled")

    def test_migration_idempotente_sur_config_vierge(self):
        module_registry.migrate_module_state()
        avant = self._liste()
        second = module_registry.migrate_module_state()
        self.assertEqual(avant, self._liste())
        self.assertFalse(second["écrit"])


class SetStatusTest(BaseEtatsModules):
    def setUp(self):
        super().setUp()
        module_registry.migrate_module_state()

    def test_desactiver_retire_de_la_liste(self):
        maj = module_registry.set_status("zeta", "disabled")
        self.assertIsNotNone(maj)
        self.assertEqual(maj["status"], "disabled")
        self.assertNotIn("zeta", self._liste())

    def test_reactiver_remet_en_fin(self):
        module_registry.set_status("chat", "disabled")
        module_registry.set_status("chat", "active")
        self.assertEqual(self._liste()[-1], "chat")

    def test_settings_indesactivable(self):
        self.assertIsNone(module_registry.set_status("settings", "disabled"))
        self.assertIn("settings", self._liste())
        self.assertEqual(module_registry.get_module("settings")["status"], "active")

    def test_module_inconnu_refuse(self):
        self.assertIsNone(module_registry.set_status("nexistepas", "active"))

    def test_status_invalide_refuse(self):
        self.assertIsNone(module_registry.set_status("chat", "peut-etre"))

    def test_settings_reinjecte_si_la_liste_le_perd(self):
        """Un patch client direct ne doit pas pouvoir débrancher les Réglages."""
        self.cfg.update({"modules_activés": ["chat", "admin"]})
        self.assertIn("settings", self.cfg.enabled_modules())
        self.assertIn("settings", module_registry.active_ids())


class RegisterRoutersTest(BaseEtatsModules):
    def _monter(self):
        app = _FauxApp()
        with mock.patch.object(
            module_registry.importlib,
            "import_module",
            side_effect=lambda nom: mock.Mock(router=_FauxRouter(nom.split(".")[1])),
        ):
            module_registry.register_routers(app)
        return [mid for mid, _ in app.montés]

    def test_monte_exactement_les_actifs(self):
        module_registry.migrate_module_state()
        module_registry.set_status("zeta", "disabled")
        module_registry.set_status("hello", "disabled")

        montés = self._monter()
        self.assertEqual(sorted(montés), ["admin", "chat", "settings"])
        self.assertNotIn("zeta", montés)
        self.assertNotIn("hello", montés)

    def test_config_vierge_monte_tout(self):
        self.assertEqual(sorted(self._monter()), sorted(self.MODULES))

    def test_module_sans_router_py_ignore(self):
        self._installer("sansrouter", avec_router=False)
        module_registry.migrate_module_state()
        self.assertIn("sansrouter", self._liste())
        self.assertNotIn("sansrouter", self._monter())

    def test_fantome_de_la_config_non_monte(self):
        self._poser_liste(["chat", "snake"])
        self.assertNotIn("snake", self._monter())


if __name__ == "__main__":
    unittest.main(verbosity=2)
