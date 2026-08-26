#!/usr/bin/env python3
"""`tools/mettre-a-jour-epure.ps1` — l'archive s'applique, elle ne s'imbrique pas.

**L'INCIDENT.** Sur la machine du destinataire, `git` est inutilisable (Smart App
Control bloque `git-remote-https.exe` et `libcurl-4.dll`, durablement). La mise à
jour du code passe donc par l'archive `main.zip` de GitHub. Or
`Expand-Archive -DestinationPath .` lancé DEPUIS le dossier `epure\\` ne remplace
pas son contenu : il y crée un sous-dossier `epure-main\\`.

Et rien ne le signale. `npm install` réussit, `faire_paquet.py` réussit,
l'installation réussit — sur l'ANCIEN code. Le destinataire reçoit exactement le
paquet qu'il avait déjà, et la seule façon de s'en apercevoir est de constater que
le comportement de l'application n'a pas changé. C'est la pire forme d'échec :
celle qui rend un succès.

**CE QUE CES TESTS GARDENT.** Pas « Appliquer-Archive fonctionne » — ça, un seul
test le dirait. Trois propriétés, dont chacune a coûté quelque chose quelque part
dans ce dépôt :

1. **Les fichiers arrivent au bon NIVEAU**, et aucun `epure-main\\` ne subsiste.
2. **On écrase, on ne supprime jamais.** C'est ce qui protège les données du
   destinataire — `backend/.env`, `memory/`, `vector_db/`, les 90 Mo du modèle
   d'embedding, `node_modules/` — et non la liste `$HORS_MISE_A_JOUR`, qui ne
   compare que des noms de premier niveau. Un test qui vérifierait la liste sans
   vérifier la propriété se tromperait de garantie.
3. **Une archive qui n'est pas le dépôt est refusée AVANT d'écrire.** Une page
   d'erreur HTML servie par un proxy captif et enregistrée sous `.zip`, une
   archive tronquée : sans ce contrôle, on écrase la racine avec n'importe quoi.

Avec un cas de **CONTRÔLE** qui rejoue l'idiome naïf et vérifie qu'il produit
bien l'imbrication — sans lui, ce fichier pourrait passer en ne mesurant rien.

**Les tests de comportement jouent sous `powershell.exe`**, pas `pwsh` : c'est
l'hôte que lance `Mettre-A-Jour-Epure.cmd`, donc le seul dont le comportement
compte. Ils extraient la fonction du VRAI script au lieu de la recopier — un test
qui recopie le code qu'il vérifie ne vérifie que sa copie.

Usage :
    python test_mise_a_jour.py
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — avant tout import core.*

from core.paths import REPO_ROOT  # noqa: E402

PS1 = REPO_ROOT / "tools" / "mettre-a-jour-epure.ps1"
CMD = REPO_ROOT / "tools" / "Mettre-A-Jour-Epure.cmd"

#: `powershell.exe` et NON `pwsh` : c'est l'hôte du `.cmd`, et les trois pièges
#: que ce script contourne n'existent que chez lui.
POWERSHELL = "powershell.exe"


def powershell_51():
    if platform.system() != "Windows":
        return None
    return shutil.which(POWERSHELL)


_PS = powershell_51()
_RAISON = f"{POWERSHELL} absent (hors Windows)"


def extraire_fonction(nom: str) -> str:
    """Le corps de la fonction TEL QU'IL EST DANS LE SCRIPT."""
    texte = PS1.read_text(encoding="ascii").replace("\r\n", "\n")
    debut = texte.index(f"function {nom} {{")
    fin = texte.index("\n}", debut) + 2
    corps = texte[debut:fin]
    assert corps.count("{") > 1, f"extraction de {nom} suspecte"
    return corps


def extraire_liste(nom: str) -> list[str]:
    """Le contenu d'un tableau `$NOM = @( … )` du script."""
    import re
    texte = PS1.read_text(encoding="ascii").replace("\r\n", "\n")
    m = re.search(rf"\${nom}\s*=\s*@\((.*?)\n\)", texte, re.DOTALL)
    assert m, f"tableau ${nom} introuvable"
    return re.findall(r"'([^']+)'", m.group(1))


def fabriquer_archive(chemin: Path, sommet: str = "epure-main",
                      entrees: dict[str, str] | None = None) -> Path:
    """Une archive de la forme exacte de celles de GitHub : UN dossier au sommet.

    C'est cette forme qui crée le piège — sans elle, `Expand-Archive` dans le
    dossier courant ne serait pas dangereux.
    """
    entrees = entrees if entrees is not None else {
        "backend/marqueur.txt": "code neuf",
        "backend/core/rag.py": "# neuf",
        "tools/faire_paquet.py": "# neuf",
        "README.md": "NEUF",
    }
    with zipfile.ZipFile(chemin, "w") as z:
        for rel, contenu in entrees.items():
            z.writestr(f"{sommet}/{rel}", contenu)
    return chemin


def fabriquer_racine(dossier: Path) -> None:
    """Une racine de travail réaliste : du code ancien ET des données locales."""
    (dossier / "backend" / "core").mkdir(parents=True)
    (dossier / "backend" / "memory").mkdir(parents=True)
    (dossier / "backend" / "embedding_model").mkdir(parents=True)
    (dossier / "frontend" / "node_modules").mkdir(parents=True)
    (dossier / "dist-paquets").mkdir()
    (dossier / "data").mkdir()
    (dossier / "backend" / "core" / "rag.py").write_text("# ancien", encoding="ascii")
    (dossier / "backend" / "ancien_fichier.py").write_text("# ancien", encoding="ascii")
    (dossier / "README.md").write_text("ANCIEN", encoding="ascii")
    # Les données du destinataire, que rien ne doit toucher.
    (dossier / "backend" / ".env").write_text("EPURE_API_TOKEN=secret", encoding="ascii")
    (dossier / "backend" / "memory" / "session.json").write_text("{}", encoding="ascii")
    (dossier / "backend" / "embedding_model" / "model.onnx").write_text("90Mo", encoding="ascii")
    (dossier / "frontend" / "node_modules" / "marqueur").write_text("x", encoding="ascii")
    (dossier / "dist-paquets" / "epure-sandr.zip").write_text("vieux paquet", encoding="ascii")
    (dossier / "data" / "fiche.pdf").write_text("PDF", encoding="ascii")


#: Harnais : extrait la fonction du VRAI script, pose la liste de premier niveau,
#: et l'applique. Rien d'autre du script n'est exécuté.
_HARNAIS = """param([string]$Script, [string]$Zip, [string]$Racine)
$ErrorActionPreference = 'Stop'
function Note([string]$t) { Write-Host "note $t" }
$src = Get-Content -LiteralPath $Script -Raw
$d = $src.IndexOf('function Appliquer-Archive {')
$f = $src.IndexOf("`n}", $d)
Invoke-Expression $src.Substring($d, $f - $d + 2)
$d = $src.IndexOf('$HORS_MISE_A_JOUR = @(')
$f = $src.IndexOf(')', $d)
Invoke-Expression $src.Substring($d, $f - $d + 1)
$n = Appliquer-Archive -Zip $Zip -Racine $Racine
Write-Host "entrees=$n"
"""

#: L'idiome NAIF, celui qui a produit l'incident. Cas de controle.
_NAIF = """param([string]$Zip, [string]$Racine)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $Racine
Expand-Archive -LiteralPath $Zip -DestinationPath . -Force
Write-Host 'fait'
"""


class _SousPowerShell(unittest.TestCase):
    """Socle : une racine, une archive, et de quoi jouer un .ps1."""

    def setUp(self):
        if _PS is None:
            self.skipTest(_RAISON)
        self._tmp = tempfile.mkdtemp(prefix="epure-test-maj-")
        self.racine = Path(self._tmp) / "epure"
        self.racine.mkdir()
        fabriquer_racine(self.racine)
        self.zip = fabriquer_archive(Path(self._tmp) / "main.zip")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def jouer(self, corps: str, *arguments: str) -> str:
        chemin = Path(self._tmp) / (self.id().rsplit(".", 1)[-1] + ".ps1")
        chemin.write_text(corps, encoding="ascii")
        res = subprocess.run(
            [_PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(chemin),
             *arguments],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
        return res.stdout + res.stderr

    def appliquer(self) -> str:
        return self.jouer(_HARNAIS, "-Script", str(PS1), "-Zip", str(self.zip),
                          "-Racine", str(self.racine))

    def imbrique(self) -> list[Path]:
        return [p for p in self.racine.rglob("epure-main")]


class ImbricationTest(_SousPowerShell):
    """LE piège, et sa preuve."""

    def test_l_archive_s_applique_au_bon_niveau(self):
        sortie = self.appliquer()
        self.assertIn("entrees=", sortie, sortie)
        marqueur = self.racine / "backend" / "marqueur.txt"
        self.assertTrue(marqueur.is_file(), sortie)
        self.assertEqual("code neuf", marqueur.read_text(encoding="ascii"))

    def test_aucun_dossier_imbrique_ne_subsiste(self):
        self.appliquer()
        self.assertEqual([], self.imbrique(),
                         "un dossier epure-main est resté : les étapes suivantes "
                         "tourneraient sur l'ancien code")

    def test_le_code_existant_est_bien_remplace(self):
        """Sans ça, « pas d'imbrication » pourrait vouloir dire « rien n'a été
        copié » — et le test précédent passerait sur une fonction qui ne fait
        rien.
        """
        avant = (self.racine / "backend" / "core" / "rag.py").read_text(encoding="ascii")
        self.assertEqual("# ancien", avant)
        self.appliquer()
        apres = (self.racine / "backend" / "core" / "rag.py").read_text(encoding="ascii")
        self.assertEqual("# neuf", apres)
        self.assertEqual("NEUF", (self.racine / "README.md").read_text(encoding="ascii"))

    def test_l_idiome_naif_produit_bien_l_imbrication(self):
        """Cas de CONTRÔLE. Sans lui, tout ce fichier pourrait passer sur une
        machine où `Expand-Archive` se comporterait autrement, et annoncer une
        protection qu'il ne mesure pas.
        """
        self.jouer(_NAIF, "-Zip", str(self.zip), "-Racine", str(self.racine))
        self.assertNotEqual([], self.imbrique(),
                            "l'idiome naif ne produit plus l'imbrication : "
                            "ce test ne prouve plus rien")
        self.assertFalse((self.racine / "backend" / "marqueur.txt").exists(),
                         "le code neuf serait arrivé au bon endroit, malgré tout")


class DonneesPreserveesTest(_SousPowerShell):
    """On écrase, on ne supprime jamais — la vraie protection des données."""

    def test_les_donnees_du_destinataire_survivent(self):
        self.appliquer()
        attendus = {
            "backend/.env": "EPURE_API_TOKEN=secret",
            "backend/memory/session.json": "{}",
            "backend/embedding_model/model.onnx": "90Mo",
            "frontend/node_modules/marqueur": "x",
            "dist-paquets/epure-sandr.zip": "vieux paquet",
            "data/fiche.pdf": "PDF",
        }
        for rel, contenu in attendus.items():
            with self.subTest(fichier=rel):
                chemin = self.racine / rel
                self.assertTrue(chemin.is_file(), f"{rel} a disparu")
                self.assertEqual(contenu, chemin.read_text(encoding="ascii"))

    def test_un_fichier_retire_en_amont_survit_lui_aussi(self):
        """La contrepartie ASSUMÉE de « on ne supprime jamais », affirmée ici
        pour qu'elle ne soit pas une surprise : un fichier retiré du dépôt reste
        sur le disque du destinataire. C'est le prix de ne jamais risquer ses
        données, et il est moins cher que l'inverse.
        """
        self.appliquer()
        self.assertTrue((self.racine / "backend" / "ancien_fichier.py").is_file())

    def test_la_liste_ne_contient_que_des_noms_de_premier_niveau(self):
        """`$HORS_MISE_A_JOUR` est comparée à `$entree.Name`. Y écrire
        `backend\\memory` donnerait une ligne qui ne compare jamais rien — et qui
        se lirait pourtant comme une garantie. Les données imbriquées sont
        protégées par « on ne supprime jamais », pas par cette liste.
        """
        for nom in extraire_liste("HORS_MISE_A_JOUR"):
            with self.subTest(entree=nom):
                self.assertNotIn("\\", nom)
                self.assertNotIn("/", nom)


class ArchiveRefuseeTest(_SousPowerShell):
    """Ce qui n'est pas le dépôt ne doit pas écraser la racine."""

    def test_une_archive_sans_dossier_unique_est_refusee(self):
        zip2 = Path(self._tmp) / "plat.zip"
        with zipfile.ZipFile(zip2, "w") as z:
            z.writestr("backend/marqueur.txt", "x")
            z.writestr("autre/chose.txt", "y")
        self.zip = zip2
        sortie = self.appliquer()
        self.assertNotIn("entrees=", sortie)
        self.assertIn("dossier unique", sortie)

    def test_une_archive_qui_n_est_pas_le_depot_est_refusee(self):
        self.zip = fabriquer_archive(Path(self._tmp) / "faux.zip",
                                     entrees={"index.html": "<html>403</html>"})
        sortie = self.appliquer()
        self.assertNotIn("entrees=", sortie)
        self.assertIn("backend", sortie)

    def test_le_refus_a_lieu_avant_toute_ecriture(self):
        """Refuser après avoir écrasé la moitié de la racine ne vaudrait rien."""
        self.zip = fabriquer_archive(Path(self._tmp) / "faux.zip",
                                     entrees={"index.html": "<html>403</html>"})
        self.appliquer()
        self.assertEqual("ANCIEN",
                         (self.racine / "README.md").read_text(encoding="ascii"))
        self.assertFalse((self.racine / "index.html").exists())


class FormeTest(unittest.TestCase):
    """Les gardes déjà établies pour les scripts de ce dépôt, appliquées ici.

    Pas de PowerShell nécessaire : ce sont des propriétés du texte.
    """

    def test_les_deux_fichiers_existent(self):
        self.assertTrue(PS1.is_file(), PS1)
        self.assertTrue(CMD.is_file(), CMD)

    def test_ascii_pur(self):
        """cp1252 : cf. `test_encodage_scripts.py`, qui tient la règle pour tous
        les scripts versionnés. Redit ici parce qu'un échec sur CE fichier doit
        nommer CE fichier.
        """
        for chemin in (PS1, CMD):
            with self.subTest(fichier=chemin.name):
                fautifs = [i for i, o in enumerate(chemin.read_bytes()) if o > 127]
                self.assertEqual([], fautifs[:5])

    def test_aucun_code_python_en_ligne_de_commande(self):
        """Le piège 3 : `powershell.exe` 5.1 ampute les guillemets internes d'un
        argument natif. On appelle des FICHIERS .py.
        """
        code = self._lignes_de_code(PS1.read_text(encoding="ascii"))
        for ligne in code:
            self.assertNotIn("'-c'", ligne)
            self.assertNotIn('"-c"', ligne)
        self.assertIn("faire_paquet.py", PS1.read_text(encoding="ascii"))

    @staticmethod
    def _lignes_de_code(texte: str) -> list[str]:
        """Ni `#`, ni l'interieur d'un bloc `<# ... #>`.

        Le premier filtre ne suffit pas : la prose d'un bloc de commentaire ne
        commence pas par `#`, et l'en-tete de ce script DECRIT les pieges qu'il
        evite -- il cite donc `2>&1` et `-c` sans les employer. Un test qui
        confondrait les deux echouerait sur la documentation.
        """
        code, dedans = [], False
        for ligne in texte.splitlines():
            nu = ligne.strip()
            if nu.startswith("<#"):
                dedans = True
            if dedans:
                if "#>" in nu:
                    dedans = False
                continue
            if nu.startswith("#") or not nu:
                continue
            code.append(nu)
        return code

    def test_les_appels_natifs_passent_par_invoquer_externe(self):
        """Le piège 2 : une redirection `2>&1` sur un binaire natif sous
        `EAP=Stop` tue le script sur une ligne de stderr, même quand le binaire
        sort en 0. Une seule redirection dans tout le fichier, dans le helper.
        """
        code = self._lignes_de_code(PS1.read_text(encoding="ascii"))
        redirections = [l for l in code if "2>&1" in l]
        self.assertEqual(1, len(redirections), redirections)
        self.assertIn("$Binaire @Arguments", redirections[0])

    def test_le_script_s_arrete_a_la_premiere_etape_qui_echoue(self):
        """`Abandonner` sort en 1 et nomme l'étape. Un script qui continue sur un
        état incertain fait perdre plus de temps qu'il n'en gagne.
        """
        texte = PS1.read_text(encoding="ascii")
        self.assertIn("exit 1", texte)
        self.assertIn("ECHEC a l etape", texte)
        self.assertIn("$ErrorActionPreference = 'Stop'", texte)

    def test_le_cmd_garde_la_fenetre_ouverte(self):
        """Un `.cmd` double-cliqué ferme sa console dès qu'il rend la main. Sans
        `pause`, le destinataire ne peut ni lire le résultat ni le copier — donc
        perd exactement ce que ce script existe pour lui donner.
        """
        texte = CMD.read_text(encoding="ascii")
        self.assertGreaterEqual(texte.count("pause"), 1)
        self.assertIn("-ExecutionPolicy Bypass", texte)
        self.assertIn("mettre-a-jour-epure.ps1", texte)

    def test_le_cmd_lance_powershell_et_non_pwsh(self):
        """`pwsh` n'est pas installé partout ; `powershell` l'est. Le .ps1
        contourne explicitement les trois défauts de 5.1.
        """
        commandes = [l.strip() for l in CMD.read_text(encoding="ascii").splitlines()
                     if l.strip() and not l.strip().lower().startswith(("rem", "echo"))]
        lanceuses = [l for l in commandes if "powershell" in l or "pwsh" in l]
        self.assertEqual(1, len(lanceuses), lanceuses)
        self.assertIn("powershell -NoProfile", lanceuses[0])
        self.assertNotIn("pwsh", lanceuses[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
