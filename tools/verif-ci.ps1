<#
    verif-ci.ps1 -- reproduit sur ce poste ce que MESURE la CI, avant de pousser.

    POURQUOI CE SCRIPT EXISTE. Deux fois dans la meme journee, un "vert en
    local" s'est revele rouge en CI, sur deux axes independants :

      1. `backend/modules/` -- la suite locale copie le vrai dossier de modules,
         qui contient sur un poste de dev les modules installes depuis le
         catalogue. `test_catalogue` echouait donc ici et passait en CI, ou le
         clone n'a que les modules versionnes. (Corrige dans `_test_env`.)
      2. Le perimetre eslint -- la CI installe TOUT le catalogue dans
         `frontend/src/modules/generated/` avant de linter, et compte donc les
         avertissements de six composants que `npm run lint` local ne voit pas.
         Mesure : 51 avertissements ici, 62 la-bas, pour un cliquet a 61.

    La cause commune n'est aucun de ces deux defauts : c'est qu'AUCUNE commande
    locale ne reproduisait le perimetre de la CI. Deux corrections ponctuelles
    auraient laisse le troisieme axe en embuscade.

    CE QUI REND CE SCRIPT FIABLE, et sans quoi il serait pire que rien :

    * il LIT `.github/workflows/ci.yml` au lieu de le paraphraser. Le cliquet
      eslint et la commande de test backend en sont extraits a l'execution. Si
      la lecture echoue -- ci.yml reecrit, cle renommee -- le script S'ARRETE en
      le disant, au lieu de retomber sur une valeur par defaut : un controle qui
      mesure silencieusement autre chose que la CI est exactement le defaut
      qu'on supprime ici.
    * il travaille dans un arbre TEMPORAIRE. La CI, elle, installe le catalogue
      dans `frontend/src/modules/generated/` puis fait `rm -rf` dessus -- sur ce
      poste, ce `rm -rf` emporterait les modules reellement installes par
      l'utilisateur. `frontend/` est donc recopie (sans `node_modules`, jointe
      par une JONCTION) et rien n'est ecrit dans l'arbre de travail.

    USAGE

        .\tools\verif-ci.ps1              # tout
        .\tools\verif-ci.ps1 -Frontend    # frontend seul
        .\tools\verif-ci.ps1 -Backend     # backend seul

    Code de retour non nul des qu'une etape echoue.

    ASCII PUR -- comme tout `.ps1` versionne (cf. `test_encodage_scripts.py`) :
    `powershell.exe` 5.1 lit un `.ps1` sans BOM en cp1252, ou le tiret cadratin
    devient un guillemet fermant qui termine une chaine ouverte.
#>

[CmdletBinding()]
param(
    [switch]$Frontend,
    [switch]$Backend
)

$ErrorActionPreference = 'Stop'

# Noms DISTINCTS de ceux des parametres, et pas par cosmetique : PowerShell est
# insensible a la casse, donc `$DOSSIER_FRONTEND` et le switch `$Frontend` sont UNE SEULE
# variable. Y affecter un chemin violait le type du parametre et le script
# mourait avant sa premiere ligne utile, sur un message qui ne nomme aucune des
# deux ("Impossible de convertir System.String en SwitchParameter").
$REPO = Split-Path -Parent $PSScriptRoot
$CI_YML = Join-Path $REPO ".github\workflows\ci.yml"
$DOSSIER_FRONTEND = Join-Path $REPO "frontend"
$DOSSIER_BACKEND = Join-Path $REPO "backend"
$DOSSIER_CATALOGUE = Join-Path $REPO "modules-catalogue"

# Sans drapeau, on fait tout : c'est le mode "avant de pousser".
if (-not $Frontend -and -not $Backend) { $Frontend = $true; $Backend = $true }

$script:Resultats = @()

# -- Sorties ------------------------------------------------------------------

function Ecrire-Titre([string]$t) {
    Write-Host ""
    Write-Host "== $t" -ForegroundColor Cyan
}

function Ecrire-Info([string]$t) { Write-Host "   $t" -ForegroundColor DarkGray }

function Arreter([string]$message, [string]$detail = "") {
    Write-Host ""
    Write-Host "ARRET : $message" -ForegroundColor Red
    if ($detail) { Write-Host $detail -ForegroundColor DarkGray }
    exit 2
}

# -- Lancement d'un binaire ---------------------------------------------------

function Invoquer-Externe {
    <#
        Copie fonctionnelle de `tools/dev-epure.ps1` -- meme incident, meme
        remede. Sous `powershell.exe` 5.1, une redirection `2>&1` sur un binaire
        NATIF convertit chaque ligne de son stderr en ErrorRecord, et
        `$ErrorActionPreference = 'Stop'` en fait une erreur TERMINANTE : le
        script meurt sur une commande qui a REUSSI, avant meme le test sur le
        code de sortie. `npm run build` (avertissement de taille de chunk),
        `npm ci`, `python -m unittest` (qui ecrit TOUT sur stderr) sont tous
        armes. L'affectation ci-dessous cree une variable de portee de FONCTION :
        la preference globale est retablie au retour, sans finally.

        Volontairement DUPLIQUEE et non importee : ce script doit pouvoir etre
        lance seul, et `dev-epure.ps1` fait tout autre chose (il lance le dev).
        Un `. dev-epure.ps1` executerait son corps.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Binaire,
        [Parameter(ValueFromRemainingArguments = $true)]$Arguments = @()
    )
    $ErrorActionPreference = 'Continue'
    $sortie = & $Binaire @Arguments 2>&1
    $code = $LASTEXITCODE
    $lignes = @($sortie | ForEach-Object { "$_" })
    return [pscustomobject]@{
        Code   = $code
        Texte  = ($lignes -join [Environment]::NewLine).Trim()
        Lignes = $lignes
    }
}

function Etape {
    <#
        Joue une etape, enregistre son verdict, et rend $true/$false.
        La sortie complete n'est affichee QU'EN CAS D'ECHEC : un run vert doit
        tenir a l'ecran, sinon personne ne le relit.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Nom,
        [Parameter(Mandatory = $true)][string]$Dossier,
        [Parameter(Mandatory = $true)][string]$Binaire,
        # PAS `$Args` : c'est une variable AUTOMATIQUE de PowerShell, et un
        # parametre qui porte ce nom ne se lie jamais -- mesure : il reste vide,
        # en silence, et l'etape lancerait le binaire sans aucun argument.
        [Parameter(Mandatory = $true)][string[]]$Parametres
    )
    Ecrire-Titre $Nom
    Ecrire-Info ("{0} {1}" -f $Binaire, ($Parametres -join ' '))
    $depart = Get-Date
    Push-Location $Dossier
    try { $r = Invoquer-Externe $Binaire @Parametres } finally { Pop-Location }
    $duree = [math]::Round(((Get-Date) - $depart).TotalSeconds, 1)
    $ok = ($r.Code -eq 0)
    if ($ok) {
        Write-Host ("   OK ({0}s)" -f $duree) -ForegroundColor Green
    } else {
        Write-Host ("   ECHEC (code {0}, {1}s)" -f $r.Code, $duree) -ForegroundColor Red
        Write-Host $r.Texte
    }
    $script:Resultats += [pscustomobject]@{ Nom = $Nom; Ok = $ok; Duree = $duree }
    return $ok
}

# -- Lecture de ci.yml --------------------------------------------------------

function Lire-CiYml {
    <#
        Extrait de ci.yml les valeurs que la CI impose. RIEN n'est recopie ici :
        une valeur en dur dans ce script recreerait, un cran plus loin, l'ecart
        local/CI que le script existe pour supprimer.

        Chaque extraction ARRETE le script si elle echoue. Un repli sur une
        valeur par defaut serait le pire des comportements : le script
        continuerait a afficher "OK" en mesurant autre chose.
    #>
    if (-not (Test-Path $CI_YML)) { Arreter "ci.yml introuvable : $CI_YML" }
    $texte = Get-Content $CI_YML -Raw

    $m = [regex]::Match($texte, '(?m)^\s*ESLINT_MAX_WARNINGS:\s*"?(?<n>\d+)"?\s*$')
    if (-not $m.Success) {
        Arreter "impossible de lire ESLINT_MAX_WARNINGS dans ci.yml" @"
La reference a change de forme. Ce script refuse de deviner : corriger le motif
ci-dessus, ou remettre la cle en 'env:' de workflow.
"@
    }
    $cliquet = [int]$m.Groups['n'].Value

    # Pas d'ancre `$` finale : ci.yml est en CRLF sur ce poste, et `(?m)$` ne se
    # place qu'avant le `\n`. La classe `[^\r\n]*` s'arretant avant le `\r`,
    # l'ancre ne pouvait jamais coller -- la lecture echouait sur un fichier
    # parfaitement conforme, et le script s'arretait (ce qui est le bon reflexe,
    # mais pour une mauvaise raison).
    $m = [regex]::Match($texte, "(?m)^\s*run:\s*(?<cmd>python -m unittest discover[^\r\n]*)")
    if (-not $m.Success) {
        Arreter "impossible de lire la commande de tests backend dans ci.yml" @"
Attendu une etape 'run: python -m unittest discover ...'. Si la CI a change de
lanceur, ce script doit changer avec elle -- c'est tout son interet.
"@
    }
    # La CI ecrit les motifs entre guillemets simples ; on les retire pour
    # repasser les arguments a un binaire Windows sans qu'ils y restent colles.
    $cmd = $m.Groups['cmd'].Value.Trim().Replace("'", '"')

    return [pscustomobject]@{ Cliquet = $cliquet; CommandeBackend = $cmd }
}

# -- Replique temporaire du frontend ------------------------------------------

function Construire-Replique {
    <#
        Copie `frontend/` dans un temporaire, SANS `node_modules` (jointe par une
        jonction) ni `dist`, puis y installe le catalogue comme le font les deux
        etapes "installation simulee" de ci.yml.

        POURQUOI UN TEMPORAIRE, et c'est le coeur du script. La CI copie les
        composants du catalogue dans `frontend/src/modules/generated/<id>/` puis
        fait `rm -rf` dessus. Sur un runner, ces dossiers n'existaient pas. Sur
        CE poste, `generated/code/` est un module REELLEMENT installe : rejouer
        la CI dans l'arbre de travail l'ecraserait puis le supprimerait.

        POURQUOI UNE JONCTION et pas une copie de `node_modules` : plusieurs
        centaines de Mo et des dizaines de milliers de fichiers. Une jonction de
        dossier se cree SANS droits d'administrateur (contrairement a un lien
        symbolique), et `npx`/`npm` resolvent leurs binaires au travers --
        verifie sur ce poste avant d'ecrire ce script.
    #>
    $racine = Join-Path $env:TEMP ("epure-verif-ci-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $front = Join-Path $racine "frontend"
    New-Item -ItemType Directory -Path $front -Force | Out-Null

    $exclus = @('node_modules', 'dist', '.vite')
    Get-ChildItem -LiteralPath $DOSSIER_FRONTEND -Force |
        Where-Object { $exclus -notcontains $_.Name } |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $front -Recurse -Force }

    $modules = Join-Path $DOSSIER_FRONTEND "node_modules"
    if (-not (Test-Path $modules)) {
        Arreter "frontend/node_modules absent" "Lancer d'abord : cd frontend ; npm ci"
    }
    New-Item -ItemType Junction -Path (Join-Path $front "node_modules") -Target $modules | Out-Null

    # Le catalogue est lu en `../modules-catalogue/*/` par les etapes de ci.yml :
    # il doit donc etre a cote de la replique, pas a cote de l'original.
    Copy-Item -LiteralPath $DOSSIER_CATALOGUE -Destination (Join-Path $racine "modules-catalogue") -Recurse -Force

    # Les deux etapes "installation simulee" (type-check/build et eslint) font
    # exactement cette copie. Une seule fois ici : elles sont identiques, et rien
    # ne les separe puisque la desinstallation intermediaire n'existe pas.
    $genere = Join-Path $front "src\modules\generated"
    $poses = @()
    foreach ($d in Get-ChildItem -LiteralPath (Join-Path $racine "modules-catalogue") -Directory) {
        $composant = Join-Path $d.FullName "Component.tsx"
        if (-not (Test-Path $composant)) { continue }
        $cible = Join-Path $genere $d.Name
        New-Item -ItemType Directory -Path $cible -Force | Out-Null
        Copy-Item -LiteralPath $composant -Destination (Join-Path $cible "Component.tsx") -Force
        $poses += $d.Name
    }

    return [pscustomobject]@{ Racine = $racine; Front = $front; Installes = $poses }
}

function Detruire-Replique([string]$racine) {
    if (-not $racine -or -not (Test-Path $racine)) { return }
    # La jonction se supprime comme un dossier vide : `Remove-Item -Recurse` la
    # traverserait sur certaines versions et effacerait le VRAI node_modules.
    # On la retire d'abord, explicitement, avec l'API qui ne suit pas le lien.
    $jonction = Join-Path $racine "frontend\node_modules"
    if (Test-Path $jonction) { [System.IO.Directory]::Delete($jonction, $false) }
    Remove-Item -LiteralPath $racine -Recurse -Force -ErrorAction SilentlyContinue
}

# -- Deroule ------------------------------------------------------------------

$ci = Lire-CiYml
Write-Host "Reference : $CI_YML" -ForegroundColor DarkGray
Write-Host ("Cliquet eslint lu dans ci.yml : {0}" -f $ci.Cliquet) -ForegroundColor DarkGray

$replique = $null
try {
    if ($Frontend) {
        Ecrire-Titre "Replique du frontend (arbre temporaire)"
        $replique = Construire-Replique
        Ecrire-Info $replique.Front
        Ecrire-Info ("catalogue installe : " + ($replique.Installes -join ', '))

        # Ordre : les portes rapides d'abord, le build (le plus lent) ensuite.
        # Un echec de typage n'a pas besoin d'attendre un bundling.
        $null = Etape "Type-check (npx tsc -b)" $replique.Front "npx" @('tsc', '-b')
        $null = Etape "Tests de composants (npm test)" $replique.Front "npm" @('test')
        $null = Etape "eslint (cliquet $($ci.Cliquet), src + catalogue)" $replique.Front `
            "npm" @('run', 'lint', '--', '--max-warnings', "$($ci.Cliquet)")
        $null = Etape "Build (npm run build)" $replique.Front "npm" @('run', 'build')
    }

    if ($Backend) {
        $morceaux = $ci.CommandeBackend -split '\s+'
        $binaire = $morceaux[0]
        $arguments = @($morceaux[1..($morceaux.Length - 1)])
        $null = Etape "Tests backend ($($ci.CommandeBackend))" $DOSSIER_BACKEND $binaire $arguments
    }
}
finally {
    if ($replique) { Detruire-Replique $replique.Racine }
}

# -- Verdict ------------------------------------------------------------------

Write-Host ""
Write-Host "== Verdict" -ForegroundColor Cyan
foreach ($r in $script:Resultats) {
    $etat = if ($r.Ok) { "OK   " } else { "ECHEC" }
    $couleur = if ($r.Ok) { "Green" } else { "Red" }
    Write-Host ("   {0}  {1,6}s  {2}" -f $etat, $r.Duree, $r.Nom) -ForegroundColor $couleur
}

$echecs = @($script:Resultats | Where-Object { -not $_.Ok })
if ($echecs.Count -gt 0) {
    Write-Host ""
    Write-Host ("{0} etape(s) en echec -- la CI dirait la meme chose." -f $echecs.Count) -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host "Tout est vert dans le perimetre de la CI." -ForegroundColor Green
exit 0
