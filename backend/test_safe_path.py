#!/usr/bin/env python3
"""Tests des gardes de confinement de chemin.

Couvre les deux gardes symétriques :
  - ``codeagent._safe_path``            (racine : workspace)
  - ``module_workshop._modules_safe_path`` (racine : backend/modules)

Scénarios (identiques pour les deux via un contrat partagé) :
  - chemin interne légitime            → accepté
  - dossier frère « <racine>-autre/ »  → refusé  (piège de l'ancien startswith)
  - traversée « ../.. »                → refusée
  - traversée par symlink sortant      → refusée (skip si symlinks indisponibles)

Plus la dérivation portable du workspace (EPURE_WORKSPACE / défaut repo).

Usage :
    python test_safe_path.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Permettre d'importer le package `core` depuis le dossier backend.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole EPURE_DATA_DIR AVANT tout import de core.* / main

from core import codeagent
from core import module_workshop
from core.paths import resolve_workspace


class _GuardContract:
    """Scénarios communs. Les sous-classes fournissent :
    - ``set_root(path)`` : installe la racine confinée dans le module testé ;
    - ``guard(rel)``     : appelle la garde avec un chemin relatif."""

    def set_root(self, root: Path) -> None:  # pragma: no cover - override
        raise NotImplementedError

    def guard(self, rel: str) -> Path:  # pragma: no cover - override
        raise NotImplementedError

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.confined = self.root / "confined"
        self.confined.mkdir()
        # Dossier frère dont le nom PRÉFIXE celui de la racine : ce cas passait
        # à travers l'ancien `str(target).startswith(str(racine))`.
        (self.root / "confined-autre").mkdir()
        (self.root / "outside").mkdir()
        self.set_root(self.confined)

    def tearDown(self):
        self._tmp.cleanup()

    def test_internal_path_accepted(self):
        p = self.guard("sub/fichier.txt")
        self.assertTrue(p.is_relative_to(self.confined))

    def test_sibling_dir_rejected(self):
        # confined-autre/ : dossier frère, piège du startswith.
        with self.assertRaises(codeagent.SecurityError):
            self.guard("../confined-autre/secret.txt")

    def test_parent_traversal_rejected(self):
        with self.assertRaises(codeagent.SecurityError):
            self.guard("../../etc/passwd")

    def test_symlink_traversal_rejected(self):
        link = self.confined / "escape"
        try:
            link.symlink_to(self.root / "outside", target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest(f"symlinks indisponibles sur cette plateforme : {e}")
        with self.assertRaises(codeagent.SecurityError):
            self.guard("escape/secret.txt")


class SafePathTest(_GuardContract, unittest.TestCase):
    def set_root(self, root):
        self._orig = codeagent.WORKSPACE
        codeagent.WORKSPACE = root

    def tearDown(self):
        codeagent.WORKSPACE = self._orig
        super().tearDown()

    def guard(self, rel):
        return codeagent._safe_path(rel)


class ModulesSafePathTest(_GuardContract, unittest.TestCase):
    def set_root(self, root):
        # modules_dir est une FONCTION depuis la bascule EPURE_MODULES_DIR (un
        # chemin figé à l'import ignorerait la variable) : on remplace la
        # fonction, pas une constante.
        self._orig = module_workshop.modules_dir
        module_workshop.modules_dir = lambda: root

    def tearDown(self):
        module_workshop.modules_dir = self._orig
        super().tearDown()

    def guard(self, rel):
        return module_workshop._modules_safe_path(rel)


class ResolveWorkspaceTest(unittest.TestCase):
    """Dérivation portable du workspace (correctif n°1)."""

    def setUp(self):
        self._had = "EPURE_WORKSPACE" in os.environ
        self._prev = os.environ.get("EPURE_WORKSPACE")

    def tearDown(self):
        if self._had:
            os.environ["EPURE_WORKSPACE"] = self._prev
        else:
            os.environ.pop("EPURE_WORKSPACE", None)

    def test_default_is_repo_workspace(self):
        # backend/test_safe_path.py → backend/ → racine du repo → /workspace
        repo_root = Path(__file__).resolve().parent.parent
        os.environ.pop("EPURE_WORKSPACE", None)
        self.assertEqual(resolve_workspace(), (repo_root / "workspace").resolve())

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["EPURE_WORKSPACE"] = d
            self.assertEqual(resolve_workspace(), Path(d).resolve())


if __name__ == "__main__":
    unittest.main()
