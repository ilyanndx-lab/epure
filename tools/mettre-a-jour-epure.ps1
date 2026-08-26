<#
.SYNOPSIS
    Met Epure a jour de bout en bout sur la machine du destinataire. Rien a taper.

.DESCRIPTION
    QUATRIEME lanceur du depot, et le confondre avec les trois autres coute une
    soiree. Chacun vise un public different :

      epure_tray.py                usage normal : icone, Ollama, Vite, console
                                   masquee.
      tools\dev-epure.ps1          le poste de developpement, apres un git pull :
                                   pull, npm ci, build, port, uvicorn au premier
                                   plan.
      tools\Installer-Epure.cmd    le destinataire qui recoit une ARCHIVE. Il
      + installer-epure.ps1        dezippe et installe. N Y PAS TOUCHER.
      tools\Mettre-A-Jour-Epure.cmd CE script : le destinataire qui a le DEPOT et
      + ce fichier                 refait le cycle complet chez lui, code source
                                   compris.

    POURQUOI IL EXISTE. Sur la machine cible, Smart App Control bloque
    durablement `git-remote-https.exe` et `libcurl-4.dll` : `git pull` est donc
    inutilisable. Chaque iteration demandait une demi-douzaine de commandes
    PowerShell copiees-collees, avec les erreurs que ca implique chez quelqu un
    qui n est pas developpeur. Ce script enchaine les cinq etapes, s arrete net a
    la premiere qui echoue, et nomme laquelle.

    LES CINQ ETAPES :

      1. code a jour     git pull si possible, sinon l archive main.zip
      2. dependances     npm.cmd install dans frontend\
      3. paquet          python tools\faire_paquet.py --arch arm64 ...
      4. arret           l instance en cours, AVANT d ecraser ses fichiers
      5. installation    dist-paquets\Installer-Epure.cmd

.PARAMETER Destinataire
    Nom court qui nomme l archive produite. Defaut : sandr.

.PARAMETER Modules
    Modules du catalogue a embarquer, separes par des virgules. Defaut : docs.

.PARAMETER Arch
    Architecture de la machine CIBLE. Defaut : arm64. Le build croise est refuse
    par faire_paquet.py lui-meme (il execute le python.exe de la cible), donc
    cette valeur doit etre celle de la machine qui joue ce script.

.PARAMETER SansCode
    Saute l etape 1. Pour reconstruire sans retelecharger le code.

.PARAMETER SansInstall
    Saute l etape 5. Produit le paquet sans l installer.

.EXAMPLE
    Double-clic sur tools\Mettre-A-Jour-Epure.cmd

.NOTES
    CE FICHIER DOIT RESTER EN ASCII PUR, ET NE JAMAIS PASSER DE CODE A
    `python -c`. Ce ne sont pas des coquetteries : ce sont les trois pieges de
    Windows PowerShell 5.1 que ce depot a payes en deux jours, et ce script est
    lance par `powershell.exe`, donc par 5.1.

      1. cp1252. Un .ps1 sans BOM est lu avec la page de code du systeme. Le
         tiret cadratin U+2014 et le filet U+2500 y produisent tous deux un
         U+201D, que PowerShell traite comme un DELIMITEUR DE CHAINE : une
         chaine ouverte par " peut donc etre fermee par lui. Mesure sur
         dev-epure.ps1 : 33 erreurs de parsing, la premiere annoncee sur une
         ligne strictement ASCII. Verrouille par test_encodage_scripts.py.

      2. stderr. Sous $ErrorActionPreference = 'Stop', une redirection 2>&1 sur
         un binaire NATIF convertit chaque ligne de stderr en ErrorRecord, donc
         en erreur TERMINANTE -- meme quand le binaire sort en 0. D ou
         `Invoquer-Externe`, qui relache la preference en portee de FONCTION.
         Verrouille par test_dev_epure.py.

      3. guillemets. La ligne de commande d un binaire natif est reconstruite
         selon CommandLineToArgvW, et 5.1 n echappe pas les " internes d un
         argument : `python -c 'print("x")'` arrive `print(x)`. L echauffement
         de l installeur n a jamais fonctionne pour cette raison. Ici, aucun
         code Python n est passe en ligne de commande -- on appelle des
         FICHIERS .py. Verrouille par test_installeur.py.

    Verrouille aussi par backend/test_mise_a_jour.py, qui rejoue le piege de
    l extraction imbriquee decrit sur `Appliquer-Archive`.
#>

[CmdletBinding()]
param(
    [string]$Destinataire = 'sandr',
    [string]$Modules = 'docs',
    [string]$Arch = 'arm64',
    [switch]$SansCode,
    [switch]$SansInstall
)

$ErrorActionPreference = 'Stop'

# Racine deduite de l emplacement de CE fichier, jamais du repertoire courant :
# un .cmd double-clique depuis l explorateur peut demarrer n importe ou, et
# tools\ n est pas la racine. Meme regle que dev-epure.ps1 et core/paths.py.
$RACINE   = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$FRONTEND = Join-Path $RACINE 'frontend'
$ARCHIVE_URL = 'https://github.com/ilyanndx-lab/epure/archive/refs/heads/main.zip'

#: Noms de PREMIER NIVEAU que la mise a jour ne remplace jamais, meme si
#: l archive venait a en contenir un.
#:
#: DES NOMS DE PREMIER NIVEAU, ET RIEN D AUTRE -- la comparaison porte sur
#: `$entree.Name`. Y ecrire `backend\memory` serait une ligne qui ne compare
#: jamais rien : elle se lirait comme une garantie et n en serait pas une.
#:
#: CE QUI PROTEGE LES DONNEES IMBRIQUEES est autre chose, et c est la propriete
#: centrale de `Appliquer-Archive` : ON ECRASE, ON NE SUPPRIME JAMAIS. Le depot
#: ne contient ni `backend\.env`, ni `backend\memory`, ni `vector_db`, ni
#: `embedding_model`, ni `node_modules` -- l archive non plus, donc rien ne les
#: touche. Verifie par `test_mise_a_jour.py`, qui pose de vraies donnees dans une
#: fausse racine et regarde ce qui en reste.
$HORS_MISE_A_JOUR = @(
    '.git',
    'dist-paquets',
    'data',
    'workspace'
)

$script:Etape = 0


# -- Sortie ------------------------------------------------------------------
# Meme vocabulaire que installer-epure.ps1 : le destinataire lit les deux, et
# deux grammaires de messages pour un meme geste, c est une de trop.

function Titre {
    param([string]$t)
    $script:Etape++
    Write-Host ''
    Write-Host ("== [{0}/5] {1}" -f $script:Etape, $t) -ForegroundColor Cyan
}
function Info   { param([string]$t) Write-Host "   $t" -ForegroundColor Gray }
function Ok     { param([string]$t) Write-Host "   OK   $t" -ForegroundColor Green }
function Note   { param([string]$t) Write-Host "   --   $t" -ForegroundColor DarkGray }
function Alerte { param([string]$t) Write-Host "   !    $t" -ForegroundColor Yellow }

function Abandonner {
    <#
    Un arret NOMME. Le but de ce script est que le destinataire n ait pas a
    deviner : le message doit dire A QUELLE ETAPE ca a casse et quoi faire, pas
    seulement que ca a rate. Il pourra copier-coller ce qui precede.
    #>
    param([string]$Quoi, [string]$Pourquoi = '')
    Write-Host ''
    Write-Host ("ECHEC a l etape {0}/5 : {1}" -f $script:Etape, $Quoi) -ForegroundColor Red
    if ($Pourquoi) {
        foreach ($l in ($Pourquoi -split "`n")) { Write-Host "   $l" -ForegroundColor Red }
    }
    Write-Host ''
    Write-Host '   Copiez tout ce qui est affiche ci-dessus et envoyez-le.' -ForegroundColor Red
    exit 1
}


# -- Appel d un binaire externe ----------------------------------------------

function Invoquer-Externe {
    <#
    Lance un binaire et rend sa sortie ET son code, sans qu une ligne de stderr
    puisse tuer le script.

    Cf. le piege 2 de l en-tete. L affectation ci-dessous cree une variable de
    PORTEE DE FONCTION : la preference globale 'Stop' est retablie au retour,
    sans sauvegarde ni finally. C est ce qui permet de relacher la regle le temps
    d un appel sans la relacher pour le script.

    Rend un objet plutot que la seule sortie, pour que l appelant n ait aucune
    raison de lire $LASTEXITCODE lui-meme :
        .Code    le code de sortie, LE SEUL juge de l echec
        .Texte   sortie fusionnee, une chaine
        .Lignes  la meme, en tableau
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

function Montrer {
    param($Resultat, [int]$Max = 12)
    $lignes = @($Resultat.Lignes)
    if ($lignes.Count -gt $Max) {
        $lignes = @($lignes | Select-Object -Last $Max)
        Info '   (...)'
    }
    foreach ($l in $lignes) { if ("$l".Trim()) { Info "   $l" } }
}


# -- 1. Code a jour ----------------------------------------------------------

function Appliquer-Archive {
    <#
    Applique une archive GitHub SUR la racine, sans jamais s imbriquer dedans.

    LE PIEGE QUE CETTE FONCTION EXISTE POUR EVITER, et il a ete vecu :
    `Expand-Archive -DestinationPath .` lance DEPUIS le dossier epure\ ne
    remplace pas son contenu -- il y cree un sous-dossier `epure-main\`. Les
    etapes suivantes tournent alors sur l ANCIEN code, et rien ne le signale :
    npm install reussit, faire_paquet.py reussit, l installation reussit, et le
    destinataire recoit exactement le paquet qu il avait deja. Une panne
    silencieuse qui ne se voit qu au comportement de l application.

    D ou la forme retenue, qui rend le piege structurellement impossible :

      1. extraire dans un dossier TEMPORAIRE, jamais dans la racine ;
      2. y trouver l unique dossier de premier niveau (`epure-main`, mais le nom
         depend de la branche -- on ne le code pas en dur) ;
      3. verifier qu il ressemble bien au depot (backend\ et tools\ presents),
         parce qu une archive tronquee ou une page d erreur HTML renommee .zip
         passerait sinon a l etape suivante ;
      4. copier SON CONTENU sur la racine, entree par entree.

    ON ECRASE, ON NE SUPPRIME PAS -- meme regle que `Deployer` dans
    installer-epure.ps1. Les fichiers retires en amont depuis la derniere mise a
    jour survivent donc, ce qui est le prix a payer pour ne pas risquer les
    donnees. `$HORS_MISE_A_JOUR` protege en plus ce que le depot ne contient pas
    et qui ne doit jamais etre touche.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Zip,
        [Parameter(Mandatory = $true)][string]$Racine
    )
    $temporaire = Join-Path ([System.IO.Path]::GetTempPath()) ('epure-maj-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $temporaire -Force | Out-Null
    try {
        Expand-Archive -LiteralPath $Zip -DestinationPath $temporaire -Force

        $sommets = @(Get-ChildItem -LiteralPath $temporaire -Directory)
        if ($sommets.Count -ne 1) {
            throw ("l archive ne contient pas un dossier unique a sa racine (" +
                   $sommets.Count + ") -- ce n est pas une archive GitHub du depot")
        }
        $source = $sommets[0].FullName
        foreach ($attendu in @('backend', 'tools')) {
            if (-not (Test-Path -LiteralPath (Join-Path $source $attendu))) {
                throw "l archive ne contient pas $attendu\ -- telechargement incomplet ?"
            }
        }

        $copies = 0
        foreach ($entree in Get-ChildItem -LiteralPath $source -Force) {
            if ($HORS_MISE_A_JOUR -contains $entree.Name) {
                Note ("{0} conserve (donnees locales)" -f $entree.Name)
                continue
            }
            if ($entree.PSIsContainer) {
                # -Destination LA RACINE, et surtout pas (Join-Path $Racine $nom) :
                # avec la cible, Copy-Item copie DANS le dossier existant et cree
                # `backend\backend`. Avec la racine, il fusionne -- verifie sous
                # 5.1 et sous 7 : les fichiers du depot arrivent a cote de ceux
                # qui etaient la, et rien n est supprime. C est le meme piege
                # d imbrication que celui du docstring, une couche plus bas.
                Copy-Item -LiteralPath $entree.FullName -Destination $Racine -Recurse -Force
            } else {
                Copy-Item -LiteralPath $entree.FullName -Destination (Join-Path $Racine $entree.Name) -Force
            }
            $copies++
        }
        return $copies
    } finally {
        Remove-Item -LiteralPath $temporaire -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Mettre-A-Jour-Code {
    Titre 'Code a jour'
    if ($SansCode) { Note 'saute (-SansCode)' ; return }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git -and (Test-Path -LiteralPath (Join-Path $RACINE '.git'))) {
        Info 'tentative par git pull'
        Push-Location $RACINE
        try {
            $r = Invoquer-Externe git pull --ff-only
        } finally { Pop-Location }
        if ($r.Code -eq 0) {
            Montrer $r 4
            Ok 'code a jour (git)'
            return
        }
        # Pas un abandon : c est le cas NORMAL sur cette machine, ou Smart App
        # Control bloque git-remote-https.exe et libcurl-4.dll. On le dit et on
        # passe a l archive.
        Alerte 'git pull a echoue -- on passe par l archive'
        Montrer $r 6
    } else {
        Note 'pas de depot git utilisable ici -- on passe par l archive'
    }

    $zip = Join-Path ([System.IO.Path]::GetTempPath()) 'epure-main.zip'
    Info "telechargement de $ARCHIVE_URL"
    try {
        # Invoke-WebRequest et non curl.exe : c est une cmdlet, donc aucun
        # argument natif a echapper, et son echec est une exception qu on attrape
        # ici plutot qu un code de sortie a interpreter.
        $ancienProgres = $ProgressPreference
        $ProgressPreference = 'SilentlyContinue'   # sinon la barre ralentit tout
        try {
            Invoke-WebRequest -Uri $ARCHIVE_URL -OutFile $zip -UseBasicParsing
        } finally { $ProgressPreference = $ancienProgres }
    } catch {
        Abandonner 'telechargement du code impossible' ("$($_.Exception.Message)`nVerifiez la connexion reseau, puis relancez.")
    }
    $taille = (Get-Item -LiteralPath $zip).Length
    Info ("archive recue : {0:N1} Mo" -f ($taille / 1MB))

    try {
        $n = Appliquer-Archive -Zip $zip -Racine $RACINE
    } catch {
        Abandonner 'application de l archive impossible' "$($_.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    }
    Ok "code a jour (archive, $n entrees)"
}


# -- 2. Dependances frontend -------------------------------------------------

function Installer-Frontend {
    Titre 'Dependances frontend'
    if (-not (Test-Path -LiteralPath $FRONTEND)) {
        Abandonner 'dossier frontend introuvable' "attendu : $FRONTEND"
    }
    # `npm.cmd` et pas `npm` : sur cette machine, `npm` seul se resout au script
    # PowerShell `npm.ps1`, que la politique d execution refuse. Le .cmd n est
    # pas soumis a cette politique.
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        Abandonner 'npm.cmd introuvable' "Installez Node.js, puis relancez.`nhttps://nodejs.org"
    }
    Push-Location $FRONTEND
    try {
        Info 'npm.cmd install (plusieurs minutes la premiere fois)'
        $r = Invoquer-Externe $npm.Source install
    } finally { Pop-Location }
    if ($r.Code -ne 0) {
        Montrer $r 20
        Abandonner 'npm install a echoue' 'La sortie est au-dessus.'
    }
    Montrer $r 4
    Ok 'dependances frontend installees'
}


# -- 3. Construction du paquet -----------------------------------------------

function Construire-Paquet {
    Titre 'Construction du paquet'
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Abandonner 'python introuvable' "Installez Python 3.12+, puis relancez.`nhttps://www.python.org/downloads/"
    }
    $script_paquet = Join-Path $RACINE 'tools\faire_paquet.py'
    if (-not (Test-Path -LiteralPath $script_paquet)) {
        Abandonner 'tools\faire_paquet.py introuvable' "L etape 1 a-t-elle vraiment mis le code a jour ?"
    }
    # Un FICHIER .py, jamais `python -c` : cf. le piege 3 de l en-tete.
    Info "python tools\faire_paquet.py --destinataire $Destinataire --arch $Arch --modules $Modules"
    Push-Location $RACINE
    try {
        $r = Invoquer-Externe $python.Source $script_paquet `
                '--destinataire' $Destinataire '--arch' $Arch '--modules' $Modules `
                '--sans-contraintes'
    } finally { Pop-Location }
    Montrer $r 20
    if ($r.Code -ne 0) {
        Abandonner 'la construction du paquet a echoue' 'La sortie est au-dessus.'
    }
    $installeur = Join-Path $RACINE 'dist-paquets\Installer-Epure.cmd'
    if (-not (Test-Path -LiteralPath $installeur)) {
        Abandonner 'paquet construit mais Installer-Epure.cmd absent' `
                   "attendu : $installeur"
    }
    Ok 'paquet construit'
}


# -- 4. Arret de l instance en cours ------------------------------------------

function Arreter-Instance {
    <#
    AVANT l installation, jamais apres.

    L installeur ecrase les fichiers du runtime Python. Si l ancienne instance
    tourne encore, elle tient `_asyncio.pyd` et l ecriture echoue -- une panne
    deja rencontree, et dont le message ne dit pas qu il suffisait de fermer
    Epure.

    Filtre sur le CHEMIN et pas sur le nom : `python.exe` est un nom trop commun
    pour qu on tue tout ce qui le porte sur la machine de quelqu un.
    #>
    Titre 'Arret de l instance en cours'
    $vises = @(Get-Process pythonw, python -ErrorAction SilentlyContinue |
               Where-Object { $_.Path -like '*Epure*' })
    if ($vises.Count -eq 0) { Note 'aucune instance en cours' ; return }
    foreach ($p in $vises) {
        Info ("PID {0} : {1}" -f $p.Id, $p.Path)
        try {
            Stop-Process -Id $p.Id -Force -ErrorAction Stop
        } catch {
            Alerte ("PID {0} n a pas pu etre arrete : {1}" -f $p.Id, $_.Exception.Message)
        }
    }
    Start-Sleep -Milliseconds 800
    $restants = @(Get-Process pythonw, python -ErrorAction SilentlyContinue |
                  Where-Object { $_.Path -like '*Epure*' })
    if ($restants.Count -gt 0) {
        Abandonner 'une instance d Epure tourne encore' `
                   ("Fermez-la a la main (Gestionnaire des taches, pythonw.exe), puis relancez.`n" +
                    "Sinon l installation echouera sur un fichier verrouille.")
    }
    Ok ("instance arretee ({0} process)" -f $vises.Count)
}


# -- 5. Installation ---------------------------------------------------------

function Installer-Paquet {
    Titre 'Installation'
    if ($SansInstall) { Note 'saute (-SansInstall)' ; return }
    $installeur = Join-Path $RACINE 'dist-paquets\Installer-Epure.cmd'
    Info $installeur
    $r = Invoquer-Externe $installeur
    Montrer $r 30
    if ($r.Code -ne 0) {
        Abandonner 'l installation a echoue' 'La sortie de l installeur est au-dessus.'
    }
    Ok 'installation terminee'
}


# -- Deroule -----------------------------------------------------------------

Write-Host ''
Write-Host 'Epure -- mise a jour complete' -ForegroundColor White
Write-Host "  depot        : $RACINE" -ForegroundColor DarkGray
Write-Host "  destinataire : $Destinataire" -ForegroundColor DarkGray
Write-Host "  architecture : $Arch" -ForegroundColor DarkGray
Write-Host "  modules      : $Modules" -ForegroundColor DarkGray

Mettre-A-Jour-Code
Installer-Frontend
Construire-Paquet
Arreter-Instance
Installer-Paquet

Write-Host ''
Write-Host 'Termine. Epure est a jour.' -ForegroundColor Green
