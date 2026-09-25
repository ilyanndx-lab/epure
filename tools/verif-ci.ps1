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
      l'utilisateur. Les fichiers SUIVIS de `frontend/` sont donc recopies
      (`git ls-files` : ni `node_modules`, jointe par une JONCTION, ni les
      modules installes, ignores par git) et rien n'est ecrit dans l'arbre de
      travail.

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

    # Version de Node : ci.yml ne la porte plus en dur, il pointe un fichier
    # (`node-version-file`). On lit le MEME fichier que la CI, jamais une
    # valeur recopiee ici.
    $m = [regex]::Match($texte, "(?m)^\s*node-version-file:\s*(?<f>[^\s#]+)")
    if (-not $m.Success) {
        Arreter "impossible de lire node-version-file dans ci.yml" @"
La CI ne declare plus sa version de Node par un fichier. Ce script refuse de
deviner laquelle elle utilise : corriger le motif, ou remettre la cle.
"@
    }
    $fichierNode = Join-Path $REPO $m.Groups['f'].Value

    return [pscustomobject]@{ Cliquet = $cliquet; CommandeBackend = $cmd; FichierNode = $fichierNode }
}

function Verifier-VersionNode([string]$fichier) {
    <#
        Compare la version MAJEURE de Node locale a celle que la CI installe.
        Incident a l'origine : CI en 22, poste en 24, 9 tests verts ici et
        rouges la-bas pendant des jours -- aucune etape locale ne pouvait le
        voir, puisque toutes tournent sous le Node du poste. Une etape comme
        les autres (verdict, code de retour), pas un simple avertissement.
    #>
    $nom = "Version de Node (locale vs $(Split-Path -Leaf $fichier))"
    Ecrire-Titre $nom
    if (-not (Test-Path $fichier)) { Arreter "fichier de version introuvable : $fichier" }
    $attendue = ((Get-Content $fichier -Raw).Trim() -replace '^v', '').Split('.')[0]
    $r = Invoquer-Externe "node" @('--version')
    $locale = if ($r.Code -eq 0) { ($r.Texte.Trim() -replace '^v', '').Split('.')[0] } else { "?" }
    $ok = ($locale -eq $attendue)
    if ($ok) {
        Write-Host ("   OK (Node {0} ici comme en CI)" -f $r.Texte.Trim()) -ForegroundColor Green
    } else {
        Write-Host ("   ECHEC : Node {0} ici, {1} en CI -- installer Node {1}, ou changer frontend/.nvmrc (source de verite de la CI)" -f $r.Texte.Trim(), $attendue) -ForegroundColor Red
    }
    $script:Resultats += [pscustomobject]@{ Nom = $nom; Ok = $ok; Duree = 0 }
}

# -- actionlint ---------------------------------------------------------------

function Obtenir-Actionlint {
    <#
        Chemin d'un actionlint.exe a la version et a l'empreinte que la CI
        epingle dans `.github/workflows/actionlint.yml` -- LUES dans ce fichier,
        jamais recopiees ici. Telecharge une fois dans %LOCALAPPDATA%, pas
        d'installation systeme.

        POURQUOI. Un workflow invalide ne demarre pas, et la PR n'affiche alors
        aucun check a son nom : `paquet-voix.yml` l'a fait le 2026-09-25 (un
        `runner.temp` dans le `env:` du job), et ce script etait vert, parce
        qu'il ne lisait que ci.yml.
    #>
    $yml = Join-Path $REPO ".github\workflows\actionlint.yml"
    if (-not (Test-Path $yml)) { Arreter "actionlint.yml introuvable : $yml" }
    $texte = Get-Content $yml -Raw
    $v = [regex]::Match($texte, '(?m)^\s*ACTIONLINT_VERSION:\s*"?(?<v>[0-9.]+)"?\s*$')
    $h = [regex]::Match($texte, '(?m)^\s*ACTIONLINT_SHA256_WINDOWS:\s*"?(?<h>[0-9a-f]{64})"?\s*$')
    if (-not $v.Success -or -not $h.Success) {
        Arreter "impossible de lire ACTIONLINT_VERSION / ACTIONLINT_SHA256_WINDOWS dans actionlint.yml"
    }
    $version = $v.Groups['v'].Value
    $empreinte = $h.Groups['h'].Value
    $dossier = Join-Path $env:LOCALAPPDATA "epure-outils\actionlint-$version"
    $exe = Join-Path $dossier "actionlint.exe"
    if (Test-Path $exe) { return $exe }

    $archive = "actionlint_${version}_windows_amd64.zip"
    $url = "https://github.com/rhysd/actionlint/releases/download/v$version/$archive"
    $tmp = Join-Path $env:TEMP ("epure-" + [guid]::NewGuid().ToString('N').Substring(0, 8) + "-" + $archive)
    Ecrire-Info "telechargement de $url"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp
        $vue = (Get-FileHash -Algorithm SHA256 -LiteralPath $tmp).Hash.ToLowerInvariant()
        if ($vue -ne $empreinte) { Arreter "empreinte d'actionlint inattendue" "attendue $empreinte, obtenue $vue" }
        New-Item -ItemType Directory -Path $dossier -Force | Out-Null
        # ZipFile et non Expand-Archive : l'extraction d'un seul membre, a un
        # niveau connu (cf. docs/claude/pieges-connus.md, Expand-Archive).
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($tmp)
        try {
            $membre = $zip.Entries | Where-Object { $_.Name -eq 'actionlint.exe' } | Select-Object -First 1
            if (-not $membre) { Arreter "actionlint.exe absent de $archive" }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($membre, $exe, $true)
        } finally { $zip.Dispose() }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    return $exe
}

# -- Replique temporaire du frontend ------------------------------------------

function Copier-Suivis([string]$sous_dossier, [string]$racine) {
    <#
        Copie sous `$racine` les seuls fichiers de `$sous_dossier` que git SUIT
        (`git ls-files`), contenu de l'arbre de travail compris.

        POURQUOI. La replique copiait tout `frontend/` sauf `node_modules`,
        `dist` et `.vite`, donc aussi les modules que l'Atelier installe dans
        `src/modules/generated/<id>/` -- ignores par git, absents du clone de la
        CI. Mesure le 2026-09-25 : 60 avertissements ici contre 59 en CI, le
        60e dans `generated/slides/`, et un verif-ci rouge sur un commit que la
        CI aurait passe.

        POURQUOI PAS eslint QUI RESPECTE `.gitignore`. La CI installe le
        catalogue dans ces memes dossiers ignores et DOIT les linter : un
        eslint qui les ecarterait sortirait le catalogue du perimetre, en CI
        comme ici, et le cliquet mesurerait moins de code sans rien dire. C'est
        la COPIE qui doit ressembler au clone ; l'installation simulee du
        catalogue vient ensuite, a l'identique de ci.yml.

        Consequence assumee : un fichier neuf pas encore `git add` n'est pas vu.
        C'est aussi le cas de la CI.
    #>
    $sortie = & git -C $REPO -c core.quotepath=off ls-files -- $sous_dossier
    if ($LASTEXITCODE -ne 0) { Arreter "git ls-files a echoue sur $sous_dossier" "Lancer ce script depuis un checkout git." }
    $n = 0
    foreach ($relatif in $sortie) {
        $source = Join-Path $REPO $relatif
        # Suivi mais supprime dans l'arbre de travail : absent, comme apres commit.
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
        $cible = Join-Path $racine $relatif
        $parent = Split-Path -Parent $cible
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -LiteralPath $source -Destination $cible -Force
        $n++
    }
    if ($n -eq 0) { Arreter "aucun fichier suivi sous $sous_dossier" "La replique serait vide : verif-ci ne mesurerait rien." }
}

function Construire-Replique {
    <#
        Copie les fichiers suivis de `frontend/` dans un temporaire
        (`Copier-Suivis` : ni `node_modules`, joint par une jonction, ni `dist`,
        ni les modules installes sur ce poste), puis y installe le catalogue
        comme le font les deux etapes "installation simulee" de ci.yml.

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

    Copier-Suivis "frontend" $racine

    $modules = Join-Path $DOSSIER_FRONTEND "node_modules"
    if (-not (Test-Path $modules)) {
        Arreter "frontend/node_modules absent" "Lancer d'abord : cd frontend ; npm ci"
    }
    New-Item -ItemType Junction -Path (Join-Path $front "node_modules") -Target $modules | Out-Null

    # Le catalogue est lu en `../modules-catalogue/*/` par les etapes de ci.yml :
    # il doit donc etre a cote de la replique, pas a cote de l'original.
    Copier-Suivis "modules-catalogue" $racine

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
    # Dans les deux modes : quelques dixiemes de seconde, et c'est ce qui dit
    # si les AUTRES workflows que ci.yml demarreront seulement. Memes options
    # que le job de actionlint.yml (shellcheck/pyflakes coupes -- cf. son en-tete).
    $actionlint = Obtenir-Actionlint
    $null = Etape "actionlint (.github/workflows)" $REPO $actionlint @('-no-color', '-shellcheck=', '-pyflakes=')

    if ($Frontend) {
        Verifier-VersionNode $ci.FichierNode

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
        # Les guillemets qui entourent un motif dans ci.yml sont retires APRES
        # la decoupe, jamais avant. Le `.Replace("'", '"')` de Lire-Reference
        # ne fait que changer l'espece de guillemet : le token reste
        # `"test_*.py"`, guillemets COMPRIS, et PowerShell le passe tel quel a
        # python -- qui cherche alors des fichiers dont le nom commence par un
        # guillemet. Symptome mesure : `Ran 0 tests` / `NO TESTS RAN`, code 5,
        # en 0,4 s. La pire forme d'echec apres celle qui rend un succes : une
        # etape rouge qui ne dit rien de ce qu'elle mesurait, et qui aurait tout
        # aussi bien pu etre verte a vide si l'absence de test n'etait pas une
        # erreur pour unittest.
        $morceaux = $ci.CommandeBackend -split '\s+' | ForEach-Object { $_ -replace '^"(.*)"$', '$1' }
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
