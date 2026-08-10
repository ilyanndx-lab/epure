<#
.SYNOPSIS
    Installe Epure et ses prerequis sur Windows 10/11. Relancable.

.DESCRIPTION
    Chaque etape est precedee d'une detection et annonce ce qu'elle a trouve :
    un script qui travaille en silence pendant vingt minutes est indistinguable
    d'un script bloque.

    IDEMPOTENT. Le relancer sur une installation existante ne casse rien et sert
    de mise a jour. Rien n'est supprime, aucun fichier existant n'est ecrase.

.PARAMETER DryRun
    N'execute rien : affiche ce qui serait fait, etape par etape. A lancer en
    premier pour relire le script sur sa propre machine avant de le laisser
    agir.

.NOTES
    CONTENU EN ASCII PUR, VOLONTAIREMENT. PowerShell 5.1 - celui livre avec
    Windows - lit un fichier UTF-8 SANS BOM comme de l'ANSI. Un tiret cadratin
    y devient une sequence de trois caracteres dont le dernier est un guillemet
    fermant typographique, que PowerShell accepte comme delimiteur de chaine :
    le script entier cesse d'etre analysable, sur une erreur qui pointe une
    ligne sans rapport. Mesure faite ici meme, premiere version du script :
    << Le terminateur " est manquant dans la chaine >> a la ligne 320, pour un
    tiret pose ligne 60. Meme regle que Epure.bat, pour une raison voisine.

    LIMITE DECLAREE : ce script n'a JAMAIS tourne sur une machine vierge. Il a
    ete ecrit et verifie sur un poste ou tout est deja installe, ou chaque
    detection repond << deja present >> - ce qui ne prouve rien des chemins
    d'installation. Le premier vrai test sera la premiere vraie installation.
    Lancez-le d'abord avec -DryRun.

    Identifiants winget verifies le 2026-08-10 par `winget search --exact` :
      Python.Python.3.12  -> Python 3.12    (3.12.10)
      OpenJS.NodeJS.LTS   -> Node.js (LTS)  (24.19.0)
      Ollama.Ollama       -> Ollama         (0.32.6)
    Python 3.12 et non << la derniere >> : c'est la version que valide la CI.

.EXAMPLE
    .\install.ps1 -DryRun
    .\install.ps1
#>

[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$RACINE  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BACKEND = Join-Path $RACINE 'backend'
$FRONT   = Join-Path $RACINE 'frontend'
$JOURNAL = Join-Path $RACINE 'install.log'

$script:Echecs = @()

# -- Sortie -------------------------------------------------------------------

function Ecrire {
    param([string]$Texte, [string]$Couleur = 'Gray')
    Write-Host $Texte -ForegroundColor $Couleur
    $horodatage = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $JOURNAL -Value "[$horodatage] $Texte" -Encoding utf8
}

function Titre  { param([string]$t) Ecrire "" ; Ecrire "== $t" 'Cyan' }
function Info   { param([string]$t) Ecrire "   $t" 'Gray' }
function Ok     { param([string]$t) Ecrire "   OK   $t" 'Green' }
function Saute  { param([string]$t) Ecrire "   --   $t" 'DarkGray' }
function Alerte { param([string]$t) Ecrire "   !    $t" 'Yellow' }

function Echec {
    param([string]$Quoi, [string]$Commande)
    Ecrire "   ECHEC  $Quoi" 'Red'
    Ecrire "          a relancer a la main : $Commande" 'Red'
    $script:Echecs += $Quoi
}

# -- Aides --------------------------------------------------------------------

function Existe-Commande {
    param([string]$Nom)
    $c = Get-Command $Nom -ErrorAction SilentlyContinue
    if ($c) { return $c.Source } else { return $null }
}

function Rafraichir-Path {
    <#
      Un paquet installe par winget n'est PAS sur le PATH du processus courant :
      winget met a jour le PATH du systeme et de l'utilisateur, pas celui d'un
      shell deja ouvert. Sans cette relecture, `python` reste introuvable juste
      apres avoir ete installe, et l'etape suivante echoue pour une raison qui
      n'a rien a voir avec elle.
    #>
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $usager  = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $usager) | Where-Object { $_ }) -join ';'
}

function Installer-Winget {
    param([string]$Id, [string]$Nom)
    if ($DryRun) { Saute "installerait $Nom ($Id)" ; return $true }
    Info "installation de $Nom, cela peut prendre plusieurs minutes..."
    winget install --id $Id --exact --silent `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Echec "$Nom non installe (winget code $LASTEXITCODE)" "winget install --id $Id --exact"
        return $false
    }
    Rafraichir-Path
    Ok "$Nom installe"
    return $true
}

function Modele-Par-Defaut {
    <#
      Nom du modele lu dans backend/config.yaml, jamais code en dur : le script
      doit rester juste quand le defaut change. PowerShell 5.1 n'a pas de
      lecteur YAML, mais on ne cherche qu'une cle a un niveau connu - `name:`
      dans le bloc `model:`.
    #>
    $cfg = Join-Path $BACKEND 'config.yaml'
    if (-not (Test-Path $cfg)) { return $null }
    $dansModel = $false
    foreach ($ligne in Get-Content $cfg -Encoding utf8) {
        if ($ligne -match '^\s*#') { continue }
        if ($ligne -match '^model:\s*$') { $dansModel = $true ; continue }
        if ($dansModel) {
            if ($ligne -match '^\S') { break }
            if ($ligne -match '^\s+name:\s*(.+?)\s*$') { return $Matches[1] }
        }
    }
    return $null
}

# -- Debut --------------------------------------------------------------------

Ecrire ""
Ecrire "  Epure - installation" 'White'
Ecrire "  racine  : $RACINE" 'DarkGray'
Ecrire "  journal : $JOURNAL" 'DarkGray'
if ($DryRun) {
    Ecrire ""
    Ecrire "  MODE -DryRun : rien ne sera installe ni modifie." 'Yellow'
}
Ecrire ""
Ecrire "  A prevoir : environ 5 Go de telechargement (modele Ollama ~4,7 Go," 'DarkGray'
Ecrire "  dependances Python et Node). Les modeles d'analyse documentaire" 'DarkGray'
Ecrire "  arrivent en plus, au premier demarrage." 'DarkGray'

# -- 1. winget ----------------------------------------------------------------

Titre "1/9  winget"
if (-not (Existe-Commande 'winget')) {
    Ecrire "   winget est introuvable." 'Red'
    Ecrire "   Il faut Windows 10 version 1809 ou plus recent, et l'application" 'Red'
    Ecrire "   'Installateur d'application' du Microsoft Store :" 'Red'
    Ecrire "   https://apps.microsoft.com/detail/9nblggh4nns1" 'Red'
    Ecrire "   Installez-la, rouvrez PowerShell, puis relancez ce script." 'Red'
    exit 1
}
Ok "present ($(winget --version))"

# -- 2. Python ----------------------------------------------------------------

Titre "2/9  Python 3.12+"
$python = Existe-Commande 'python'
$versionPy = $null
if ($python) {
    try { $versionPy = (& python --version 2>&1) -replace '^Python\s+', '' } catch { $versionPy = $null }
}
if ($versionPy -and ([version]($versionPy -split '\+')[0] -ge [version]'3.12')) {
    Ok "Python $versionPy deja present, ignore"
} else {
    if ($versionPy) { Alerte "Python $versionPy trop ancien" } else { Info "Python absent" }
    [void](Installer-Winget 'Python.Python.3.12' 'Python 3.12')
}

# -- 3. Node ------------------------------------------------------------------

Titre "3/9  Node.js 20+"
$node = Existe-Commande 'node'
$versionNode = $null
if ($node) {
    try { $versionNode = (& node --version) -replace '^v', '' } catch { $versionNode = $null }
}
if ($versionNode -and ([version]$versionNode -ge [version]'20.0.0')) {
    Ok "Node $versionNode deja present, ignore"
} else {
    if ($versionNode) { Alerte "Node $versionNode trop ancien" } else { Info "Node absent" }
    [void](Installer-Winget 'OpenJS.NodeJS.LTS' 'Node.js (LTS)')
}

# -- 4. Ollama ----------------------------------------------------------------

Titre "4/9  Ollama"
if (Existe-Commande 'ollama') {
    Ok "deja present, ignore"
} else {
    Info "absent"
    [void](Installer-Winget 'Ollama.Ollama' 'Ollama')
}

# -- 5. Modele local ----------------------------------------------------------

Titre "5/9  Modele local"
$modele = Modele-Par-Defaut
if (-not $modele) {
    Alerte "nom du modele illisible dans backend/config.yaml - etape ignoree"
} elseif (-not (Existe-Commande 'ollama')) {
    Alerte "Ollama indisponible dans cette session - rouvrez PowerShell et relancez"
} else {
    Info "modele attendu par backend/config.yaml : $modele"
    $listeOllama = ''
    try { $listeOllama = (& ollama list 2>&1 | Out-String) } catch { $listeOllama = '' }
    if ($listeOllama -match [regex]::Escape($modele)) {
        Ok "$modele deja telecharge, ignore"
    } elseif ($DryRun) {
        Saute "telechargerait $modele (~4,7 Go)"
    } else {
        Info "telechargement de $modele (~4,7 Go), soyez patient..."
        & ollama pull $modele
        if ($LASTEXITCODE -ne 0) { Echec "modele $modele non telecharge" "ollama pull $modele" }
        else { Ok "$modele pret" }
    }
}

# -- 6. Dependances Python ----------------------------------------------------

Titre "6/9  Dependances Python"
$requirements = Join-Path $BACKEND 'requirements.txt'
if (-not (Test-Path $requirements)) {
    Echec "backend/requirements.txt introuvable" "verifiez que le depot est complet"
} elseif ($DryRun) {
    Saute "lancerait : python -m pip install -r backend\requirements.txt"
} elseif (-not (Existe-Commande 'python')) {
    Echec "python indisponible dans cette session" "rouvrez PowerShell puis relancez .\install.ps1"
} else {
    Info "installation (plusieurs minutes, ~1,6 Go)..."
    & python -m pip install --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) { Echec "dependances Python" "python -m pip install -r backend\requirements.txt" }
    else { Ok "dependances Python installees" }
}

# -- 7. Dependances Node ------------------------------------------------------

Titre "7/9  Dependances Node"
if (-not (Test-Path (Join-Path $FRONT 'package-lock.json'))) {
    Echec "frontend/package-lock.json introuvable" "verifiez que le depot est complet"
} elseif ($DryRun) {
    Saute "lancerait : npm ci   (dans frontend\)"
} elseif (-not (Existe-Commande 'npm')) {
    Echec "npm indisponible dans cette session" "rouvrez PowerShell puis relancez .\install.ps1"
} else {
    Push-Location $FRONT
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { Echec "dependances Node" "cd frontend puis npm ci" }
        else { Ok "dependances Node installees" }
    } finally { Pop-Location }
}

# -- 8. Fichier .env ----------------------------------------------------------

Titre "8/9  Configuration (.env)"
$env_exemple = Join-Path $BACKEND '.env.example'
$env_reel    = Join-Path $BACKEND '.env'
if (Test-Path $env_reel) {
    # Jamais d'ecrasement : ce fichier contient les cles API de l'utilisateur.
    Ok "backend\.env existe deja - laisse intact"
} elseif (-not (Test-Path $env_exemple)) {
    Alerte "backend\.env.example introuvable - etape ignoree"
} elseif ($DryRun) {
    Saute "copierait backend\.env.example vers backend\.env"
} else {
    Copy-Item $env_exemple $env_reel
    Ok "backend\.env cree depuis l'exemple (toutes les cles sont optionnelles)"
}

# -- 9. Raccourci Bureau ------------------------------------------------------

Titre "9/9  Raccourci sur le Bureau"
$cible = Join-Path $RACINE 'Epure.bat'
if (-not (Test-Path $cible)) {
    Echec "Epure.bat introuvable" "verifiez que le depot est complet"
} else {
    $bureau = [Environment]::GetFolderPath('Desktop')
    # Le E accentu construit par son point de code : le libelle visible porte
    # l'accent sans qu'un octet non-ASCII entre dans ce fichier. La CIBLE, elle,
    # reste Epure.bat en ASCII.
    $nomAffiche = [string][char]0x00C9 + 'pure.lnk'
    $raccourci = Join-Path $bureau $nomAffiche
    if ($DryRun) {
        Saute "creerait le raccourci : $raccourci -> $cible"
    } else {
        try {
            $shell = New-Object -ComObject WScript.Shell
            $lnk = $shell.CreateShortcut($raccourci)
            $lnk.TargetPath       = $cible
            $lnk.WorkingDirectory = $RACINE
            $lnk.Description      = 'Epure - assistant local'
            $lnk.Save()
            Ok "raccourci cree sur le Bureau"
        } catch {
            Echec "raccourci non cree ($($_.Exception.Message))" "creez-le a la main vers $cible"
        }
    }
}

# -- Bilan --------------------------------------------------------------------

Ecrire ""
if ($script:Echecs.Count -gt 0) {
    Ecrire "  Termine avec $($script:Echecs.Count) echec(s) :" 'Red'
    foreach ($e in $script:Echecs) { Ecrire "    - $e" 'Red' }
    Ecrire "  Detail complet dans $JOURNAL" 'Red'
    exit 1
}

if ($DryRun) {
    Ecrire "  -DryRun termine : rien n'a ete modifie." 'Yellow'
    Ecrire "  Relancez sans -DryRun pour installer." 'Yellow'
} else {
    Ecrire "  Installation terminee." 'Green'
    Ecrire ""
    Ecrire "  Lancez Epure par le raccourci du Bureau, ou en double-cliquant" 'Green'
    Ecrire "  sur Epure.bat a la racine du dossier." 'Green'
    Ecrire ""
    Ecrire "  Au premier demarrage, le backend telecharge ses modeles d'analyse :" 'DarkGray'
    Ecrire "  comptez quelques minutes avant que tout reponde." 'DarkGray'
    Ecrire "  Si rien n'apparait, le seul endroit ou regarder est epure_tray.log." 'DarkGray'
}
exit 0
