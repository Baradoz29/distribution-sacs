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

if not exist "data" mkdir "data"

if not exist "data\waste_tracking.db" (
    echo Initialisation de la base de donnees...
    call %PYTHON_CMD% scripts\init_db.py
    if errorlevel 1 (
        echo Echec de l'initialisation de la base.
        pause
        exit /b 1
    )
)

echo Demarrage du serveur local...
start "Suivi des sacs - serveur" cmd /k "%PYTHON_CMD% app.py"

echo Attente de la verification des caches et du demarrage complet du serveur...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$deadline=(Get-Date).AddMinutes(30);" ^
  "while((Get-Date) -lt $deadline){" ^
  "  try {" ^
  "    $response=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8000/api/overview' -TimeoutSec 2;" ^
  "    if($response.StatusCode -eq 200){ exit 0 }" ^
  "  } catch {}" ^
  "  Start-Sleep -Seconds 2" ^
  "}" ^
  "exit 1"
if errorlevel 1 (
    echo Le serveur a mis trop de temps a devenir disponible.
    echo La fenetre du serveur contient les details du prechargement.
    pause
    exit /b 1
)

echo Ouverture de l'application dans le navigateur...
start "" "http://127.0.0.1:8000"

exit /b 0
