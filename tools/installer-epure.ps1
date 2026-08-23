<#
.SYNOPSIS
    Installe un paquet Epure livre : dezippe, ecrit le lanceur, pose le raccourci.
    Etape C de docs/distribution-empaquetee.md.

.DESCRIPTION
    Le destinataire recoit trois fichiers dans un meme dossier : cette archive
    epure-<nom>.zip, ce script, et Installer-Epure.cmd. Il double-clique le .cmd.
    Aucune ligne de commande, aucun prerequis a installer, aucun droit
    administrateur.

    POURQUOI POWERSHELL ET PAS UN EXECUTABLE. PowerShell 5.1 est livre avec
    Windows 10 et 11 : c'est la seule chose dont on soit sur qu'elle est deja la.
    Un .exe demanderait un toolchain au build et se ferait arreter par SmartScreen
    faute de signature -- exactement le probleme decrit plus bas pour les .pyd, mais
    sur le fichier que le destinataire doit lancer en premier. Un .cmd seul ne sait
    ni dezipper ni poser un raccourci. Le .cmd d'amorcage existe pour deux raisons
    et deux seulement : un .ps1 double-clique s'ouvre dans le Bloc-notes, et
    l'ExecutionPolicy par defaut refuse les scripts non signes.

    IDEMPOTENT, ET C'EST LA MOITIE DU SUJET. Relance sur une installation
    existante, cette procedure est une MISE A JOUR. La regle tient en une ligne :
    on ecrit tout ce que l'archive contient, on ne supprime jamais rien d'autre.
    Elle est correcte parce que l'archive, par construction, ne contient AUCUNE
    donnee du destinataire -- `tools/faire_paquet.py` exclut memory/, history/,
    vector_db/, chroma_db/, doc_uploads/, piper_models/ et le .env a la racine de
    backend/, et `backend/test_paquet.py` le verifie a chaque commit. Les donnees
    survivent donc parce qu'elles sont ABSENTES de l'archive, pas parce qu'une
    liste de protection les nomme.

    Cette liste existe quand meme ($DONNEES_PRESERVEES) mais elle ne filtre rien :
    elle sert d'instantane avant/apres, pour que la promesse soit VERIFIEE a
    l'execution et non seulement documentee. Un ecrasement silencieux de memory/
    coute le profil, l'historique et le token d'API du destinataire ; l'erreur
    doit etre bruyante.

    DEUX EXCEPTIONS a la regle, toutes deux volontaires :

    1. `app\backend\.env` EST dans l'archive (ecrit par faire_paquet.py pour
       eteindre l'Atelier cote serveur), et c'est aussi le fichier ou
       `PUT /settings/api-keys` ecrit les cles du destinataire. L'ecraser lui
       ferait perdre ses cles. Il n'est donc ecrit que s'il est absent ; s'il
       existe, on verifie seulement qu'il porte EPURE_ATELIER=0 et on ajoute la
       ligne si elle manque -- sans elle les routes de l'Atelier redeviennent
       joignables alors que l'ecran n'existe pas dans le bundle.

    2. `app\frontend\dist` est REMPLACE en entier (vide puis recopie). C'est le
       seul dossier purement genere dont les noms de fichiers changent a chaque
       build : ses assets sont horodates par un hash (index-<hash>.js), donc une
       simple superposition les accumulerait a chaque mise a jour. `python\` est
       aussi un dossier genere mais n'est PAS traite ainsi : le destinataire peut
       y avoir installe torch a la main (~2 Go, cf. ecart 3 de l'etape C), et le
       vider le lui ferait retelecharger.

    CE QUI N'EST PAS FAIT, et qu'il ne faut pas croire fait : l'installation
    d'Ollama et le telechargement d'un modele local. Le plan d'etape C les
    prevoyait ; ils demandent un telechargement de plusieurs Go, des droits
    administrateur selon le chemin choisi, et n'ont rien a voir avec le paquet.
    Epure demarre et sert son interface sans Ollama -- avec des modeles locaux
    annonces indisponibles, ce qui est un etat coherent. Voir docs/
    distribution-empaquetee.md, etape C.

    ECHAUFFEMENT SMART APP CONTROL. Sur un Windows 11 ou Smart App Control est
    applique, le premier chargement d'une DLL non signee fraichement dezippee dans
    un dossier utilisateur peut etre bloque, puis autorise quelques minutes plus
    tard sans que rien n'ait change sur le disque (mesure le 2026-08-10, cf.
    etape C). C'est intermittent, donc c'est le pire a diagnostiquer a distance.
    Ce script importe donc une fois chaque module natif du paquet, pendant
    l'installation ou l'attente est attendue, et retente une fois en cas d'echec.
    C'est l'option 2 des trois que l'etape C laissait ouvertes.

.PARAMETER Archive
    Le zip a installer. Par defaut : le seul epure-*.zip a cote de ce script.
    Plusieurs candidats -> refus, avec la liste. Deviner serait pire.

.PARAMETER Cible
    Dossier d'installation. Par defaut %LOCALAPPDATA%\Epure.

    Ce choix n'est pas anodin : Bureau et Documents sont rediriges vers OneDrive
    sur beaucoup de postes (verifie sur celui-ci : Desktop pointe vers
    <profil>\OneDrive\Desktop). Une installation la-dedans se ferait
    synchroniser -- 130 Mo et 7400 fichiers dont un python.exe -- avec des
    verrous de fichiers a l'execution et des copies "(2)" a la moindre
    reinstallation. LOCALAPPDATA n'est jamais redirige et ne demande aucun droit.

.PARAMETER Bureau
    Ou poser le raccourci. Par defaut le vrai Bureau de l'utilisateur. Ce
    parametre existe pour que les tests puissent verifier le raccourci sans
    ecrire sur le Bureau reel.

.PARAMETER SansRaccourci
    Ne pose pas de raccourci.

.PARAMETER SansLancement
    N'ouvre pas Epure a la fin. Les tests s'en servent.

.PARAMETER SansEchauffement
    Saute l'echauffement des modules natifs (cf. plus haut).

.NOTES
    CONTENU EN ASCII PUR, VOLONTAIREMENT -- meme regle que install.ps1 et
    Epure.bat, et pour la meme raison mesuree : PowerShell 5.1, celui livre avec
    Windows, lit un fichier UTF-8 SANS BOM comme de l'ANSI. Un tiret cadratin y
    devient trois caracteres dont le dernier est un guillemet fermant
    typographique, que PowerShell accepte comme delimiteur de chaine : le script
    entier cesse d'etre analysable, sur une erreur qui pointe une ligne sans
    rapport. C'est arrive dans install.ps1 : << Le terminateur " est manquant
    dans la chaine >> ligne 320, pour un tiret pose ligne 60. Verifie par
    backend/test_installeur.py, qui refuse le moindre octet >= 128.

    Teste par backend/test_installeur.py : premiere installation sur un dossier
    vide, puis reinstallation par-dessus avec des donnees factices dans
    memory/, vector_db/, workspace/, data/fiches/ et un .env portant une cle.

.EXAMPLE
    .\Installer-Epure.cmd

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\installer-epure.ps1 -Cible D:\Epure
#>

[CmdletBinding()]
param(
    [string]$Archive,
    [string]$Cible = (Join-Path $env:LOCALAPPDATA 'Epure'),
    [string]$Bureau = [Environment]::GetFolderPath('Desktop'),
    [switch]$SansRaccourci,
    [switch]$SansLancement,
    [switch]$SansEchauffement
)

$ErrorActionPreference = 'Stop'

#: Port d'ecoute d'Epure dans un paquet. En dur et en loopback : le paquet n'a
#: pas de lanceur configurable, et CLAUDE.md section 6 interdit d'ecouter
#: au-dela de la machine locale (le wifi d'une prepa est un reseau hostile).
$PORT = 8000
$HOTE = '127.0.0.1'

#: Racine de ce script. %~dp0 cote .cmd, $PSScriptRoot ici : aucun chemin absolu
#: en dur nulle part (CLAUDE.md section 10).
$ICI = $PSScriptRoot
if (-not $ICI) { $ICI = Split-Path -Parent $MyInvocation.MyCommand.Definition }

#: Donnees du destinataire. CETTE LISTE NE FILTRE RIEN -- elle ne peut pas :
#: aucune de ces entrees n'est dans l'archive (cf. EXCLUS_RACINE et
#: EXCLUS_FICHIERS de tools/faire_paquet.py), donc la boucle de deploiement ne
#: les rencontre jamais. Elle sert d'instantane avant/apres pour transformer la
#: promesse en verification, et de point de synchronisation avec le script
#: d'assemblage : backend/test_installeur.py refuse que les deux divergent.
#:
#: `app\workspace` et `app\data` ne viennent pas d'EXCLUS_RACINE mais de
#: core/paths.py : resolve_workspace() rend <racine>/workspace et
#: resolve_fiches_dir() rend <racine>/data/fiches, et dans un paquet `app\` tient
#: le role de racine du depot. Ce sont les fichiers que le destinataire depose
#: lui-meme -- les oublier serait perdre ses PDF.
$DONNEES_PRESERVEES = @(
    'app\backend\.env',
    'app\backend\memory',
    'app\backend\history',
    'app\backend\vector_db',
    'app\backend\chroma_db',
    'app\backend\doc_uploads',
    'app\backend\piper_models',
    'app\workspace',
    'app\data'
)

#: Modules natifs a echauffer. Ce sont les extensions compilees de
#: tools/contraintes-paquet.txt : celles qui chargent une DLL, donc les seules
#: que Smart App Control peut bloquer. Absentes selon l'architecture (av et
#: ctranslate2 partent du paquet ARM64 avec la voix) : find_spec decide, on ne
#: presume rien.
$MODULES_NATIFS = @(
    'numpy', 'sqlite3', 'pydantic_core', 'tokenizers',
    'onnxruntime', 'lxml', 'av', 'ctranslate2'
)


# -- Sortie ------------------------------------------------------------------
# Le journal est ouvert des que la cible existe. Avant, les lignes sont
# tamponnees : un script qui echoue AVANT d'avoir cree son dossier doit quand
# meme laisser une trace lisible a l'ecran.

$script:Journal = $null
$script:Tampon = @()

function Ecrire {
    param([string]$Texte, [string]$Couleur = 'Gray')
    Write-Host $Texte -ForegroundColor $Couleur
    $ligne = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Texte
    if ($script:Journal) {
        Add-Content -Path $script:Journal -Value $ligne -Encoding utf8
    } else {
        $script:Tampon += $ligne
    }
}

function Ouvrir-Journal {
    param([string]$Dossier)
    $script:Journal = Join-Path $Dossier 'installation.log'
    if ($script:Tampon.Count -gt 0) {
        Add-Content -Path $script:Journal -Value $script:Tampon -Encoding utf8
        $script:Tampon = @()
    }
}

function Titre { param([string]$t) Ecrire '' ; Ecrire "== $t" 'Cyan' }
function Info  { param([string]$t) Ecrire "   $t" 'Gray' }
function Ok    { param([string]$t) Ecrire "   OK   $t" 'Green' }
function Note  { param([string]$t) Ecrire "   --   $t" 'DarkGray' }
function Alerte{ param([string]$t) Ecrire "   !    $t" 'Yellow' }

function Abandonner {
    param([string]$Pourquoi, [string]$QueFaire = '')
    Ecrire '' ; Ecrire "ECHEC : $Pourquoi" 'Red'
    if ($QueFaire) { Ecrire "        $QueFaire" 'Red' }
    exit 1
}


# -- Confinement -------------------------------------------------------------

function Sous-Dossier {
    <#
    Le chemin $Candidat est-il bien SOUS $Racine ?

    Canonicalisation puis comparaison AVEC le separateur final. Le separateur
    est ce qui rend le test correct : sans lui, un dossier frere nomme
    <racine>-autre passerait pour un enfant de <racine>. C'est la meme regle
    que core/paths.py, ou le
    confinement se fait par resolve() + is_relative_to() et jamais par un
    startswith de chaines (CLAUDE.md section 3.5).

    Path.GetRelativePath n'existe pas en .NET Framework 4.x, donc pas dans
    PowerShell 5.1 : la version ci-dessous est celle qui marche partout.
    #>
    param([string]$Racine, [string]$Candidat)
    $sep = [System.IO.Path]::DirectorySeparatorChar
    $r = [System.IO.Path]::GetFullPath($Racine).TrimEnd($sep) + $sep
    $c = [System.IO.Path]::GetFullPath($Candidat)
    return $c.StartsWith($r, [System.StringComparison]::OrdinalIgnoreCase)
}


# -- Archive -----------------------------------------------------------------

function Trouver-Archive {
    if ($Archive) {
        if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) {
            Abandonner "archive introuvable : $Archive"
        }
        return (Resolve-Path -LiteralPath $Archive).Path
    }
    $candidats = @(Get-ChildItem -LiteralPath $ICI -Filter 'epure-*.zip' -File -ErrorAction SilentlyContinue)
    if ($candidats.Count -eq 0) {
        Abandonner "aucune archive epure-*.zip a cote de ce script ($ICI)" `
                   "Placez le zip recu dans ce dossier, puis relancez."
    }
    if ($candidats.Count -gt 1) {
        # Deviner serait pire que refuser : installer la mauvaise version est
        # invisible jusqu'au premier bug qu'on cherchera dans le code.
        Ecrire '' ; Ecrire 'Plusieurs archives possibles :' 'Yellow'
        foreach ($c in $candidats) { Ecrire "   $($c.Name)" 'Yellow' }
        Abandonner 'plusieurs archives epure-*.zip dans ce dossier' `
                   'Relancez avec -Archive <chemin du zip a installer>.'
    }
    return $candidats[0].FullName
}

function Verifier-Archive {
    <#
    Refuse tout zip qui n'est pas un paquet Epure, AVANT d'ecrire un octet.

    Sans ce controle, une archive quelconque deposee a cote du script se
    deverserait dans %LOCALAPPDATA%\Epure et l'installation aurait l'air
    reussie.
    #>
    param([string]$Chemin)
    $attendus = @('PAQUET.json', 'app/backend/main.py')
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Chemin)
    try {
        $noms = @($zip.Entries | ForEach-Object { $_.FullName })
        foreach ($a in $attendus) {
            if ($noms -notcontains $a) {
                Abandonner "l'archive ne contient pas $a -- ce n'est pas un paquet Epure" `
                           "Verifiez le fichier recu : $(Split-Path -Leaf $Chemin)"
            }
        }
        return $noms.Count
    } finally {
        $zip.Dispose()
    }
}

function Deja-En-Marche {
    <#
    Une instance repond-elle deja sur le port ?

    Verifie par /health et non par un simple connect : le port 8000 est banal, et
    conclure "Epure tourne" parce que quelque chose ecoute conduirait a refuser
    la mise a jour a cause d'un programme sans rapport.
    #>
    try {
        $r = Invoke-WebRequest -Uri "http://${HOTE}:$PORT/health" -TimeoutSec 2 -UseBasicParsing
        return ($r.StatusCode -eq 200 -and $r.Content -match 'ollama')
    } catch {
        return $false
    }
}


# -- Instantane des donnees --------------------------------------------------

function Instantane {
    <#
    Empreinte taille + date de tout fichier de donnees deja present.

    Taille et date plutot qu'un hash : un vector_db peut peser des centaines de
    Mo, et le but est de detecter un ECRASEMENT, pas une corruption d'octets.
    #>
    param([string]$Racine)
    $vu = @{}
    foreach ($rel in $DONNEES_PRESERVEES) {
        $p = Join-Path $Racine $rel
        if (-not (Test-Path -LiteralPath $p)) { continue }
        $fichiers = if (Test-Path -LiteralPath $p -PathType Leaf) {
            @(Get-Item -LiteralPath $p)
        } else {
            @(Get-ChildItem -LiteralPath $p -Recurse -File -ErrorAction SilentlyContinue)
        }
        foreach ($f in $fichiers) {
            $vu[$f.FullName] = "$($f.Length)|$($f.LastWriteTimeUtc.Ticks)"
        }
    }
    return $vu
}

function Verifier-Instantane {
    param([hashtable]$Avant, [string]$Racine)
    if ($Avant.Count -eq 0) { return }
    $apres = Instantane -Racine $Racine
    $abimes = @()
    foreach ($cle in $Avant.Keys) {
        if (-not $apres.ContainsKey($cle)) { $abimes += "disparu   $cle" ; continue }
        if ($apres[$cle] -ne $Avant[$cle]) { $abimes += "modifie   $cle" }
    }
    if ($abimes.Count -gt 0) {
        foreach ($a in $abimes) { Ecrire "   $a" 'Red' }
        Abandonner "$($abimes.Count) fichier(s) de donnees touche(s) par l'installation" `
                   'Ceci est un bug de ce script. Ne relancez pas : signalez-le.'
    }
    Ok "$($Avant.Count) fichier(s) de donnees intact(s)"
}


# -- Deploiement -------------------------------------------------------------

function Deployer {
    <#
    Ecrit tout ce que l'archive contient, ne supprime jamais rien d'autre.

    Une seule passe sur les entrees du zip, ecrasement autorise. Pas
    d'extraction dans un staging puis copie : ce serait 260 Mo d'ecritures pour
    130 Mo de paquet, sur des disques qui sont parfois lents.
    #>
    param([string]$Chemin, [string]$Racine, [int]$Total)

    # Le seul dossier qu'on vide avant d'ecrire -- cf. l'en-tete du fichier.
    $dist = Join-Path $Racine 'app\frontend\dist'
    if (Test-Path -LiteralPath $dist) {
        Remove-Item -LiteralPath $dist -Recurse -Force
        Note 'app\frontend\dist vide avant recopie (assets horodates par un hash)'
    }

    $env_relatif = 'app/backend/.env'
    $n = 0 ; $saute_env = $false
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Chemin)
    try {
        foreach ($e in $zip.Entries) {
            # Une entree de dossier a un nom qui finit par '/' et une taille
            # nulle : ZipFile n'en cree pas, mais une archive faite ailleurs
            # peut en contenir.
            if ($e.FullName.EndsWith('/')) { continue }

            $destination = Join-Path $Racine ($e.FullName -replace '/', '\')
            if (-not (Sous-Dossier -Racine $Racine -Candidat $destination)) {
                # Zip slip. L'archive vient de notre propre script, mais un
                # extracteur qui fait confiance a ses entrees est un extracteur
                # casse le jour ou l'archive ne vient plus de la.
                Abandonner "entree d'archive hors du dossier d'installation : $($e.FullName)"
            }

            if ($e.FullName -eq $env_relatif -and (Test-Path -LiteralPath $destination)) {
                # Il porte les cles d'API du destinataire (PUT /settings/api-keys
                # ecrit dedans). On ne l'ecrase pas ; on verifie seulement plus
                # bas qu'il eteint toujours l'Atelier.
                $saute_env = $true
                continue
            }

            $dossier = Split-Path -Parent $destination
            if (-not (Test-Path -LiteralPath $dossier)) {
                New-Item -ItemType Directory -Path $dossier -Force | Out-Null
            }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $destination, $true)

            $n++
            if ($Total -gt 0 -and ($n % 1000) -eq 0) {
                Info ("{0} / {1} fichiers" -f $n, $Total)
            }
        }
    } finally {
        $zip.Dispose()
    }
    Ok "$n fichiers ecrits"
    if ($saute_env) { Note 'app\backend\.env existant conserve (vos cles d API)' }
    return $saute_env
}

function Garder-Atelier-Eteint {
    <#
    Le .env conserve doit toujours porter EPURE_ATELIER=0.

    main.py lit os.environ.get("EPURE_ATELIER", "1") : le defaut est ACTIF. Un
    .env sans cette ligne rend donc les routes /workshop* joignables alors que
    l'ecran est absent du bundle -- l'ecart 5 de l'etape C, exactement, mais sur
    le chemin de la mise a jour ou personne ne l'avait cherche.
    #>
    param([string]$Racine)
    $env_fichier = Join-Path $Racine 'app\backend\.env'
    if (-not (Test-Path -LiteralPath $env_fichier)) { return }
    $contenu = Get-Content -LiteralPath $env_fichier -Raw -ErrorAction SilentlyContinue
    if ($contenu -match '(?m)^\s*EPURE_ATELIER\s*=\s*0\s*$') { return }
    $ajout = @(
        '',
        '# Ajoute par installer-epure.ps1 : sans cette ligne les routes de',
        "# l'Atelier redeviennent joignables alors que son ecran n'est pas",
        '# dans ce paquet.',
        'EPURE_ATELIER=0'
    )
    Add-Content -LiteralPath $env_fichier -Value $ajout -Encoding ascii
    Alerte 'EPURE_ATELIER=0 remis dans .env (il manquait)'
}


# -- Lanceur -----------------------------------------------------------------

function Ecrire-Lanceur {
    <#
    Genere Epure.cmd et demarrer.py. TOUJOURS regeneres, jamais des donnees.

    C'est le point de l'etape C : le lanceur d'un paquet livre etait ecrit a la
    main a chaque fois, donc absent de tous les paquets assembles avant celui-ci.
    Un fichier qu'il faut penser a ecrire est un fichier qui manque.

    Deux fichiers et non un :

    - `demarrer.py` porte tout ce qui se raisonne (attendre le port, ouvrir le
      navigateur, journaliser). Il tourne sous pythonw.exe, donc sans console.
    - `Epure.cmd` n'est qu'une ligne : il rend le lanceur double-cliquable dans
      l'explorateur. Le raccourci du Bureau, lui, vise pythonw.exe directement,
      ce qui evite le clignotement de console qu'un .cmd provoque.
    #>
    param([string]$Racine)

    $cmd = @(
        '@echo off',
        'rem ========================================================================',
        'rem  Epure -- lanceur. GENERE par installer-epure.ps1 : ne pas editer, une',
        'rem  reinstallation ecrase ce fichier.',
        'rem',
        'rem  ASCII pur : cmd.exe lit un .bat dans la page de codes OEM (850/437) et',
        'rem  pas en UTF-8. Un accent sortirait en mojibake dans un echo, et casserait',
        'rem  une commande.',
        'rem',
        'rem  pythonw et non python : pythonw.exe appartient au sous-systeme GUI, il',
        'rem  n a pas de console. Avec python.exe une fenetre noire resterait ouverte',
        'rem  toute la session.',
        'rem',
        'rem  start "" : sans lui cmd.exe attend la fin du programme lance, meme un',
        'rem  programme GUI, et la console de ce .cmd resterait ouverte -- ce qui',
        'rem  annulerait l interet de pythonw. Le "" est le TITRE de fenetre que start',
        'rem  reclame des que son premier argument est entre guillemets ; sans lui,',
        'rem  start prendrait le chemin pour un titre et ne lancerait rien.',
        'rem',
        'rem  %~dp0 : dossier de CE fichier. Le lanceur reste donc correct quel que',
        'rem  soit le repertoire courant, sans chemin absolu en dur.',
        'rem',
        'rem  RIEN N APPARAIT ? Lisez epure.log, a cote de ce fichier. Sous pythonw il',
        'rem  n y a pas de stderr : c est la seule trace d un demarrage qui echoue.',
        'rem ========================================================================',
        '',
        'start "" "%~dp0python\pythonw.exe" "%~dp0demarrer.py"'
    )
    $chemin_cmd = Join-Path $Racine 'Epure.cmd'
    Set-Content -LiteralPath $chemin_cmd -Value $cmd -Encoding ascii

    # ATTENTION : ce bloc est du code Python, ecrit tel quel dans demarrer.py.
    # Il est en @'...'@ (litteral) et non @"..."@ : sans cela PowerShell
    # developperait $..., c'est-a-dire toutes les variables Python.
    # backend/test_installeur.py l'extrait de ce fichier et le compile, pour
    # qu'une coquille ici soit rouge en CI et non chez le destinataire.
    $py = @'
"""Demarre Epure : uvicorn en loopback, navigateur quand le port repond.

GENERE par tools/installer-epure.ps1 -- ne pas editer, une reinstallation
ecrase ce fichier. ASCII pur, comme le script qui l ecrit.

PAS DE CONSOLE, ET TOUT DECOULE DE LA. Epure.cmd lance pythonw.exe : sys.stdout
et sys.stderr valent None. Un simple print() leverait AttributeError, et une
traceback n existerait nulle part. Les deux flux sont donc rediriges vers
epure.log des la premiere ligne utile, et uvicorn recoit log_config=None pour
qu il n installe pas ses propres handlers sur un stdout absent -- ses loggers
remontent alors a la racine, donc dans le meme fichier. Le filtre de
core/logs.py, pose par main.py sur uvicorn.access, continue de masquer le token
qui voyage en query param des WebSockets.

Le port est teste AVANT de demarrer uvicorn : avec un raccourci sur le Bureau,
double-lancer est le cas normal, et deux uvicorn sur le meme port donnent une
erreur de bind invisible. Si Epure repond deja, on ouvre seulement le
navigateur.
"""

import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

RACINE = Path(__file__).resolve().parent
BACKEND = RACINE / "app" / "backend"
HOTE = "127.0.0.1"
PORT = 8000
URL = "http://%s:%d/" % (HOTE, PORT)

_journal = open(str(RACINE / "epure.log"), "a", encoding="utf-8", buffering=1)
sys.stdout = _journal
sys.stderr = _journal
logging.basicConfig(
    stream=_journal, level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)


def deja_lance():
    """Epure repond-elle deja ? Teste /health, pas seulement le port.

    Le port 8000 est banal. Conclure << Epure tourne >> parce que quelque chose
    ecoute ouvrirait le navigateur sur le programme de quelqu un d autre.
    """
    try:
        with urllib.request.urlopen(URL + "health", timeout=2) as r:
            return r.status == 200 and b"ollama" in r.read(4096)
    except Exception:
        return False


def ouvrir_quand_pret():
    """Attend que le port reponde, puis ouvre le navigateur.

    Cinq minutes de patience : le premier demarrage dezippe des .pyd que
    l antivirus inspecte, et Smart App Control peut retarder le chargement d une
    DLL non signee de plusieurs minutes (cf. l en-tete de l installeur). Ouvrir
    le navigateur tout de suite afficherait une erreur de connexion sur une
    application qui demarre normalement.
    """
    for _ in range(600):
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((HOTE, PORT)) == 0:
                logging.info("port ouvert, ouverture du navigateur")
                webbrowser.open(URL)
                return
        time.sleep(0.5)
    logging.error("le serveur n a pas repondu apres 5 minutes -- voir plus haut")


def main():
    if deja_lance():
        logging.info("Epure repond deja sur %s -- ouverture du navigateur seul", URL)
        webbrowser.open(URL)
        return
    os.chdir(str(BACKEND))
    sys.path.insert(0, str(BACKEND))
    threading.Thread(target=ouvrir_quand_pret, daemon=True).start()
    import uvicorn
    logging.info("demarrage d uvicorn sur %s:%d", HOTE, PORT)
    uvicorn.run("main:app", host=HOTE, port=PORT, log_config=None)


main()
'@
    $chemin_py = Join-Path $Racine 'demarrer.py'
    Set-Content -LiteralPath $chemin_py -Value $py -Encoding ascii
    Ok 'Epure.cmd et demarrer.py generes'
}


# -- Echauffement ------------------------------------------------------------

function Executer-Natif {
    <#
    Lance un binaire, rend son code de sortie ET ses lignes, sans lever.

    Necessaire, et pas par prudence excessive : `$ErrorActionPreference = 'Stop'`
    transforme la sortie d erreur d un programme externe en erreur TERMINANTE
    (NativeCommandError en PowerShell 5.1), et PowerShell 7.4 va plus loin en
    faisant de tout code de sortie non nul une exception
    ($PSNativeCommandUseErrorActionPreference vaut $true par defaut). Or ici un
    code non nul est une INFORMATION -- c est le resultat qu on vient chercher.
    Sans cette fonction, l echauffement mourrait au premier module bloque au lieu
    de retenter, c est-a-dire precisement dans le cas pour lequel il existe.
    #>
    param([string]$Exe, [string[]]$Arguments)
    $prefEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $avaitPrefNatif = Test-Path 'variable:PSNativeCommandUseErrorActionPreference'
    if ($avaitPrefNatif) {
        $prefNatif = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $sortie = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        return @{ Code = $LASTEXITCODE; Sortie = @($sortie) }
    } finally {
        $ErrorActionPreference = $prefEA
        if ($avaitPrefNatif) {
            $PSNativeCommandUseErrorActionPreference = $prefNatif
        }
    }
}


function Echauffer {
    param([string]$Racine)
    $python = Join-Path $Racine 'python\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        Note 'pas de runtime Python dans ce paquet -- echauffement saute'
        return
    }
    $code = @'
import importlib
import importlib.util
import sys

echecs = []
for nom in sys.argv[1:]:
    if importlib.util.find_spec(nom) is None:
        print("absent   " + nom)
        continue
    try:
        importlib.import_module(nom)
        print("ok       " + nom)
    except Exception as exc:
        print("ECHEC    " + nom + " : " + str(exc))
        echecs.append(nom)
sys.exit(1 if echecs else 0)
'@
    foreach ($essai in 1, 2) {
        $r = Executer-Natif -Exe $python -Arguments (@('-c', $code) + $MODULES_NATIFS)
        $ok = ($r.Code -eq 0)
        foreach ($l in $r.Sortie) { Info "$l" }
        if ($ok) { Ok 'modules natifs charges' ; return }
        if ($essai -eq 1) {
            Alerte 'un module natif a refuse de se charger -- nouvelle tentative dans 20 s'
            Alerte 'Smart App Control evalue la reputation des DLL fraichement dezippees'
            Start-Sleep -Seconds 20
        }
    }
    Alerte 'echauffement incomplet. Si Epure ne demarre pas, relancez dans quelques'
    Alerte 'minutes : le blocage est temporaire (voir epure.log et installation.log).'
}


# -- Raccourci ---------------------------------------------------------------

function Poser-Raccourci {
    <#
    Raccourci Bureau visant pythonw.exe, pas Epure.cmd.

    Un raccourci vers un .cmd ouvre une console le temps du start, ce qui donne
    un clignotement noir a chaque lancement. pythonw.exe n en ouvre aucune.
    Epure.cmd reste dans le dossier pour qui l ouvre depuis l explorateur.
    #>
    param([string]$Racine, [string]$Ou)
    if (-not (Test-Path -LiteralPath $Ou)) {
        Alerte "dossier du raccourci introuvable ($Ou) -- raccourci non pose"
        return
    }
    # E accent aigu par code de caractere : ce fichier est en ASCII pur (cf.
    # l en-tete), et le nom du raccourci est ce que le destinataire lit.
    $nom = ([char]0xC9) + 'pure.lnk'
    $lien = Join-Path $Ou $nom
    $cible = Join-Path $Racine 'python\pythonw.exe'
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lien)
    $sc.TargetPath = $cible
    $sc.Arguments = '"' + (Join-Path $Racine 'demarrer.py') + '"'
    $sc.WorkingDirectory = $Racine
    $sc.IconLocation = "$cible,0"
    $sc.Description = 'Epure -- assistant d etude local'
    $sc.Save()
    Ok "raccourci : $lien"
}


# -- Deroulement -------------------------------------------------------------

Add-Type -AssemblyName System.IO.Compression.FileSystem

Ecrire ''
Ecrire '  Installation d Epure' 'White'
Ecrire '  --------------------' 'White'

$zip_chemin = Trouver-Archive
Titre 'Archive'
Info "fichier : $(Split-Path -Leaf $zip_chemin)"
Info ("taille  : {0:N1} Mo" -f ((Get-Item -LiteralPath $zip_chemin).Length / 1MB))
$total = Verifier-Archive -Chemin $zip_chemin
Ok "$total entrees, paquet Epure reconnu"

Titre 'Destination'
$mise_a_jour = Test-Path -LiteralPath (Join-Path $Cible 'PAQUET.json')
if (-not (Test-Path -LiteralPath $Cible)) {
    New-Item -ItemType Directory -Path $Cible -Force | Out-Null
}
$Cible = (Resolve-Path -LiteralPath $Cible).Path
Ouvrir-Journal -Dossier $Cible
Info "dossier : $Cible"
if ($mise_a_jour) {
    Ok 'installation existante detectee -- MISE A JOUR (rien ne sera supprime)'
    if (Deja-En-Marche) {
        Abandonner 'Epure est en cours d execution' `
                   'Quittez-la (clic droit sur la fenetre du navigateur ne suffit pas : le serveur tourne en arriere-plan, terminez pythonw.exe dans le Gestionnaire des taches), puis relancez.'
    }
} else {
    Ok 'premiere installation'
}

$avant = Instantane -Racine $Cible

Titre 'Copie'
Deployer -Chemin $zip_chemin -Racine $Cible -Total $total | Out-Null

# ORDRE IMPERATIF : la verification porte sur le DEPLOIEMENT, donc elle passe
# avant toute modification voulue. Remettre EPURE_ATELIER=0 dans le .env conserve
# est une ecriture legitime et annoncee ; la faire avant, c'etait faire echouer le
# garde-fou sur son propre travail -- ce qu'un test a constate immediatement.
Titre 'Donnees'
Verifier-Instantane -Avant $avant -Racine $Cible

Garder-Atelier-Eteint -Racine $Cible

Titre 'Lanceur'
Ecrire-Lanceur -Racine $Cible

if (-not $SansEchauffement) {
    Titre 'Modules natifs'
    Echauffer -Racine $Cible
}

if (-not $SansRaccourci) {
    Titre 'Raccourci'
    Poser-Raccourci -Racine $Cible -Ou $Bureau
}

Titre 'Fait'
Info "Epure est installee dans $Cible"
Info 'Lancez-la par le raccourci du Bureau, ou par Epure.cmd dans ce dossier.'
Info 'Journaux : installation.log (cette installation) et epure.log (execution).'

if (-not $SansLancement) {
    Ecrire ''
    Info 'ouverture d Epure...'
    Start-Process -FilePath (Join-Path $Cible 'python\pythonw.exe') `
                  -ArgumentList ('"' + (Join-Path $Cible 'demarrer.py') + '"') `
                  -WorkingDirectory $Cible
}

exit 0
