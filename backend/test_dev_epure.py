"""Un avertissement sur stderr ne doit pas tuer `tools/dev-epure.ps1`.

**L'INCIDENT.** Le lanceur de developpement s'arretait a l'etape 5 avec
"NativeCommandError" alors que le build REUSSISSAIT -- `dist/index.html` ecrit,
"built in 2.89s". Ce qui le tuait etait l'avertissement de taille de chunk que
Vite/Rolldown ecrit sur stderr (deux chunks au-dela de 500 ko), c'est-a-dire une
ligne de texte parfaitement benigne. Consequence reelle : plus aucun demarrage en
developpement, pas seulement un faux message affiche.

**LE MECANISME**, qui ne se voit pas en relisant le site d'appel. Le script pose
`$ErrorActionPreference = 'Stop'`, et il le doit : c'est un lanceur dont le but
est de s'arreter net et nomme. Or sous **Windows PowerShell 5.1**
(`powershell.exe`, l'hote que lance le raccourci de bureau -- pas `pwsh`), une
redirection `2>&1` sur un binaire NATIF convertit chaque ligne de son stderr en
`ErrorRecord`, et 'Stop' en fait une erreur TERMINANTE. Le script meurt donc sur
du stderr, meme quand le binaire sort ensuite avec le code 0 -- et la ligne
suivante, le `if ($LASTEXITCODE -ne 0)` ecrit exactement pour decider de l'echec,
n'est jamais atteinte. L'intention etait bonne depuis le debut ; c'est l'hote qui
ne laissait pas la tenir.

Les trois cas, mesures hors du depot avec un binaire qui ecrit une ligne sur
stderr puis sort en **0** :

    powershell.exe, `& bin 2>&1`, EAP='Stop'   -> meurt (NativeCommandError)
    powershell.exe, `& bin` sans redirection   -> survit
    pwsh 7, `& bin 2>&1`, EAP='Stop'           -> survit

D'ou l'invisibilite du bug : il n'existe que dans l'hote reellement utilise.

**LES SIX SITES ETAIENT ARMES**, pas seulement le build : `git pull` ecrit sa
progression sur stderr, `npm ci` ses avis, `taskkill` ses refus, un interpreteur
Python sans fastapi sa trace -- le message "ecarte (dependances absentes)" de
`Trouver-Python` etait litteralement inatteignable. La correction est donc un
point unique, `Invoquer-Externe`, et ce que ce fichier verrouille est autant
cette unicite que le comportement.

**CE QUE CE TEST NE FAIT PAS** : jouer `npm run build`. Il n'a pas besoin de node
ni de node_modules -- il extrait la fonction du vrai script et l'eprouve contre un
binaire dont il choisit le code de sortie et le bruit. Le cas de controle
(l'ancien idiome) est la pour prouver que le montage mesure bien quelque chose.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  -- avant tout import core.*

from core.paths import REPO_ROOT

SCRIPT = REPO_ROOT / "tools" / "dev-epure.ps1"

#: `powershell.exe` et NON `pwsh` : c'est tout le sujet. Un test qui passerait
#: sous pwsh ne dirait rien de l'hote qui casse.
POWERSHELL = "powershell.exe"

#: Le binaire bruyant du montage : une ligne sur stderr, une sur stdout, et le
#: code de sortie passe en argument.
BRUYANT = "\n".join([
    "import sys",
    "sys.stderr.write('avertissement benin' + chr(10))",
    "sys.stdout.write('resultat utile' + chr(10))",
    "sys.exit(int(sys.argv[1]))",
    "",
])


def powershell_51():
    return shutil.which(POWERSHELL)


def extraire_fonction(nom):
    """Le corps de la fonction TEL QU'IL EST DANS LE SCRIPT.

    Extraire plutot que retaper : un test qui recopie le code qu'il verifie ne
    verifie que sa copie. L'accolade fermante est cherchee en colonne 0, ce qui
    est la mise en forme de ce fichier -- si elle change, l'extraction leve au
    lieu de passer silencieusement sur du vide.
    """
    texte = SCRIPT.read_text(encoding="utf-8")
    debut = texte.index("function " + nom + " {")
    fin = texte.index("\n}", debut) + 2
    corps = texte[debut:fin]
    assert corps.count("{") > 1, "extraction de " + nom + " suspecte"
    return corps


class _Harnais(unittest.TestCase):
    """Outillage commun : un binaire bruyant, et un .ps1 jetable a jouer."""

    @classmethod
    def setUpClass(cls):
        if not powershell_51():
            raise unittest.SkipTest(POWERSHELL + " absent (hors Windows)")
        cls._dossier = tempfile.mkdtemp(prefix="epure-test-devepure-")
        cls.bruyant = Path(cls._dossier) / "bruyant.py"
        cls.bruyant.write_text(BRUYANT, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dossier, ignore_errors=True)

    def jouer(self, corps):
        """Joue un .ps1 sous Windows PowerShell 5.1, EAP='Stop' pose d'entree."""
        chemin = Path(self._dossier) / (self.id().rsplit(".", 1)[-1] + ".ps1")
        chemin.write_text("$ErrorActionPreference = 'Stop'\n" + corps + "\n",
                          encoding="ascii")
        return subprocess.run(
            [powershell_51(), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(chemin)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)

    def appel(self, code_sortie):
        """L'appel du binaire bruyant, chemins en slash avant (PowerShell les
        accepte, et ca evite d'echapper des antislash dans un .ps1 genere).
        """
        return '"{0}" "{1}" {2}'.format(Path(sys.executable).as_posix(),
                                        self.bruyant.as_posix(), code_sortie)


class LeMontageMesureBienQuelqueChose(_Harnais):
    """Cas de CONTROLE : l'ancien idiome doit encore mourir.

    Sans lui, tout ce fichier pourrait passer sur une machine ou le probleme
    n'existe pas, et annoncer une protection qu'il ne mesure pas.
    """

    def test_ancien_idiome_meurt_sur_stderr_malgre_un_code_0(self):
        res = self.jouer("$s = & " + self.appel(0) + " 2>&1\n"
                         "Write-Host 'ATTEINT'")
        self.assertNotIn(
            "ATTEINT", res.stdout,
            "l'ancien idiome ne meurt plus : ce fichier ne prouve plus rien")
        self.assertIn("NativeCommandError", res.stdout + res.stderr)

    def test_sans_redirection_le_meme_appel_survit(self):
        """La redirection est bien la condition -- pas le stderr a lui seul."""
        res = self.jouer("$s = & " + self.appel(0) + "\n"
                         "Write-Host 'ATTEINT'")
        self.assertIn("ATTEINT", res.stdout)


class InvoquerExterne(_Harnais):

    def corps(self, code_sortie):
        return "\n".join([
            extraire_fonction("Invoquer-Externe"),
            "$r = Invoquer-Externe " + self.appel(code_sortie),
            'Write-Host "CODE=$($r.Code)"',
            'Write-Host "STDERR_VU=$($r.Texte -match ([regex]::Escape(\'avertissement benin\')))"',
            'Write-Host "STDOUT_VU=$($r.Texte -match ([regex]::Escape(\'resultat utile\')))"',
            'Write-Host "LIGNES=$($r.Lignes.Count)"',
            'Write-Host "EAP=$ErrorActionPreference"',
            "Write-Host 'ATTEINT'",
        ])

    def test_un_avertissement_sur_stderr_ne_tue_plus_le_script(self):
        res = self.jouer(self.corps(0))
        self.assertIn("ATTEINT", res.stdout, res.stdout + res.stderr)
        self.assertIn("CODE=0", res.stdout)

    def test_la_sortie_des_deux_flux_est_rendue_a_l_appelant(self):
        """Le helper fusionne stderr et stdout : les messages d'erreur d'un vrai
        echec doivent rester affichables, sinon le lanceur s'arreterait sans
        montrer pourquoi.
        """
        res = self.jouer(self.corps(0))
        self.assertIn("STDERR_VU=True", res.stdout)
        self.assertIn("STDOUT_VU=True", res.stdout)

    def test_un_vrai_echec_reste_un_echec(self):
        """Corriger le faux positif ne doit pas emporter la vraie detection."""
        res = self.jouer(self.corps(3))
        self.assertIn("ATTEINT", res.stdout, res.stdout + res.stderr)
        self.assertIn("CODE=3", res.stdout)

    def test_la_preference_globale_est_intacte_au_retour(self):
        """La portee de FONCTION est ce qui rend le relachement acceptable.

        Si l'affectation fuyait dans la portee du script, le lanceur perdrait son
        'Stop' pour tout ce qui suit, et un echec de cmdlet passerait inapercu.
        C'est cette garantie qui autorise a ne pas ecrire de finally.
        """
        res = self.jouer(self.corps(0))
        self.assertIn("EAP=Stop", res.stdout)


class UnSeulPointDePassage(unittest.TestCase):
    """L'unicite du site de redirection, verifiee sans lancer PowerShell.

    Un correctif local a l'etape 5 aurait laisse cinq mines en place : c'est
    l'unicite, autant que le comportement, qui protege.
    """

    def setUp(self):
        self.lignes = SCRIPT.read_text(encoding="utf-8").split("\n")

    def code(self):
        """Les lignes de CODE : ni commentaire, ni prose de bloc <# ... #>."""
        dedans = False
        for numero, ligne in enumerate(self.lignes, 1):
            nu = ligne.strip()
            if nu.startswith("<#"):
                dedans = True
            if dedans:
                if "#>" in nu:
                    dedans = False
                continue
            if nu.startswith("#") or not nu:
                continue
            yield numero, ligne

    def test_une_seule_redirection_de_stderr_dans_tout_le_script(self):
        trouvees = [(n, l.strip()) for n, l in self.code() if "2>&1" in l]
        self.assertEqual(
            1, len(trouvees),
            "la redirection stderr doit vivre dans Invoquer-Externe et nulle "
            "part ailleurs ; trouvee(s) : " + repr(trouvees))
        self.assertIn("$Binaire @Arguments", trouvees[0][1])

    def test_aucun_appel_natif_direct_ne_subsiste(self):
        """`& git`, `& npm`, `& taskkill`, `& node` passent tous par le helper."""
        fautifs = [(n, l.strip()[:80]) for n, l in self.code()
                   if re.search(r"&\s+(git|npm|taskkill|node)\b", l)]
        self.assertEqual([], fautifs,
                         "appels natifs hors Invoquer-Externe : " + repr(fautifs))

    def test_la_preference_stop_reste_posee_globalement(self):
        """La correction ne doit pas etre "passer le script en Continue".

        Ce serait la reponse facile, et elle rendrait muet chaque echec de cmdlet
        (un Push-Location sur un dossier absent, un Remove-Item qui rate) --
        exactement ce que ce lanceur existe pour ne plus laisser passer.
        """
        poses = [l.strip() for _, l in self.code()
                 if l.startswith("$ErrorActionPreference")]
        self.assertIn("$ErrorActionPreference = 'Stop'", poses)


if __name__ == "__main__":
    unittest.main(verbosity=2)
