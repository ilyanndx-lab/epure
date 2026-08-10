@echo off
rem ===========================================================================
rem  Epure - lanceur double-cliquable. Aucun terminal a ouvrir.
rem
rem  Contenu volontairement en ASCII pur : cmd.exe lit un .bat dans la page de
rem  codes OEM (850/437), pas en UTF-8. Un accent dans un echo sortirait en
rem  mojibake, et un accent dans une commande casserait la commande.
rem
rem  pythonw et non python : pythonw.exe est un binaire du sous-systeme GUI, il
rem  n'a pas de console attachee. Avec python.exe, une fenetre noire resterait
rem  ouverte tant que le tray tourne, c'est-a-dire toute la session.
rem
rem  start "" : sans lui, cmd.exe attend la fin du programme qu'il lance, meme
rem  un programme GUI - la console de ce .bat resterait donc ouverte elle aussi,
rem  ce qui annulerait tout l'interet de pythonw. Avec start, cmd rend la main
rem  immediatement et sa fenetre se ferme. Le "" est le TITRE de fenetre que
rem  start reclame des que son premier argument est entre guillemets : sans lui,
rem  start prendrait le chemin de pythonw pour un titre et ne lancerait rien.
rem
rem  %~dp0 : dossier de CE fichier, antislash final compris. Le lanceur reste
rem  donc correct quel que soit le repertoire courant - raccourci sur le Bureau,
rem  menu Demarrer, barre des taches - sans chemin absolu en dur (CLAUDE.md
rem  section 10). C'est precisement ce que start.ps1 ne savait pas faire.
rem
rem  OU REGARDER QUAND RIEN N'APPARAIT : epure_tray.log, a cote de ce fichier.
rem  _log() ecrit dans un vrai fichier et ne depend pas d'une console ; sous
rem  pythonw il n'y a plus de stderr, donc c'est la SEULE trace d'un demarrage
rem  qui echoue.
rem ===========================================================================

where pythonw >nul 2>&1
if errorlevel 1 (
    echo.
    echo   pythonw.exe est introuvable sur le PATH.
    echo.
    echo   Installez Python 3.12 ou plus recent depuis
    echo   https://www.python.org/downloads/ en cochant
    echo   "Add python.exe to PATH", puis relancez ce fichier.
    echo.
    pause
    exit /b 1
)

start "" pythonw "%~dp0epure_tray.py"
