@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_CMD="
where python >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"

if not defined PYTHON_CMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
    echo Python est introuvable.
    echo Installez Python puis relancez ce fichier.
    pause
    exit /b 1
)

echo Prechargement du cache disque des tuiles satellite IGN du territoire de Douarnenez Communaute...
echo Cette operation peut prendre plusieurs minutes et utiliser plusieurs centaines de Mo.
call %PYTHON_CMD% outils\prefetch_ortho_tiles.py --min-zoom 13 --max-zoom 19 --refresh-stale --workers 8
if errorlevel 1 (
    echo Echec de la mise a jour du cache satellite.
    pause
    exit /b 1
)

echo Cache satellite local mis a jour.
pause
exit /b 0
