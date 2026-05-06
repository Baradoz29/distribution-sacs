@echo off
setlocal

cd /d "%~dp0\.."

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

echo Mise a jour du cache local de la carte...
call %PYTHON_CMD% outils\refresh_building_cache.py
if errorlevel 1 (
    echo Echec de la mise a jour du cache local.
    pause
    exit /b 1
)

echo Cache local de la carte mis a jour.
pause
exit /b 0
