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

echo Regeneration realiste des habitants fictifs...
call %PYTHON_CMD% outils\regenerate_realistic_residents.py --force --population 14068
if errorlevel 1 (
    echo Echec de la regeneration des habitants.
    pause
    exit /b 1
)

echo Regeneration terminee.
pause
exit /b 0
