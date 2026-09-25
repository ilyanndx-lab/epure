"""`.gitignore` reste en UTF-8 sans BOM — incident du 2026-09-24.

**L'INCIDENT.** Une règle `Claude outputs/` ajoutée par
`echo "..." >> .gitignore` sous Windows PowerShell 5.1 ne s'appliquait pas : le
dossier restait visible dans `git status`, et `git diff` annonçait
`.gitignore | Bin` — git voyait le fichier comme BINAIRE.

**LE MÉCANISME.** Sous 5.1, `>>` est `Out-File -Append`, dont l'encodage par
défaut est UTF-16 LE (« Unicode »). La ligne ajoutée arrive donc octet par octet
entrelacée de `00`, au milieu d'un fichier UTF-8 : git ne reconnaît pas la
règle, et un seul octet NUL suffit pour qu'il classe le fichier en binaire, ce
qui cache le défaut dans tous les diffs. Rien n'échoue : c'est un fichier
d'ignorés qui ignore un peu moins, en silence. `pwsh 7` écrit de l'UTF-8 sans
BOM et ne le reproduit pas — d'où un piège propre au raccourci de bureau et aux
consoles par défaut de Windows.

Le test lit les octets : pas de NUL, pas de BOM (UTF-8 ou UTF-16), décodable en
UTF-8 strict. Coût : une lecture de 8 Ko.

Usage :
    python test_encodage_gitignore.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401,E402  -- avant tout import core.*

from core.paths import REPO_ROOT  # noqa: E402

#: Les `.gitignore` VERSIONNÉS. Celui de la racine est celui de l'incident ; les
#: autres sont écrits par les mêmes mains, avec la même console.
FICHIERS = [p for p in (REPO_ROOT / ".gitignore", REPO_ROOT / "frontend" / ".gitignore")
            if p.is_file()]


class EncodageGitignoreTest(unittest.TestCase):

    def test_il_y_a_bien_un_gitignore(self):
        """Garde-fou du garde-fou : une liste vide passerait le test ci-dessous."""
        self.assertIn(REPO_ROOT / ".gitignore", FICHIERS)

    def test_utf8_sans_bom_ni_octet_nul(self):
        for chemin in FICHIERS:
            with self.subTest(fichier=str(chemin.relative_to(REPO_ROOT))):
                octets = chemin.read_bytes()
                self.assertNotIn(b"\x00", octets,
                                 "octet NUL : ligne ajoutée en UTF-16 (`>>` sous PowerShell 5.1)")
                for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
                    self.assertFalse(octets.startswith(bom), f"BOM {bom!r} en tête")
                octets.decode("utf-8")  # strict : lève sur un octet invalide


if __name__ == "__main__":
    unittest.main(verbosity=2)
