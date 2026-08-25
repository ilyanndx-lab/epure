"""Les scripts PowerShell du depot restent en ASCII pur.

**L'INCIDENT.** `tools/dev-epure.ps1` ne demarrait plus du tout : 33 erreurs de
parsing, la premiere annoncee ligne 253, puis une cascade de "Bloc d'instruction
manquante dans la clause de l'instruction switch" a partir de la ligne 481. La
ligne 253 etait strictement ASCII et sans defaut -- elle ne faisait que subir un
decalage ouvert bien plus haut.

**LE MECANISME**, et c'est lui qui justifie une regle et non une relecture.
Windows PowerShell 5.1 (`powershell.exe`, celui que lance un raccourci de bureau)
lit un `.ps1` sans BOM avec la page de code du systeme -- Windows-1252, pas
UTF-8. Un caractere hors ASCII y arrive donc mojibake, et DEUX d'entre eux ne
sont pas seulement illisibles :

    tiret cadratin  U+2014  = E2 80 94  ->  cp1252 : a-circonflexe, euro, U+201D
    filet           U+2500  = E2 94 80  ->  cp1252 : a-circonflexe, U+201D, euro

Ce U+201D est un guillemet-fermant typographique, et **PowerShell le traite comme
un delimiteur de chaine a l'egal du guillemet droit**. Une chaine ouverte par `"`
peut donc etre fermee par lui.

Mesure sur le fichier fautif : 501 filets et 8 cadratins. Les 501 filets etaient
tous dans des commentaires, donc inoffensifs (un commentaire court jusqu'a la fin
de la ligne). Le cadratin d'UNE ligne, lui, etait dans une chaine `"..."` : relu
en cp1252 il la fermait, apres quoi l'apostrophe de "l'etape" ouvrait une chaine
simple qui courait sur le reste du fichier. Corriger ce seul caractere ramenait
33 erreurs a 30 et faisait disparaitre celle de la ligne 253. Le meme fichier lu
par pwsh 7 (UTF-8 par defaut) parsait sans une seule erreur -- la syntaxe n'avait
jamais ete en cause.

**POURQUOI L'ASCII ET PAS UN BOM.** Un BOM corrigerait la lecture, et c'est la
reponse canonique de Windows. Ce depot a deja paye le prix des BOM ailleurs
(`core/jsonstore.py` lit en `utf-8-sig` parce qu'un BOM pose par PowerShell 5.1
rendait la memoire de session invisible), et l'ASCII, lui, marche sous n'importe
quelle page de code, avec ou sans BOM. `install.ps1`, `tools/installer-epure.ps1`
et `tools/Installer-Epure.cmd` le tenaient deja ; `tools/dev-epure.ps1`, arrive
plus tard, ne le tenait pas.

**CE QUE CE TEST NE COUVRE PAS.** Les `.py` : Python lit ses sources en UTF-8 par
defaut (PEP 3120), l'accent y est sans danger. Seuls les scripts confies a
`powershell.exe` ou `cmd.exe` sont concernes.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  -- avant tout import core.*

from core.paths import REPO_ROOT

#: Extensions des scripts lus par un interpreteur Windows sensible a la page de
#: code. `.cmd`/`.bat` sont dans le lot : `cmd.exe` a exactement le meme defaut.
EXTENSIONS = (".ps1", ".cmd", ".bat")

#: Arbres a ne pas parcourir, quand `git` n'est pas la pour le dire (cf.
#: `scripts()`). `node_modules` pese des dizaines de milliers de fichiers dont
#: `npm` genere lui-meme les `.ps1`, et `_backups` garde des versions anciennes
#: de modules generes, qu'on ne corrigera pas.
IGNORES = {".git", "node_modules", "dist", "_backups", "build", "__pycache__",
           ".venv", "venv", "site-packages"}

#: Les caracteres que cp1252 fait naitre a partir d'un octet 0x82-0x9D et que
#: PowerShell accepte comme delimiteur de chaine. C'est la classe qui transforme
#: un probleme d'affichage en probleme de parsing.
GUILLEMETS_CP1252 = {
    "\u2018", "\u2019", "\u201a", "\u201b",   # simples
    "\u201c", "\u201d", "\u201e",             # doubles
}


def scripts() -> list[Path]:
    """Les scripts d'interpreteur Windows QUE LE DEPOT PORTE.

    `git ls-files` et non un parcours du disque, et ce n'est pas un detail de
    confort : un parcours attrape `start.ps1`, un residu local que `.gitignore`
    exclut depuis qu'il a ete retire du depot (il portait un chemin absolu, cf.
    CLAUDE.md 10) -- et il porte un BOM. Le test echouait donc sur un fichier
    que personne ne livre et que ce depot a deja decide de ne plus suivre.
    L'invariant porte sur ce qui est VERSIONNE, pas sur ce qui traine.

    Repli sur le parcours quand `git` manque ou que ce n'est pas un checkout
    (export d'archive) : mieux vaut verifier trop que ne rien verifier.
    """
    try:
        sortie = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "*.ps1", "*.cmd", "*.bat"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if sortie.returncode == 0:
            relatifs = [x for x in sortie.stdout.split(chr(0)) if x]
            return sorted(REPO_ROOT / x for x in relatifs)
    except (OSError, subprocess.SubprocessError):
        pass
    trouves = []
    for racine, dossiers, fichiers in os.walk(REPO_ROOT):
        dossiers[:] = [d for d in dossiers if d not in IGNORES]
        for nom in fichiers:
            if nom.lower().endswith(EXTENSIONS):
                trouves.append(Path(racine) / nom)
    return sorted(trouves)


class EncodageDesScripts(unittest.TestCase):

    def test_il_y_a_bien_des_scripts_a_verifier(self):
        """Garde-fou du garde-fou : une liste vide passerait tous les tests.

        C'est la panne silencieuse classique d'un test qui parcourt un arbre --
        une exclusion trop large, un `REPO_ROOT` qui bouge, et il ne verifie
        plus rien en restant vert.
        """
        noms = {p.name for p in scripts()}
        self.assertIn("dev-epure.ps1", noms)
        self.assertIn("installer-epure.ps1", noms)
        self.assertGreaterEqual(len(noms), 4)

    def test_aucun_caractere_hors_ascii(self):
        for chemin in scripts():
            octets = chemin.read_bytes()
            fautifs = [(i, o) for i, o in enumerate(octets) if o > 127]
            if fautifs:
                position = fautifs[0][0]
                ligne = octets[:position].count(b"\n") + 1
                texte = octets.decode("utf-8", errors="replace")
                caracteres = sorted({c for c in texte if ord(c) > 127})
                self.fail(
                    f"{chemin.relative_to(REPO_ROOT)} : {len(fautifs)} octets hors "
                    f"ASCII, le premier ligne {ligne}. Caracteres en cause : "
                    f"{[f'U+{ord(c):04X} {c}' for c in caracteres[:8]]}. "
                    "powershell.exe lira ce fichier en Windows-1252 : voir le "
                    "docstring de ce module, un tiret cadratin ou un filet y "
                    "devient un guillemet et casse le parsing du RESTE du fichier."
                )

    def test_relu_en_cp1252_aucun_guillemet_ne_nait(self):
        """Le test qui nomme la cause, pas seulement la regle.

        Redondant avec le precedent aujourd'hui -- un fichier ASCII pur ne peut
        rien faire naitre. Il est la pour le jour ou quelqu'un decidera qu'un
        accent est acceptable : celui-la l'est sans doute (`e-aigu` -> `A-tilde,
        copyright`, inoffensif), mais le message doit dire lesquels ne le sont
        pas, et le prouver plutot que de l'affirmer.
        """
        for chemin in scripts():
            mojibake = chemin.read_bytes().decode("cp1252", errors="replace")
            nes = sorted({c for c in mojibake if c in GUILLEMETS_CP1252})
            self.assertEqual(
                [], nes,
                f"{chemin.relative_to(REPO_ROOT)} : relu par powershell.exe en "
                f"Windows-1252, ce fichier fait naitre {[f'U+{ord(c):04X}' for c in nes]}, "
                "que PowerShell traite comme un delimiteur de chaine."
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
