#!/usr/bin/env python3
"""Tests de l'installeur du paquet livré — `tools/installer-epure.ps1`, étape C.

`docs/distribution-empaquetee.md`. Le sujet de ce fichier est **la mise à jour**,
pas l'installation. Une première installation qui échoue se voit tout de suite ;
une réinstallation qui écrase `memory/` détruit le profil, l'historique et le
token d'API du destinataire — et ça ne se voit qu'après, chez quelqu'un d'autre,
sur des données qu'on ne peut pas reconstituer.

D'où la forme des tests : deux couches, comme `test_paquet.py`.

**1. La forme du script**, vérifiable partout — y compris sur le runner Linux de
la CI. Trois invariants qui ne se voient pas à l'exécution :

- **ASCII pur.** PowerShell 5.1, celui livré avec Windows, lit un fichier UTF-8
  sans BOM comme de l'ANSI : un tiret cadratin y devient trois caractères dont le
  dernier est un guillemet fermant typographique, que PowerShell accepte comme
  délimiteur de chaîne. Le script entier cesse d'être analysable, sur une erreur
  qui pointe une ligne sans rapport (mesuré dans `install.ps1` : « Le terminateur
  " est manquant dans la chaîne » ligne 320, pour un tiret posé ligne 60).
- **Le Python généré compile.** L'installeur écrit `demarrer.py` depuis un
  here-string. Une coquille dedans ne casse rien au build, rien à l'installation,
  et tout au premier lancement chez le destinataire — sous `pythonw`, donc sans
  console où lire la `SyntaxError`. Les blocs sont donc extraits du `.ps1` et
  passés à `compile()`.
- **La liste des données préservées ne dérive pas de `faire_paquet.py`.** Ajouter
  un dossier de données à `EXCLUS_RACINE` sans le déclarer à l'installeur ne
  casserait rien de visible : l'instantané avant/après cesserait simplement de
  surveiller ce dossier.

**2. Le comportement réel**, en lançant vraiment PowerShell sur une archive
fabriquée. Ces tests se **sautent** hors de Windows (le runner de la CI est un
Linux ; `New-Object -ComObject WScript.Shell` n'y existe pas), comme les cas
Windows de `test_lanceur.py`. Ils restent donc à vérifier à la main — c'est fait,
et c'est la raison pour laquelle la couche 1 existe.

Aucun test n'écrit hors d'un dossier temporaire : ni sur le vrai Bureau (d'où le
paramètre `-Bureau`), ni dans `%LOCALAPPDATA%` (d'où `-Cible`), et rien ne lance
Épure (`-SansLancement`).

Usage :
    python test_installeur.py
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _test_env  # noqa: F401  — isole les chemins AVANT tout import de core.*

_BACKEND = Path(__file__).resolve().parent
_REPO = _BACKEND.parent
sys.path.insert(0, str(_REPO / "tools"))

import faire_paquet as paquet  # noqa: E402

PS1 = _REPO / "tools" / "installer-epure.ps1"
CMD = _REPO / "tools" / "Installer-Epure.cmd"

#: Entrées d'un paquet minimal mais réaliste. La forme vient de l'archive
#: réellement produite (`PAQUET.json` à la racine, `app/backend`, `app/frontend/
#: dist`, `python/`) et non d'une invention : un installeur testé sur une autre
#: arborescence ne teste rien.
def _entrees(version: str, hash_asset: str) -> dict[str, str]:
    return {
        "PAQUET.json": json.dumps({"destinataire": "essai", "arch": "amd64",
                                   "atelier": False, "version": version}),
        "app/backend/main.py": f"# main.py {version}\n",
        # Présent dans l'archive, et c'est tout le problème : c'est aussi le
        # fichier où `PUT /settings/api-keys` écrit les clés du destinataire.
        "app/backend/.env": "# Epure\nEPURE_ATELIER=0\n",
        "app/backend/requirements.txt": "fastapi\n",
        "app/backend/core/paths.py": f"# paths {version}\n",
        "app/frontend/dist/index.html": f"<html>{version}</html>",
        f"app/frontend/dist/_assets/index-{hash_asset}.js": "console.log(1)",
        "python/python.exe": "faux binaire",
        "python/pythonw.exe": "faux binaire",
    }


#: Ce que le destinataire a produit lui-même et qui doit survivre à une mise à
#: jour. Un fichier par emplacement de `$DONNEES_PRESERVEES`, plus un intrus
#: (`migrer_vectoriel.py`) qui n'est PAS une donnée : il vérifie la limite
#: déclarée — une mise à jour ajoute et remplace, elle ne retire pas.
DONNEES_FACTICES = {
    "app/backend/.env": "EPURE_ATELIER=0\nGEMINI_API_KEY=cle-du-destinataire\n",
    "app/backend/memory/instance_config.json": '{"modules_actives": ["chat"]}',
    "app/backend/memory/profil.json": '{"prenom": "Sandr"}',
    "app/backend/history/2026-08.json": "[]",
    "app/backend/vector_db/store.sqlite3": "SQLite format 3 -- faux",
    "app/backend/doc_uploads/cours.pdf": "%PDF-1.4 faux",
    "app/workspace/note.txt": "mes notes",
    "app/data/fiches/fiche.pdf": "%PDF-1.4 fiche",
    "app/backend/migrer_vectoriel.py": "# outil retire du paquet apres coup",
}


def _fabriquer_archive(chemin: Path, version: str, hash_asset: str) -> Path:
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED) as z:
        for nom, contenu in _entrees(version, hash_asset).items():
            z.writestr(nom, contenu)
    return chemin


def _bloc_powershell(nom_variable: str) -> str:
    """Le contenu d'un tableau PowerShell ``$NOM = @( 'a', 'b' )``."""
    texte = PS1.read_text(encoding="ascii")
    m = re.search(rf"\${nom_variable}\s*=\s*@\((.*?)\n\)", texte, re.DOTALL)
    assert m, f"tableau ${nom_variable} introuvable dans {PS1.name}"
    return m.group(1)


def _blocs_python() -> list[str]:
    """Tous les here-strings littéraux ``@'…'@`` du script.

    Ce sont les blocs que l'installeur écrit et exécute tels quels : le
    `demarrer.py` généré et le code d'échauffement. Les deux sont du Python, donc
    les deux doivent compiler.

    (Le second était passé à ``python -c`` jusqu'au 2026-08-26. Il est désormais
    écrit dans un fichier temporaire — cf. `EchauffementTest`, qui explique
    pourquoi cette différence n'est pas cosmétique.)
    """
    texte = PS1.read_text(encoding="ascii").replace("\r\n", "\n")
    return re.findall(r"@'\n(.*?)\n'@", texte, re.DOTALL)


class FormeTest(unittest.TestCase):
    """Couche 1 — ce qui se vérifie sans Windows, donc en CI."""

    def test_les_deux_fichiers_existent(self):
        for f in (PS1, CMD):
            with self.subTest(fichier=f.name):
                self.assertTrue(f.is_file(), f"{f} manquant")

    def test_ascii_pur(self):
        """Un seul octet ≥ 128 rend le script inanalysable en PowerShell 5.1."""
        for f in (PS1, CMD):
            octets = f.read_bytes()
            fautifs = [(octets[:i].count(b"\n") + 1, octets[i])
                       for i in range(len(octets)) if octets[i] >= 128]
            with self.subTest(fichier=f.name):
                self.assertEqual(
                    fautifs, [],
                    f"{f.name} : octets hors ASCII aux lignes "
                    f"{sorted({l for l, _ in fautifs})} — PowerShell 5.1 lit ce "
                    "fichier comme de l'ANSI et cesse de l'analyser",
                )

    def test_fins_de_ligne_crlf(self):
        """`.gitattributes` impose CRLF : cmd.exe peut mal lire un .cmd en LF."""
        for f in (PS1, CMD):
            octets = f.read_bytes()
            with self.subTest(fichier=f.name):
                self.assertEqual(
                    octets.count(b"\n"), octets.count(b"\r\n"),
                    f"{f.name} contient des fins de ligne LF nues",
                )

    def test_aucun_chemin_absolu_en_dur(self):
        """CLAUDE.md §10. Le script se repère par $PSScriptRoot et %~dp0."""
        for f in (PS1, CMD):
            with self.subTest(fichier=f.name):
                texte = f.read_text(encoding="ascii")
                self.assertNotIn("C:" + chr(92), texte)
                self.assertNotIn("C:/", texte)

    def test_le_cmd_amorce_le_ps1_et_rien_d_autre(self):
        """Le .cmd n'existe que pour rendre le .ps1 double-cliquable.

        Deux raisons distinctes, et il faut les deux : un .ps1 double-cliqué
        s'ouvre dans le Bloc-notes, et l'ExecutionPolicy par défaut refuse un
        script non signé venu d'internet.
        """
        texte = CMD.read_text(encoding="ascii")
        for attendu in ("%~dp0installer-epure.ps1", "-ExecutionPolicy Bypass",
                        "-NoProfile", "%*", "pause"):
            with self.subTest(attendu=attendu):
                self.assertIn(attendu, texte)

    def test_les_donnees_preservees_suivent_faire_paquet(self):
        """Le garde-fou anti-dérive entre l'installeur et le script d'assemblage.

        Chaque dossier de données exclu du paquet doit être surveillé par
        l'instantané avant/après de l'installeur. Sans ce test, ajouter une
        entrée à `EXCLUS_RACINE` ne casserait rien de visible : l'installeur
        cesserait simplement de vérifier ce dossier, et l'écrasement éventuel
        redeviendrait silencieux.
        """
        declare = _bloc_powershell("DONNEES_PRESERVEES")
        entrees = set(re.findall(r"'([^']+)'", declare))
        for nom in paquet.EXCLUS_RACINE:
            attendu = "app" + chr(92) + "backend" + chr(92) + nom
            with self.subTest(dossier=nom):
                self.assertIn(
                    attendu, entrees,
                    f"{nom} est exclu du paquet (EXCLUS_RACINE) mais absent de "
                    "$DONNEES_PRESERVEES : l'installeur ne surveillerait pas "
                    "son écrasement",
                )
        # Le `.env` vient d'EXCLUS_FICHIERS, workspace/ et data/ de
        # core/paths.py (resolve_workspace, resolve_fiches_dir) — `app\` tient le
        # rôle de racine du dépôt dans un paquet.
        for attendu in ("app" + chr(92) + "backend" + chr(92) + ".env",
                        "app" + chr(92) + "workspace",
                        "app" + chr(92) + "data"):
            with self.subTest(entree=attendu):
                self.assertIn(attendu, entrees)

    def test_le_python_genere_compile(self):
        """Une coquille dans un here-string ne se verrait qu'au premier lancement.

        Et elle se verrait mal : `demarrer.py` tourne sous `pythonw`, donc sans
        console, donc la `SyntaxError` n'existe nulle part sauf dans un journal
        que le fichier n'a pas encore eu le temps d'ouvrir.
        """
        blocs = _blocs_python()
        self.assertEqual(len(blocs), 2,
                         "attendu deux blocs Python (demarrer.py et l'echauffement)")
        for i, code in enumerate(blocs):
            with self.subTest(bloc=i):
                compile(code, f"<bloc {i} de installer-epure.ps1>", "exec")

    def test_le_lanceur_genere_ecoute_en_loopback(self):
        """CLAUDE.md §6 : jamais au-delà de la machine locale."""
        demarrer = next(b for b in _blocs_python() if "uvicorn.run" in b)
        self.assertIn('HOTE = "127.0.0.1"', demarrer)
        self.assertNotIn("0.0.0.0", demarrer)

    def test_le_lanceur_genere_survit_a_l_absence_de_console(self):
        """Sous `pythonw`, `sys.stdout` vaut None : tout print lèverait.

        Et `log_config=None` n'est pas cosmétique — la configuration par défaut
        d'uvicorn installe un StreamHandler sur un stdout absent.
        """
        demarrer = next(b for b in _blocs_python() if "uvicorn.run" in b)
        for attendu in ("sys.stdout = _journal", "sys.stderr = _journal",
                        "log_config=None"):
            with self.subTest(attendu=attendu):
                self.assertIn(attendu, demarrer)


class LivraisonTest(unittest.TestCase):
    """L'installeur arrive-t-il jusqu'au destinataire ?

    Question distincte de « l'installeur fonctionne-t-il ». Le lanceur du paquet
    marchait très bien quand on l'écrivait à la main : il n'a simplement jamais
    été dans un paquet. Un outil qu'il faut penser à joindre est un outil qui
    manque, donc `faire_paquet.py` le copie à chaque assemblage.
    """

    def test_poser_installeur_copie_les_deux_fichiers(self):
        with tempfile.TemporaryDirectory(prefix="epure-test-livraison-") as tmp:
            sortie = Path(tmp) / "dist-paquets"
            poses = paquet.poser_installeur(sortie, journal=lambda _m: None)
            self.assertEqual(len(poses), 2)
            for nom in paquet.INSTALLEUR:
                with self.subTest(fichier=nom):
                    copie = sortie / nom
                    self.assertTrue(copie.is_file(), f"{nom} non copié")
                    self.assertEqual(copie.read_bytes(),
                                     (_REPO / "tools" / nom).read_bytes())

    def test_poser_installeur_est_relancable(self):
        """Un assemblage écrase la copie précédente : ce n'est pas une donnée."""
        with tempfile.TemporaryDirectory(prefix="epure-test-livraison-") as tmp:
            sortie = Path(tmp)
            (sortie / paquet.INSTALLEUR[0]).write_text("vieille version", encoding="ascii")
            paquet.poser_installeur(sortie, journal=lambda _m: None)
            self.assertEqual((sortie / paquet.INSTALLEUR[0]).read_bytes(),
                             (_REPO / "tools" / paquet.INSTALLEUR[0]).read_bytes())

    def test_les_noms_livres_sont_ceux_que_le_cmd_attend(self):
        """Le .cmd cherche le .ps1 par son nom : les deux listes ne peuvent pas
        diverger sans casser le double-clic chez le destinataire."""
        self.assertIn("installer-epure.ps1", paquet.INSTALLEUR)
        self.assertIn("installer-epure.ps1",
                      CMD.read_text(encoding="ascii"))


def _powershell() -> str | None:
    """Interpréteur pour les tests de comportement, ou None si hors Windows."""
    if platform.system() != "Windows":
        return None
    return shutil.which("powershell") or shutil.which("pwsh")


_PS = _powershell()
_RAISON = "cas propre à Windows (PowerShell + COM WScript.Shell) — cf. l'en-tête"


def _fonction_powershell(nom: str) -> str:
    """Le corps d'une fonction du script, accolade fermante en colonne 0."""
    texte = PS1.read_text(encoding="ascii").replace("\r\n", "\n")
    debut = texte.index(f"function {nom} {{")
    fin = texte.index("\n}", debut) + 2
    corps = texte[debut:fin]
    assert corps.count("{") > 1, f"extraction de {nom} suspecte"
    return corps


class EchauffementTest(unittest.TestCase):
    """`Echauffer` — le transport du code Python, et ce qu'on ose en conclure.

    **L'INCIDENT.** Chez un destinataire, l'échauffement des modules natifs
    échouait sur une vraie `SyntaxError`, et les deux alertes qui suivaient
    accusaient Smart App Control :

        File "<string>", line 8
           print(absent
                ^
        SyntaxError: '(' was never closed
        !    un module natif a refuse de se charger -- nouvelle tentative dans 20 s
        !    Smart App Control evalue la reputation des DLL fraichement dezippees

    Le source, lui, disait `print("absent   " + nom)`. **Les guillemets doubles
    avaient disparu entre le here-string et ce que `python.exe -c` recevait.**

    **MÉCANISME**, reproduit sur x64, sans SAC, donc sans rien devoir à
    l'architecture : sous Windows PowerShell 5.1 — celui que lance
    `Installer-Epure.cmd`, donc celui de TOUS les destinataires — la ligne de
    commande d'un binaire natif est reconstruite selon les règles de
    `CommandLineToArgvW`, et 5.1 n'échappe pas les guillemets internes d'un
    argument. Mesuré sur le même here-string :

        powershell.exe 5.1, `-c`   -> SyntaxError: '(' was never closed
        pwsh 7.6, `-c`             -> fonctionne
        fichier temporaire         -> fonctionne dans les DEUX
        stdin (`python -`)         -> fonctionne dans les DEUX

    **Conséquence à mesurer, pas à minimiser : l'échauffement n'a JAMAIS
    fonctionné chez un destinataire.** Il affichait une `SyntaxError`, accusait
    Smart App Control, attendait 20 s, rejouait exactement le même échec, puis
    promettait que « le blocage est temporaire ».

    **CE QUE CES TESTS GARDENT.** Deux choses distinctes, et la seconde vaut
    autant que la première : que le code arrive intact jusqu'à Python, et que le
    diagnostic n'affirme pas une cause qu'il n'a pas mesurée. Un message qui se
    trompe de coupable coûte plus cher qu'une absence de message — il envoie
    chercher un problème qu'on n'a pas.
    """

    def setUp(self):
        self.corps = _fonction_powershell("Echauffer")

    # ── Structure ────────────────────────────────────────────────────────────

    def test_le_code_python_n_est_jamais_passe_en_ligne_de_commande(self):
        """La règle, et elle est structurelle : aucun `-c`.

        Reformuler le Python sans guillemets doubles marcherait aujourd'hui et
        casserait au premier `"` ajouté, sans que rien ne le signale — la panne
        n'apparaîtrait que chez le destinataire. Le fichier temporaire supprime
        la surface entière.
        """
        self.assertNotIn("'-c'", self.corps)
        self.assertNotIn('"-c"', self.corps)
        self.assertIn("Set-Content", self.corps)
        self.assertIn("Remove-Item", self.corps)   # nettoyé derrière

    def test_le_diagnostic_ne_blame_pas_sac_avant_de_savoir(self):
        """Le contrôle de flux, pas seulement la présence du mot.

        La garde `SyntaxError` doit venir AVANT l'alerte qui nomme Smart App
        Control, et rendre la main : sinon les deux messages sortent ensemble et
        le destinataire lit la mauvaise cause.
        """
        i_garde = self.corps.find("SyntaxError")
        i_sac = self.corps.find("Smart App Control")
        self.assertNotEqual(-1, i_garde, "plus de garde sur SyntaxError")
        self.assertNotEqual(-1, i_sac)
        self.assertLess(i_garde, i_sac, "la garde ne protège plus rien")
        entre = self.corps[i_garde:i_sac]
        self.assertIn("return", entre, "la garde ne rend pas la main")

    def test_un_echec_deterministe_est_nomme_comme_tel(self):
        """Deux essais identiques ne sont pas une réputation en cours d'évaluation.

        C'est la seule mesure dont l'installeur dispose pour distinguer les deux,
        et elle ne coûte rien : comparer les deux sorties.
        """
        self.assertIn("$precedente", self.corps)
        self.assertIn("IDENTIQUE", self.corps)

    # ── Comportement, sous le vrai interpréteur ──────────────────────────────

    def _jouer(self, source_ps1, modules):
        """Joue le VRAI `Echauffer` d'un script donné, sur le vrai Python.

        La racine est un dossier temporaire dont `python\` est un point de
        jonction vers l'interpréteur de ce poste : `Echauffer` cherche
        `<racine>\python\python.exe` et le trouve, sans qu'on ait à copier une
        installation Python.
        """
        with tempfile.TemporaryDirectory() as tmp:
            harnais = Path(tmp) / "harnais.ps1"
            cible = Path(tmp) / "installeur.ps1"
            cible.write_text(Path(source_ps1).read_text(encoding="ascii"),
                             encoding="ascii")
            harnais.write_text(_HARNAIS, encoding="ascii")
            res = subprocess.run(
                [_PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(harnais), "-Installeur", str(cible),
                 "-DossierPython", str(Path(sys.executable).parent),
                 "-Modules", ",".join(modules)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=180)
            return res.stdout + res.stderr

    @unittest.skipIf(_PS is None, _RAISON)
    def test_les_guillemets_survivent_jusqu_a_python(self):
        """LE test. Les marqueurs sont espacés par des littéraux entre
        guillemets doubles (`"absent   "`, `"ok       "`) : les retrouver
        intacts, avec leur espacement, prouve que la chaîne est arrivée entière.
        """
        sortie = self._jouer(PS1, ["json", "module_qui_n_existe_pas"])
        self.assertIn("ok       json", sortie, sortie)
        self.assertIn("absent   module_qui_n_existe_pas", sortie, sortie)
        self.assertIn("modules natifs charges", sortie, sortie)
        self.assertNotIn("SyntaxError", sortie, sortie)
        # Et surtout : aucune accusation, puisqu'il n'y a rien à accuser.
        self.assertNotIn("Smart App Control", sortie, sortie)

    @unittest.skipIf(_PS is None, _RAISON)
    def test_l_ancien_idiome_echouerait_encore(self):
        """Cas de CONTRÔLE : sans lui, tout ce fichier pourrait passer sur une
        machine où le problème n'existe pas, et annoncer une protection qu'il ne
        mesure pas.

        On rejoue l'idiome d'avant — le même here-string passé en `python -c` —
        et on vérifie qu'il produit toujours la `SyntaxError` du destinataire.
        """
        with tempfile.TemporaryDirectory() as tmp:
            essai = Path(tmp) / "ancien.ps1"
            essai.write_text(_ANCIEN_IDIOME, encoding="ascii")
            res = subprocess.run(
                [_PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(essai), "-Exe", sys.executable],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120)
        sortie = res.stdout + res.stderr
        if "pwsh" in (_PS or "").lower():
            self.skipTest("PowerShell 7 n'a pas ce défaut — cf. le tableau du docstring")
        self.assertIn("SyntaxError", sortie,
                      "l'ancien idiome ne casse plus : ce test ne prouve plus rien")


#: Harnais : extrait du VRAI script ses deux fonctions et joue `Echauffer` sur
#: une racine factice. Les sorties de l'installeur sont remplacées par des stubs
#: — on teste `Echauffer`, pas la journalisation.
_HARNAIS = """param([string]$Installeur, [string]$DossierPython, [string]$Modules)
$ErrorActionPreference = 'Stop'
function Info([string]$m)   { Write-Host $m }
function Ok([string]$m)     { Write-Host $m }
function Alerte([string]$m) { Write-Host $m }
function Note([string]$m)   { Write-Host $m }

$src = Get-Content -LiteralPath $Installeur -Raw
foreach ($nom in @('Executer-Natif', 'Echauffer')) {
    $d = $src.IndexOf("function $nom {")
    $f = $src.IndexOf("`n}", $d)
    Invoke-Expression $src.Substring($d, $f - $d + 2)
}
# La liste reelle est remplacee : le test doit dire ce qu'il attend, pas dependre
# de ce qui est installe sur la machine qui le joue.
$MODULES_NATIFS = $Modules -split ','

$racine = Join-Path $env:TEMP ('epure-test-ech-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $racine | Out-Null
New-Item -ItemType Junction -Path (Join-Path $racine 'python') -Target $DossierPython | Out-Null
try {
    Echauffer -Racine $racine
} finally {
    Remove-Item -LiteralPath (Join-Path $racine 'python') -Force -Recurse -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $racine -Recurse -Force -ErrorAction SilentlyContinue
}
"""

#: L'idiome d'AVANT, reproduit tel quel pour le cas de controle.
_ANCIEN_IDIOME = """param([string]$Exe)
$ErrorActionPreference = 'Continue'
$code = @'
import sys
print("absent   " + sys.argv[1])
'@
& $Exe @('-c', $code) + @('json') 2>&1 | ForEach-Object { Write-Host "$_" }
"""


class _ScenarioInstallation(unittest.TestCase):
    """Socle commun : un dossier temporaire, une archive, un faux Bureau."""

    @classmethod
    def _preparer(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="epure-test-inst-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.cible = cls.tmp / "install"
        cls.bureau = cls.tmp / "bureau"
        cls.bureau.mkdir()

    @classmethod
    def _installer(cls, archive: Path, cible: Path | None = None,
                   script: Path | None = None) -> subprocess.CompletedProcess:
        cmd = [
            _PS, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script or PS1),
            "-Cible", str(cible or cls.cible),
            "-Bureau", str(cls.bureau),
            # Rien ne démarre et rien ne charge de DLL : le paquet fabriqué ici
            # n'a qu'un faux python.exe, et un test n'a pas à lancer un serveur.
            "-SansLancement", "-SansEchauffement",
        ]
        if archive is not None:
            cmd += ["-Archive", str(archive)]
        return subprocess.run(cmd, capture_output=True, timeout=180,
                              # La sortie console de PowerShell 5.1 n'est pas en
                              # UTF-8 : on ne l'analyse pas, on la garde lisible
                              # pour les messages d'échec.
                              text=True, encoding="utf-8", errors="replace")


@unittest.skipUnless(_PS, _RAISON)
class PremiereInstallationTest(_ScenarioInstallation):
    """Dossier vide → une installation complète et lançable."""

    @classmethod
    def setUpClass(cls):
        cls._preparer()
        archive = _fabriquer_archive(cls.tmp / "epure-essai.zip", "v1", "AAAA")
        cls.resultat = cls._installer(archive)

    def test_le_script_reussit(self):
        self.assertEqual(self.resultat.returncode, 0, self.resultat.stdout)

    def test_l_archive_est_deployee(self):
        for rel in ("PAQUET.json", "app/backend/main.py",
                    "app/frontend/dist/index.html",
                    "app/frontend/dist/_assets/index-AAAA.js",
                    "python/pythonw.exe"):
            with self.subTest(fichier=rel):
                self.assertTrue((self.cible / rel).is_file(), f"{rel} manquant")

    def test_le_lanceur_est_genere(self):
        """Le cœur de l'étape C : plus jamais un lanceur écrit à la main.

        Aucun paquet assemblé avant celui-ci n'en contenait — un fichier qu'il
        faut penser à écrire est un fichier qui manque.
        """
        cmd = self.cible / "Epure.cmd"
        py = self.cible / "demarrer.py"
        self.assertTrue(cmd.is_file(), "Epure.cmd non généré")
        self.assertTrue(py.is_file(), "demarrer.py non généré")
        texte = cmd.read_text(encoding="ascii")
        self.assertIn("pythonw.exe", texte,
                      "le lanceur doit passer par pythonw (pas de console)")
        self.assertIn("%~dp0", texte, "le lanceur doit rester relatif à son dossier")
        compile(py.read_text(encoding="ascii"), str(py), "exec")

    def test_le_raccourci_est_pose(self):
        liens = list(self.bureau.glob("*.lnk"))
        self.assertEqual(len(liens), 1, f"raccourcis trouvés : {liens}")
        self.assertEqual(liens[0].name, "\u00c9pure.lnk")

    def test_le_journal_d_installation_existe(self):
        journal = self.cible / "installation.log"
        self.assertTrue(journal.is_file())
        self.assertIn("premiere installation",
                      journal.read_text(encoding="utf-8", errors="replace"))


@unittest.skipUnless(_PS, _RAISON)
class MiseAJourTest(_ScenarioInstallation):
    """Réinstallation par-dessus une installation vécue. Le vrai sujet.

    Scénario : on installe la v1, on écrit les données que le destinataire aurait
    produites (profil, historique, index vectoriel, PDF déposés, clé d'API dans
    le `.env`), puis on installe la v2 par-dessus.
    """

    @classmethod
    def setUpClass(cls):
        cls._preparer()
        cls.resultat_v1 = cls._installer(
            _fabriquer_archive(cls.tmp / "epure-v1.zip", "v1", "AAAA"))
        for rel, contenu in DONNEES_FACTICES.items():
            p = cls.cible / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(contenu, encoding="utf-8")
        cls.resultat_v2 = cls._installer(
            _fabriquer_archive(cls.tmp / "epure-v2.zip", "v2", "BBBB"))

    def test_les_deux_installations_reussissent(self):
        self.assertEqual(self.resultat_v1.returncode, 0, self.resultat_v1.stdout)
        self.assertEqual(self.resultat_v2.returncode, 0, self.resultat_v2.stdout)

    def test_la_mise_a_jour_est_annoncee_comme_telle(self):
        """Un installeur qui ne dit pas qu'il met à jour est indistinguable d'un
        installeur qui recommence à zéro — et c'est ce qu'on veut savoir avant
        de le laisser écrire."""
        self.assertIn("MISE A JOUR", self.resultat_v2.stdout)

    def test_aucune_donnee_du_destinataire_n_est_touchee(self):
        for rel, contenu in DONNEES_FACTICES.items():
            p = self.cible / rel
            with self.subTest(fichier=rel):
                self.assertTrue(p.is_file(), f"{rel} a disparu à la mise à jour")
                self.assertEqual(
                    p.read_text(encoding="utf-8"), contenu,
                    f"{rel} a été réécrit par la mise à jour",
                )

    def test_la_cle_d_api_du_destinataire_survit(self):
        """Le seul fichier de données que l'archive contient AUSSI.

        `faire_paquet.py` écrit un `.env` dans le paquet pour éteindre l'Atelier
        côté serveur ; c'est le même fichier que `PUT /settings/api-keys`
        complète. L'écraser coûterait toutes les clés du destinataire — et
        « installer la mise à jour » est exactement le geste après lequel
        personne ne va vérifier ses clés.
        """
        env = (self.cible / "app/backend/.env").read_text(encoding="utf-8")
        self.assertIn("GEMINI_API_KEY=cle-du-destinataire", env)
        self.assertIn("EPURE_ATELIER=0", env)

    def test_le_code_et_l_interface_sont_bien_remplaces(self):
        """Le pendant : préserver les données ne doit pas empêcher la mise à jour."""
        self.assertIn("v2", (self.cible / "app/backend/main.py").read_text())
        self.assertIn("v2", (self.cible / "app/backend/core/paths.py").read_text())
        self.assertIn("v2", (self.cible / "app/frontend/dist/index.html").read_text())
        self.assertEqual(
            json.loads((self.cible / "PAQUET.json").read_text())["version"], "v2")

    def test_les_anciens_assets_ne_s_accumulent_pas(self):
        """`app\\frontend\\dist` est le seul dossier vidé avant recopie.

        Ses fichiers portent un hash dans leur nom (`index-<hash>.js`) : une
        simple superposition les empilerait à chaque mise à jour, sans que rien
        ne le signale — `index.html` ne référence que le dernier.
        """
        assets = self.cible / "app/frontend/dist/_assets"
        self.assertTrue((assets / "index-BBBB.js").is_file())
        self.assertFalse((assets / "index-AAAA.js").exists(),
                         "l'asset de la version précédente est resté")

    def test_un_fichier_retire_du_paquet_reste_sur_le_disque(self):
        """Limite DÉCLARÉE, affirmée ici pour qu'elle ne soit pas une surprise.

        Une mise à jour ajoute et remplace, elle ne retire pas : `python\\` peut
        contenir un torch installé à la main (~2 Go, écart 3 de l'étape C), et
        aucune règle ne distingue de façon sûre « fichier retiré du paquet » de
        « fichier ajouté par le destinataire ». Le fichier survit, inerte.
        """
        self.assertTrue((self.cible / "app/backend/migrer_vectoriel.py").is_file())


@unittest.skipUnless(_PS, _RAISON)
class RefusTest(_ScenarioInstallation):
    """Ce que l'installeur doit refuser plutôt que deviner."""

    @classmethod
    def setUpClass(cls):
        cls._preparer()

    def test_une_archive_etrangere_est_refusee_avant_d_ecrire(self):
        """Sans ce contrôle, n'importe quel zip se déverserait dans la cible et
        l'installation aurait l'air réussie."""
        etranger = self.tmp / "epure-pas-un-paquet.zip"
        with zipfile.ZipFile(etranger, "w") as z:
            z.writestr("photos/plage.jpg", "pas un paquet")
        cible = self.tmp / "refus-archive"
        r = self._installer(etranger, cible=cible)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertFalse((cible / "photos").exists(),
                         "l'archive étrangère a été déployée quand même")

    def test_plusieurs_archives_a_cote_du_script_font_refuser(self):
        """Deviner serait pire : installer la mauvaise version est invisible
        jusqu'au premier bug qu'on cherchera dans le code."""
        atelier = self.tmp / "deux-zips"
        atelier.mkdir()
        script = atelier / PS1.name
        shutil.copy2(PS1, script)
        _fabriquer_archive(atelier / "epure-sandr.zip", "v1", "AAAA")
        _fabriquer_archive(atelier / "epure-autre.zip", "v1", "AAAA")
        r = self._installer(None, cible=self.tmp / "refus-ambigu", script=script)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("plusieurs archives", r.stdout.lower())

    def test_une_archive_unique_a_cote_du_script_est_trouvee_seule(self):
        """Le pendant : c'est le chemin nominal du destinataire, qui ne passe
        aucun argument — il double-clique le .cmd."""
        atelier = self.tmp / "un-zip"
        atelier.mkdir()
        script = atelier / PS1.name
        shutil.copy2(PS1, script)
        _fabriquer_archive(atelier / "epure-sandr.zip", "v1", "AAAA")
        cible = self.tmp / "auto"
        r = self._installer(None, cible=cible, script=script)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((cible / "demarrer.py").is_file())


@unittest.skipUnless(_PS, _RAISON)
class AtelierEteintTest(_ScenarioInstallation):
    """Le `.env` conservé doit toujours couper les routes de l'Atelier.

    `main.py` lit ``os.environ.get("EPURE_ATELIER", "1")`` : le défaut est
    ACTIF. Un `.env` sans cette ligne rend donc `/workshop*` joignable alors que
    l'écran est absent du bundle — l'écart 5 de l'étape C, mais sur le chemin de
    la mise à jour, où personne ne l'avait cherché.
    """

    @classmethod
    def setUpClass(cls):
        cls._preparer()
        cls._installer(_fabriquer_archive(cls.tmp / "epure-v1.zip", "v1", "AAAA"))
        (cls.cible / "app/backend/.env").write_text(
            "GEMINI_API_KEY=cle\n", encoding="utf-8")
        cls.resultat = cls._installer(
            _fabriquer_archive(cls.tmp / "epure-v2.zip", "v2", "BBBB"))

    def test_un_paquet_sans_env_en_recoit_un(self):
        """Une archive assemblée avant le 2026-08-22 n'en contient pas.

        `faire_paquet.py` n'écrit ce `.env` que depuis cette date : celle déjà
        produite pour sandr n'en a aucun. Sans cette branche, l'Atelier serait
        joignable ou non selon le millésime du zip qu'on installe — un invariant
        de sécurité ne doit pas dépendre de l'âge de l'archive.
        """
        with tempfile.TemporaryDirectory(prefix="epure-test-sansenv-") as tmp:
            tmp = Path(tmp)
            archive = tmp / "epure-vieux.zip"
            entrees = {k: v for k, v in _entrees("vieux", "AAAA").items()
                       if k != "app/backend/.env"}
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
                for nom, contenu in entrees.items():
                    z.writestr(nom, contenu)
            cible = tmp / "install"
            r = self._installer(archive, cible=cible)
            self.assertEqual(r.returncode, 0, r.stdout)
            env = cible / "app/backend/.env"
            self.assertTrue(env.is_file(), ".env non créé pour une archive qui n'en a pas")
            self.assertRegex(env.read_text(encoding="ascii"),
                             r"(?m)^\s*EPURE_ATELIER\s*=\s*0\s*$")

    def test_la_ligne_manquante_est_remise(self):
        self.assertEqual(self.resultat.returncode, 0, self.resultat.stdout)
        env = (self.cible / "app/backend/.env").read_text(encoding="utf-8")
        self.assertRegex(env, r"(?m)^\s*EPURE_ATELIER\s*=\s*0\s*$")
        self.assertIn("GEMINI_API_KEY=cle", env,
                      "la clé du destinataire a été perdue en remettant la ligne")


if __name__ == "__main__":
    unittest.main(verbosity=2)
