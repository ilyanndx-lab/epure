<#
.SYNOPSIS
    Relance Epure sur le poste de DEV apres un git pull. Rien a taper.

.DESCRIPTION
    Ce script n'est PAS le lanceur des destinataires. Trois lanceurs coexistent et
    ne visent pas les memes gens :

      epure_tray.py            usage normal sur ce poste : icone, Ollama, Vite,
                               uvicorn en arriere-plan, console masquee.
      Installer-Epure.cmd/.ps1 le destinataire d'un paquet livre. N'Y PAS TOUCHER.
      tools\dev-epure.ps1      CE fichier : le cycle "git pull -> ca doit marcher"
                               sur le checkout, logs au premier plan.

    POURQUOI IL EXISTE. Quatre pannes revenaient a chaque relance apres un pull,
    toujours les memes, toujours resolues a la main :

      1. un raccourci de bureau qui lance le mauvais interpreteur, ou depuis le
         mauvais dossier. Ce que l'inspection du bureau montre reellement :
         `epure.lnk` vise `...\WindowsApps\pythonw.exe` avec
         WorkingDirectory = `...\WindowsApps`. Il n'est PAS casse pour autant --
         `epure_tray.py` ancre ses chemins sur `Path(__file__)` et passe
         `cwd=BACKEND_DIR` a uvicorn, donc il survit a un repertoire courant
         faux. Ce qui reste vrai, et suffit : un raccourci qui NOMME un
         interpreteur et un dossier peut devenir faux sans que rien ne le
         signale, et diagnostiquer ca coute une soiree. Celui de ce script ne
         nomme ni l'un ni l'autre (cf. Poser-Raccourci) ;
      2. `npm ci` qui echoue en EPERM parce qu'un `node` residuel (un `tsc
         --watch`, un `vite` d'une session precedente) tient un fichier de
         node_modules ;
      3. `tsc` introuvable apres un `npm ci` interrompu -- node_modules existe
         mais est incomplet, et rien ne le signale avant le build ;
      4. le port 8000 encore tenu par l'uvicorn du lancement precedent.

    Chacune est un ARRET NET ici, avec la cause nommee, ou une reparation
    automatique -- jamais un echec silencieux.

    CE QU'IL NE FAIT PAS, et c'est delibere : il ne lance ni Ollama ni Vite. Le
    backend sert le frontend CONSTRUIT (resolve_web_dir -> frontend\dist, cf.
    CLAUDE.md 3.5), donc `npm run build` suffit pour voir l'interface a jour sur
    http://127.0.0.1:8000. Pour le dev frontend avec rechargement a chaud, c'est
    `epure_tray.py` ou `npm run dev` qu'il faut, pas ce script.

.PARAMETER SansPull
    Saute `git pull`. Pour relancer sans toucher au depot.

.PARAMETER SansInstall
    Saute `npm ci`. Utile quand seul le backend a bouge.

.PARAMETER SansBuild
    Saute `npm run build`.

.PARAMETER Diagnostic
    Joue TOUTES les verifications et s'arrete avant uvicorn. C'est le mode par
    lequel ce script a ete teste : il permet d'eprouver les quatre pannes sans
    occuper le port ni bloquer une console.

.PARAMETER PoserRaccourci
    (Re)cree le raccourci de bureau vers ce script, puis sort. Signale les
    raccourcis Epure existants qui pointent ailleurs -- il ne les supprime que si
    -RemplacerAnciens est passe aussi.

.PARAMETER RemplacerAnciens
    Avec -PoserRaccourci : supprime les raccourcis de bureau qui visent ce
    checkout autrement que par ce script.

.EXAMPLE
    .\tools\dev-epure.ps1
    Le cas normal : pull, install, build, liberation du port, uvicorn.

.EXAMPLE
    .\tools\dev-epure.ps1 -PoserRaccourci -RemplacerAnciens
    A jouer une fois, pour remplacer l'ancien raccourci obsolete.

.NOTES
    CE FICHIER DOIT RESTER EN ASCII PUR, et ce n'est pas une coquetterie.

    Il est lance par powershell.exe (Windows PowerShell 5.1), qui lit un .ps1
    sans BOM avec la page de code du systeme -- Windows-1252 ici, pas UTF-8. Un
    caractere hors ASCII y arrive donc mojibake, et deux d'entre eux ne sont pas
    seulement illisibles : le tiret cadratin U+2014 (octets E2 80 94) et le
    filet U+2500 (E2 94 80) produisent tous les deux un U+201D, que PowerShell
    accepte comme DELIMITEUR DE CHAINE a l'egal du guillemet droit.

    Mesure, pas deduit. La version d'avant portait 501 filets et 8 cadratins.
    Les filets etaient tous dans des commentaires, donc inoffensifs. Le cadratin
    de la ligne 226 (numerotation d'avant cette note) etait dans une chaine "..." : relu en cp1252 il la fermait,
    apres quoi l'apostrophe de "l'etape" ouvrait une chaine simple qui courait
    sur le reste du fichier. Bilan : 33 erreurs de parsing, la premiere
    annoncee ligne 253 -- une ligne strictement ASCII et sans defaut, qui ne
    faisait que subir le decalage. Corriger ce seul cadratin ramenait le total a
    30 et faisait disparaitre l'erreur de la ligne 253 ; le meme fichier lu par
    pwsh 7 (UTF-8 par defaut) parsait sans une seule erreur.

    D'ou la regle, deja tenue par install.ps1, tools/installer-epure.ps1 et
    demarrer.py : ASCII pur plutot qu'un BOM. Un BOM corrigerait la lecture,
    mais ce depot a deja paye le prix des BOM ailleurs (core/jsonstore.py lit en
    utf-8-sig pour cette raison), et l'ASCII marche sous n'importe quelle page
    de code, avec ou sans BOM. Verrouille par backend/test_encodage_scripts.py.
#>

[CmdletBinding()]
param(
    [switch]$SansPull,
    [switch]$SansInstall,
    [switch]$SansBuild,
    [switch]$Diagnostic,
    [switch]$PoserRaccourci,
    [switch]$RemplacerAnciens
)

$ErrorActionPreference = 'Stop'

# Racine du depot deduite de l'emplacement de CE fichier, jamais du repertoire
# courant : le script doit marcher depuis un raccourci, depuis n'importe ou.
# C'est la meme regle que core/paths.py (BACKEND_DIR ancre sur __file__).
$RACINE   = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$FRONTEND = Join-Path $RACINE 'frontend'
$BACKEND  = Join-Path $RACINE 'backend'
$PORT     = 8000

$script:Etape = 0

function Ecrire-Etape([string]$texte) {
    $script:Etape++
    Write-Host ''
    Write-Host ("[{0}] {1}" -f $script:Etape, $texte) -ForegroundColor Cyan
}

function Ecrire-Ok([string]$texte)    { Write-Host "    ok   $texte" -ForegroundColor DarkGray }
function Ecrire-Info([string]$texte)  { Write-Host "    ..   $texte" -ForegroundColor DarkGray }
function Ecrire-Alerte([string]$texte){ Write-Host "    !    $texte" -ForegroundColor Yellow }

function Arreter([string]$quoi, [string]$pourquoi) {
    # Un arret NOMME. Le but de ce script est de ne plus chercher pourquoi ca ne
    # demarre pas : le message doit dire quoi faire, pas seulement que ca a rate.
    Write-Host ''
    Write-Host "ECHEC : $quoi" -ForegroundColor Red
    if ($pourquoi) { Write-Host $pourquoi -ForegroundColor Red }
    exit 1
}


# -- Appel d'un binaire externe ------------------------------------------------

function Invoquer-Externe {
    <#
        Lance un binaire et rend sa sortie ET son code de sortie, sans qu'une
        ligne ecrite sur stderr puisse tuer le script.

        L'INCIDENT, et il ne se voit pas en relisant le site d'appel. Ce script
        pose $ErrorActionPreference = 'Stop' (il le doit : une commande PowerShell
        qui rate en silence ferait continuer un lanceur dont le but est justement
        d'arreter net). Or, sous Windows PowerShell 5.1 -- powershell.exe, celui
        que lance le raccourci de bureau, PAS pwsh -- une redirection 2>&1 sur un
        binaire natif convertit chaque ligne de son stderr en ErrorRecord, et
        'Stop' fait de cet ErrorRecord une erreur TERMINANTE. Le script meurt
        donc sur une ligne de stderr, meme quand le binaire se termine ensuite
        avec le code 0, et la ligne suivante -- le test sur $LASTEXITCODE, ecrit
        exactement pour decider de l'echec -- n'est jamais atteinte.

        MESURE, pas deduit. 'npm run build' reussissait (dist\index.html ecrit,
        'built in 2.89s') et le script s'arretait quand meme a l'etape 5 avec
        NativeCommandError, sur le seul avertissement de taille de chunk que
        Vite/Rolldown ecrit sur stderr. Reproduit hors du depot avec un binaire
        qui ecrit une ligne sur stderr puis sort en 0 : powershell.exe + 2>&1 +
        'Stop' meurt ; le meme appel sans 2>&1 survit ; le meme appel sous pwsh 7
        survit. D'ou l'invisibilite du bug : il ne se manifeste que dans l'hote
        qu'utilise le raccourci.

        LES SIX SITES ETAIENT ARMES, pas seulement le build -- 'git pull' ecrit
        sa progression ('From github.com...') sur stderr, 'npm ci' ses avis,
        'taskkill' ses refus, et un interpreteur Python sans fastapi sa trace.
        Le message 'ecarte (dependances absentes)' de Trouver-Python etait
        litteralement inatteignable. C'est pourquoi la correction est ici et non
        a l'etape 5 : un correctif local aurait laisse cinq mines en place.

        POURQUOI UNE FONCTION. L'affectation ci-dessous cree une variable de
        PORTEE DE FONCTION : la preference globale 'Stop' est retablie au retour,
        sans sauvegarde ni finally -- verifie ($ErrorActionPreference vaut bien
        'Stop' au retour). C'est ce qui permet de relacher la regle le temps d'un
        appel sans la relacher pour le script.

        Rend un objet plutot que la seule sortie, pour que l'appelant n'ait plus
        aucune raison de lire $LASTEXITCODE lui-meme :
            .Code    le code de sortie, LE SEUL juge de l'echec
            .Texte   sortie fusionnee, une chaine (pour -match et l'affichage)
            .Lignes  la meme, en tableau de lignes
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

# -- Interpreteur Python ------------------------------------------------------

function Trouver-Python {
    <#
        Le premier interpreteur qui a REELLEMENT les dependances.

        Panne 1 de l'en-tete : un lanceur qui prend `python` du PATH tombe sur ce
        qui vient en premier, y compris l'alias du Microsoft Store. On ne devine
        pas -- on teste `import fastapi, uvicorn` sur chaque candidat et on prend
        celui qui repond. Un python sans les deps produit sinon un ModuleNotFound
        au demarrage d'uvicorn, plusieurs etapes trop tard.

        $env:EPURE_PYTHON passe devant : c'est le seul moyen de forcer un venv.
    #>
    $candidats = @()
    if ($env:EPURE_PYTHON) { $candidats += $env:EPURE_PYTHON }
    $duPath = @(Get-Command python.exe -All -ErrorAction SilentlyContinue |
                ForEach-Object { $_.Source }) | Where-Object { $_ }
    # Les alias du Microsoft Store passent EN DERNIER, jamais exclus.
    #
    # Sur ce poste, `...\WindowsApps\python.exe` a bien les dependances (mesure :
    # meme 3.14.5 que les autres, `import fastapi` passe) -- ce n'est donc pas un
    # faux python et l'ecarter serait faux. Mais c'est un shim : il redirige, et
    # sur une machine ou l'application Store n'est pas installee il ouvre le Store
    # au lieu de lancer python. Preferer un chemin reel quand il y en a un evite
    # ce detour sans rien casser quand il est le seul disponible.
    # `-like` et non `-match` : le motif contient des antislashes, que `-match`
    # interpreterait comme une regex (`\W` = "non-mot", et un antislash final
    # rend le motif invalide -- erreur de parsing observee en ecrivant ceci).
    $reels = @($duPath | Where-Object { $_ -notlike '*\WindowsApps\*' })
    $shims = @($duPath | Where-Object { $_ -like    '*\WindowsApps\*' })
    $candidats += $reels + $shims
    $candidats = $candidats | Where-Object { $_ } | Select-Object -Unique

    foreach ($c in $candidats) {
        if (-not (Test-Path $c)) { continue }
        $r = Invoquer-Externe $c -c "import fastapi, uvicorn; print('ok')"
        if ($r.Code -eq 0 -and $r.Texte -eq 'ok') { return $c }
        Ecrire-Info "ecarte (dependances absentes) : $c"
    }
    Arreter "aucun interpreteur Python n'a les dependances d'Epure" @"
Candidats essayes :
$($candidats -join "`n")

Installer les dependances dans l'un d'eux :
    <python> -m pip install -r "$BACKEND\requirements.txt"
Ou designer le bon :
    `$env:EPURE_PYTHON = 'C:\chemin\vers\python.exe'
"@
}

# -- 1. Processus node residuels ----------------------------------------------

function Trouver-NodeDuCheckout {
    <#
        Les process qui tiennent un fichier de NOTRE node_modules.

        Deux criteres, et pas un kill de tous les `node` de la machine -- ce
        serait tuer VS Code, un autre projet, un serveur qui tourne a cote :

          a. la ligne de commande mentionne le chemin du checkout. Mesure sur ce
             poste : un `tsc --watch` reel a pour CommandLine
             "node" "C:\...\epure\frontend\node_modules\.bin\..\typescript\bin\tsc",
             donc il est reconnaissable ;
          b. l'executable lui-meme est SOUS le checkout (certains paquets
             installent un .exe dans node_modules, qui se verrouille tout seul).
             Critere irrefutable : si le binaire vient de mon node_modules, il est
             a moi.

        LIMITE CONNUE, a ne pas se cacher : un `node` lance depuis le checkout
        dont la ligne de commande ne le mentionne pas (`node -e "..."`, mesure)
        echappe aux deux criteres. Il n'existe pas d'API Windows simple pour lire
        le repertoire courant d'un autre processus. Si npm echoue quand meme en
        EPERM, l'etape 3 nettoie node_modules -- c'est le filet.
    #>
    $tousNode = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue)
    $autres   = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                  Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($RACINE, 'OrdinalIgnoreCase') })

    $suspects = @()
    foreach ($p in $tousNode) {
        if ($p.CommandLine -and $p.CommandLine.ToLower().Contains($RACINE.ToLower())) {
            $suspects += $p
        }
    }
    $suspects += $autres
    return @($suspects | Sort-Object ProcessId -Unique)
}

function Liberer-NodeModules([switch]$Imbrique) {
    # `-Imbrique` : appelee depuis la reparation EPERM, cette fonction ne doit pas
    # consommer un numero d'etape -- observe en testant, l'affichage annoncait une
    # "etape 4" au milieu de l'etape 3 et donnait a lire deux deroules
    # differents pour un seul.
    if ($Imbrique) { Ecrire-Info 'processus node residuels' } else { Ecrire-Etape 'Processus node residuels' }
    $suspects = Trouver-NodeDuCheckout
    if ($suspects.Count -eq 0) { Ecrire-Ok 'aucun'; return }

    foreach ($p in $suspects) {
        $quoi = if ($p.CommandLine) { $p.CommandLine } else { $p.ExecutablePath }
        if ($quoi.Length -gt 110) { $quoi = $quoi.Substring(0, 110) + '...' }
        Ecrire-Alerte "PID $($p.ProcessId) tient ce checkout : $quoi"
        # /T : les descendants aussi. Un `npm run` laisse un shell parent dont la
        # mort seule laisserait node vivant (meme raison que lanceur.tuer_arbre).
        $null = Invoquer-Externe taskkill /F /T /PID $p.ProcessId
    }
    Start-Sleep -Milliseconds 400
    $restants = Trouver-NodeDuCheckout
    if ($restants.Count -gt 0) {
        Ecrire-Alerte "$($restants.Count) process resistent -- l'etape npm nettoiera node_modules si besoin"
    } else {
        Ecrire-Ok "$($suspects.Count) process termine(s)"
    }
}

# -- 2. git pull --------------------------------------------------------------


function Tete-Git {
    <#
        Le SHA de HEAD, ou un arret nomme. Passe par Invoquer-Externe comme tout
        le reste : sans code de sortie verifie, un `git rev-parse` qui rate rend
        $null, et le `.Trim()` d'alors echouait sur "You cannot call a method on
        a null-valued expression" -- un message qui ne dit rien de git.
    #>
    $r = Invoquer-Externe git rev-parse HEAD
    if ($r.Code -ne 0) { Arreter 'git rev-parse HEAD a echoue' $r.Texte }
    return $r.Texte
}

function Mettre-A-Jour {
    Ecrire-Etape 'git pull'
    if ($SansPull) { Ecrire-Info 'saute (-SansPull)'; return }

    Push-Location $RACINE
    try {
        $etat = Invoquer-Externe git status --porcelain
        if ($etat.Code -ne 0) { Arreter 'git status a echoue' $etat.Texte }
        if ($etat.Texte) {
            # On ne tente RIEN sur un arbre sale : un pull qui echoue a mi-chemin
            # sur un conflit laisse un etat que ce script ne sait pas demeler, et
            # le demeler a sa place risquerait du travail non commite.
            Ecrire-Alerte 'arbre de travail modifie -- pull saute'
            ($etat.Lignes | Select-Object -First 8) | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkGray }
            return
        }
        $avant = Tete-Git
        $pull = Invoquer-Externe git pull --ff-only
        if ($pull.Code -ne 0) {
            Arreter 'git pull a echoue' "$($pull.Texte)`n`nA regler a la main (divergence, reseau, authentification)."
        }
        $apres = Tete-Git
        if ($avant -eq $apres) {
            Ecrire-Ok "deja a jour ($($apres.Substring(0,7)))"
        } else {
            Ecrire-Ok "$($avant.Substring(0,7)) -> $($apres.Substring(0,7))"
        }
    } finally { Pop-Location }
}

# -- 3. npm ci, avec reparation EPERM -----------------------------------------

function Installer-Dependances {
    Ecrire-Etape 'npm ci (frontend)'
    if ($SansInstall) { Ecrire-Info 'saute (-SansInstall)'; return }

    $modules = Join-Path $FRONTEND 'node_modules'
    Push-Location $FRONTEND
    try {
        foreach ($essai in 1, 2) {
            Ecrire-Info "essai $essai/2"
            $ci = Invoquer-Externe npm ci
            if ($ci.Code -eq 0) { Ecrire-Ok 'installe'; return }

            $texte = $ci.Texte
            Write-Host $texte -ForegroundColor DarkGray

            if ($essai -eq 2) {
                Arreter 'npm ci a echoue deux fois' 'La seconde tentative partait de node_modules supprime : la cause n''est pas un verrou.'
            }
            # EPERM/EBUSY = un fichier verrouille. Le seul remede fiable est de
            # repartir de zero : npm ne sait pas ecraser ce qu'un autre process
            # tient, et un `npm ci` interrompu laisse en plus un node_modules
            # incomplet (panne 3 de l'en-tete : `tsc` introuvable au build).
            if ($texte -match 'EPERM|EBUSY|EACCES|operation not permitted') {
                Ecrire-Alerte 'EPERM/EBUSY -- suppression de node_modules puis nouvelle tentative'
                Liberer-NodeModules -Imbrique
                if (Test-Path $modules) {
                    Remove-Item $modules -Recurse -Force -ErrorAction SilentlyContinue
                    if (Test-Path $modules) {
                        Arreter 'node_modules ne peut pas etre supprime' @"
Un processus le tient encore et ce script ne l'a pas identifie.
A faire : fermer VS Code / les terminaux ouverts sur $FRONTEND, puis relancer.
"@
                    }
                    Ecrire-Ok 'node_modules supprime'
                }
            } else {
                Arreter 'npm ci a echoue' 'La sortie ci-dessus ne mentionne pas de verrou (EPERM/EBUSY) : ce n''est pas un residu de process.'
            }
        }
    } finally { Pop-Location }
}

function Verifier-Outils {
    <#
        Panne 3 : un `npm ci` interrompu laisse node_modules present mais
        incomplet, et `npm run build` echoue plus loin sur un binaire manquant.
        On verifie ici que les deux outils du build repondent, pendant qu'on peut
        encore le dire clairement.
    #>
    Ecrire-Etape 'Outils du build'
    Push-Location $FRONTEND
    try {
        foreach ($outil in 'tsc', 'vite') {
            $bin = Join-Path $FRONTEND "node_modules\.bin\$outil.cmd"
            if (-not (Test-Path $bin)) {
                # Pas de backtick dans un here-string INTERPOLANT (@" "@) : PowerShell
                # y lit `n comme un retour a la ligne, et le mot "npm" precede
                # d'un backtick s'affichait "\n pm". Trouve en testant cette
                # panne pour de vrai, pas en relisant.
                Arreter "$outil est introuvable dans node_modules" @"
node_modules existe mais est incomplet -- typiquement un 'npm ci' interrompu.
A faire : .\tools\dev-epure.ps1   (l'etape npm ci le reinstallera)
Ou, si ca se reproduit : supprimer $FRONTEND\node_modules et relancer.
"@
            }
        }
        Ecrire-Ok 'tsc et vite presents'
    } finally { Pop-Location }
}

# -- 4. npm run build --------------------------------------------------------

function Construire-Interface {
    Ecrire-Etape 'npm run build'
    if ($SansBuild) { Ecrire-Info 'saute (-SansBuild)'; return }

    Push-Location $FRONTEND
    try {
        $build = Invoquer-Externe npm run build
        if ($build.Code -ne 0) {
            Write-Host $build.Texte -ForegroundColor DarkGray
            Arreter 'npm run build a echoue' 'Erreur de compilation : elle est au-dessus, elle vient du code, pas du lanceur.'
        }
        # `index.html` et non le seul code de sortie : c'est ce fichier que
        # `main._register_web` cherche pour decider de servir l'interface. Sans
        # lui, le backend demarre en mode developpement et 127.0.0.1:8000 rend une
        # page vide -- un symptome qui ne ressemble pas a un build rate.
        $index = Join-Path $FRONTEND 'dist\index.html'
        if (-not (Test-Path $index)) {
            Arreter 'le build est passe mais dist\index.html est absent' 'Le backend servirait alors une page vide.'
        }
        Ecrire-Ok "dist\index.html ($([math]::Round((Get-Item $index).Length / 1KB, 1)) Ko)"
    } finally { Pop-Location }
}

# -- 5. Port 8000 ------------------------------------------------------------

function Liberer-Port([string]$python) {
    <#
        Panne 4 : l'uvicorn du lancement precedent tient encore le port.

        La logique n'est PAS reecrite ici : `lanceur.py` la porte deja, avec 37
        tests (backend/test_lanceur.py), et elle identifie un backend Epure par
        son COMPORTEMENT -- `/health` est la seule route ouverte sans token et sa
        forme est reconnaissable. C'est ce qui autorise a liberer le port sans
        tuer le processus de quelqu'un d'autre. Deux implementations de cette
        decision divergeraient, et celle qui se tromperait tuerait un process
        etranger.
    #>
    Ecrire-Etape "Port $PORT"
    $code = @"
import sys
sys.path.insert(0, r'$RACINE')
import lanceur
pid = lanceur.port_occupant($PORT)
if pid is None:
    print('LIBRE')
elif lanceur.backend_epure_repond($PORT):
    lanceur.tuer_arbre(pid)
    print('EPURE_TUE %d' % pid)
else:
    print('ETRANGER %d %s' % (pid, lanceur.nom_processus(pid)))
"@
    $verdict = (Invoquer-Externe $python -c $code).Texte
    $mots = $verdict -split '\s+'
    switch ($mots[0]) {
        'LIBRE'      { Ecrire-Ok 'libre' }
        'EPURE_TUE'  {
            Ecrire-Alerte "backend Epure residuel (PID $($mots[1])) -- arrete"
            Start-Sleep -Milliseconds 600
        }
        'ETRANGER'   {
            Arreter "le port $PORT est pris par un processus qui n'est pas Epure" @"
PID $($mots[1]) : $($mots[2])
Il n'est PAS tue : /health n'a pas repondu comme un backend Epure, donc il
appartient a autre chose. A regler a la main, ou changer de port.
"@
        }
        default      { Arreter "verification du port impossible" $verdict }
    }
}

# -- 6. uvicorn au premier plan ----------------------------------------------

function Lancer-Uvicorn([string]$python) {
    Ecrire-Etape 'uvicorn (premier plan)'
    Write-Host "    interface : http://127.0.0.1:$PORT" -ForegroundColor Green
    Write-Host '    Ctrl+C pour arreter' -ForegroundColor DarkGray
    Write-Host ''
    # cwd = backend\ : `main:app` est resolu par rapport au repertoire courant.
    # C'est le defaut que le raccourci obsolete de ce poste n'avait pas
    # (WorkingDirectory = ...\WindowsApps), et le symptome etait un
    # ModuleNotFoundError sur `main` -- pas un message parlant.
    Set-Location $BACKEND
    # SEUL appel natif qui reste hors Invoquer-Externe, et c'est voulu :
    # uvicorn journalise sur stderr en continu, et le but de cette etape est
    # de VOIR ces lignes en direct. Les capturer les avalerait. Sans
    # redirection 2>&1, powershell.exe ne les convertit pas en ErrorRecord
    # (mesure), donc 'Stop' ne les rend pas terminantes -- c'est la
    # redirection qui armait le piege, pas le stderr. Cf. Invoquer-Externe.
    & $python -m uvicorn main:app --host 127.0.0.1 --port $PORT
}

# -- Raccourci de bureau -----------------------------------------------------

function Poser-Raccourci {
    <#
        Un raccourci qui pointe sur CE script, pas sur un interpreteur.

        C'est la reponse a la panne 1 : le raccourci ne connait plus ni le python
        ni le repertoire de travail d'uvicorn. Il lance le script, qui les
        determine a chaque fois. Un raccourci ne peut donc plus devenir obsolete
        sans que le script le devienne aussi.

        `-NoExit` : sur un arret (Ctrl+C) ou une erreur, la console reste ouverte.
        Sans lui, un echec d'une des cinq etapes ferme la fenetre avant qu'on ait
        lu la cause -- ce qui est exactement le probleme qu'on cherche a supprimer.
    #>
    $bureau = [Environment]::GetFolderPath('Desktop')
    $cible  = Join-Path $bureau 'Epure (dev).lnk'
    $shell  = New-Object -ComObject WScript.Shell

    $lnk = $shell.CreateShortcut($cible)
    $lnk.TargetPath       = (Get-Command powershell.exe).Source
    $lnk.Arguments        = "-NoExit -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $lnk.WorkingDirectory = $RACINE
    $lnk.Description      = 'Epure -- dev : git pull, build, uvicorn au premier plan'
    $lnk.IconLocation     = "$((Get-Command powershell.exe).Source),0"
    $lnk.Save()
    Write-Host "Raccourci ecrit : $cible" -ForegroundColor Green

    # Les autres raccourcis qui visent ce checkout : signales, supprimes seulement
    # sur demande. En effacer un sans le dire ferait disparaitre un lanceur que
    # l'utilisateur voulait peut-etre garder (epure_tray.py en est un).
    $anciens = @()
    foreach ($l in @(Get-ChildItem $bureau -Filter *.lnk -ErrorAction SilentlyContinue)) {
        if ($l.FullName -eq $cible) { continue }
        $t = $shell.CreateShortcut($l.FullName)
        $texte = "$($t.TargetPath) $($t.Arguments)"
        if ($texte.ToLower().Contains($RACINE.ToLower())) { $anciens += ,@($l, $texte) }
    }
    if ($anciens.Count -eq 0) { return }

    Write-Host ''
    Write-Host "$($anciens.Count) autre(s) raccourci(s) visent ce checkout :" -ForegroundColor Yellow
    foreach ($a in $anciens) {
        Write-Host "    $($a[0].Name)  ->  $($a[1])" -ForegroundColor DarkGray
    }
    if ($RemplacerAnciens) {
        foreach ($a in $anciens) {
            Remove-Item $a[0].FullName -Force -ErrorAction SilentlyContinue
            Write-Host "    supprime : $($a[0].Name)" -ForegroundColor Yellow
        }
    } else {
        Write-Host '    conserves. -RemplacerAnciens pour les supprimer.' -ForegroundColor DarkGray
    }
}

# -- Deroule -----------------------------------------------------------------

Write-Host ''
Write-Host 'Epure -- relance de developpement' -ForegroundColor White
Write-Host "  depot : $RACINE" -ForegroundColor DarkGray

if ($PoserRaccourci) { Poser-Raccourci; exit 0 }

$python = Trouver-Python
Write-Host "  python : $python" -ForegroundColor DarkGray

Liberer-NodeModules
Mettre-A-Jour
Installer-Dependances
Verifier-Outils
Construire-Interface
Liberer-Port $python

if ($Diagnostic) {
    Write-Host ''
    Write-Host 'Diagnostic : toutes les verifications sont passees, uvicorn non lance.' -ForegroundColor Green
    exit 0
}

Lancer-Uvicorn $python
