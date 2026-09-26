#!/usr/bin/env python3
"""Aucun ``shell=True`` dans le code Python du dépôt (CLAUDE.md §6).

La règle existait, rien ne la tenait hors des trois sites de
``test_command_exec.py`` : ``epure_tray.py`` lançait encore ``npm run dev``
avec ``shell=True`` jusqu'au 2026-09-26, seulement pour que Windows trouve
``npm.cmd`` — ce que ``shutil.which`` fait sans shell.

Scan AST (pas de recherche de texte : un commentaire ou une docstring qui cite
la règle ne doit pas compter) de tout appel portant ``shell=True`` littéral,
sur les fichiers SUIVIS par git — les dossiers de données (``_backups/``,
``_staging/``) et les environnements virtuels n'en font pas partie.

Usage :
    python test_aucun_shell_true.py
"""

import ast
import os
import subprocess
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def _fichiers_python() -> list[Path]:
    sortie = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=_REPO, capture_output=True,
        text=True, encoding="utf-8", check=True,
    ).stdout
    return [_REPO / ligne for ligne in sortie.splitlines() if ligne.strip()]


def _appels_shell_true(source: str) -> list[int]:
    lignes = []
    for noeud in ast.walk(ast.parse(source)):
        if isinstance(noeud, ast.Call):
            for kw in noeud.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    lignes.append(noeud.lineno)
    return lignes


class AucunShellTrueTest(unittest.TestCase):
    def test_le_scan_voit_un_shell_true(self):
        """Pas de vert par vacuité : le détecteur trouve le motif interdit."""
        self.assertEqual(_appels_shell_true("import subprocess\nsubprocess.run('x', shell=True)\n"), [2])
        self.assertEqual(_appels_shell_true("# shell=True en commentaire\n"), [])

    def test_aucun_shell_true_dans_le_depot(self):
        fichiers = _fichiers_python()
        self.assertTrue(any(f.name == "epure_tray.py" for f in fichiers), "scan vide ?")
        fautifs = []
        for f in fichiers:
            if not f.is_file():
                continue  # supprimé de l'arbre de travail, pas encore de l'index
            source = f.read_text(encoding="utf-8-sig", errors="replace")
            try:
                lignes = _appels_shell_true(source)
            except SyntaxError:
                continue  # un fichier qui ne parse pas n'est pas ce que ce test mesure
            fautifs += [f"{f.relative_to(_REPO).as_posix()}:{n}" for n in lignes]
        self.assertEqual(fautifs, [], "shell=True interdit (CLAUDE.md §6)")


if __name__ == "__main__":
    os.chdir(_REPO)
    unittest.main()
