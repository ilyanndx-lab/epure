@echo off
rem  Epure -- mise a jour complete. Double-cliquez ce fichier, c est tout.
rem
rem  Ce .cmd ne fait rien lui-meme : il lance mettre-a-jour-epure.ps1, qui
rem  enchaine les cinq etapes (code a jour, dependances, paquet, arret de
rem  l instance, installation). La logique est dans le .ps1 parce qu elle est
rem  testable la-bas et illisible ici -- meme partage que Installer-Epure.cmd.
rem
rem  `powershell` et non `pwsh` : c est l interpreteur present sur toute machine
rem  Windows, sans rien installer. Il a trois defauts que le .ps1 contourne
rem  explicitement (cf. son en-tete .NOTES) ; en changer serait supposer une
rem  installation qui n existe pas forcement chez le destinataire.
rem
rem  `-ExecutionPolicy Bypass` : la politique par defaut refuse d executer un
rem  .ps1 telecharge. Bypass ne vaut que pour CE lancement, rien n est modifie
rem  sur la machine.
rem
rem  `pause` a la fin, dans les deux cas : un .cmd double-clique ferme sa console
rem  des qu il rend la main. Sans cette ligne, le destinataire ne pourrait ni
rem  lire le resultat ni le copier en cas de probleme -- c est-a-dire perdre
rem  exactement ce que ce script existe pour lui donner.

if not exist "%~dp0mettre-a-jour-epure.ps1" (
    echo.
    echo   mettre-a-jour-epure.ps1 est introuvable a cote de ce fichier.
    echo.
    echo   Les deux fichiers doivent rester ensemble dans le dossier tools\
    echo   du depot Epure.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0mettre-a-jour-epure.ps1" %*
set CODE=%ERRORLEVEL%

echo.
if not "%CODE%"=="0" (
    echo   La mise a jour s est arretee. Le message ci-dessus dit a quelle etape.
) else (
    echo   Vous pouvez fermer cette fenetre.
)
pause
exit /b %CODE%
