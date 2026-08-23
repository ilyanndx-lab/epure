@echo off
rem ===========================================================================
rem  Epure -- installation. Double-cliquez ce fichier.
rem
rem  Ce fichier n a qu un role : rendre installer-epure.ps1 lancable par un
rem  double-clic. Il existe parce que Windows refuse les deux, et pas pour la
rem  meme raison :
rem
rem   - un .ps1 double-clique s OUVRE dans le Bloc-notes, il ne s execute pas ;
rem   - l ExecutionPolicy par defaut (RemoteSigned pour l utilisateur) REFUSE un
rem     script non signe telecharge depuis internet, ce que sera celui-ci.
rem
rem  -ExecutionPolicy Bypass ne change rien sur la machine : l option ne vaut
rem  que pour ce processus PowerShell. -NoProfile evite qu un profil utilisateur
rem  exotique modifie le comportement du script.
rem
rem  %~dp0 : dossier de CE fichier, antislash final compris. Le .ps1 est cherche
rem  a cote, jamais par un chemin absolu (CLAUDE.md section 10). %* transmet les
rem  arguments, pour pouvoir passer -Cible ou -Archive sans ouvrir de terminal.
rem
rem  ASCII pur, comme le .ps1 : cmd.exe lit ce fichier dans la page de codes OEM
rem  et pas en UTF-8.
rem
rem  pause a la fin : sans lui la fenetre se ferme des que le script rend la
rem  main, et le resume de l installation -- ou le message d erreur -- n est pas
rem  lisible.
rem ===========================================================================

if not exist "%~dp0installer-epure.ps1" (
    echo.
    echo   installer-epure.ps1 est introuvable a cote de ce fichier.
    echo.
    echo   Les trois fichiers recus doivent rester ensemble dans le meme
    echo   dossier : epure-^<nom^>.zip, installer-epure.ps1 et ce fichier.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer-epure.ps1" %*

echo.
pause
