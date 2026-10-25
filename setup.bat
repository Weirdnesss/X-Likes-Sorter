@echo off
title X Likes To Hydrus Setup
cd /d "%~dp0"

echo =========================================
echo     X Likes To Hydrus - Environment Setup
echo =========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python was not found on PATH. Install Python 3.10+ and tick "Add to PATH".
    pause
    exit /b 1
)

echo [1/4] Creating Python Virtual Environment (venv)...
if not exist "venv\Scripts\activate.bat" (
    python -m venv venv
    echo Virtual environment created.
) else (
    echo Virtual environment already exists. Skipping...
)

echo.
echo [2/4] Installing Required Dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [!] Dependency install failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo [3/4] Creating Project Folders ^& Config Files...
if not exist "config" mkdir config
if not exist "data" mkdir data
if not exist "model" mkdir model

if not exist "config\config.json" (
    if exist "config\config.example.json" (
        copy "config\config.example.json" "config\config.json" >nul
        echo Created config\config.json from config.example.json.
    ) else (
        echo [!] config\config.example.json is missing. Re-download the project.
    )
)

if not exist "config\hydrus.json" (
    if exist "config\hydrus.example.json" (
        copy "config\hydrus.example.json" "config\hydrus.json" >nul
        echo Created config\hydrus.json from hydrus.example.json.
    ) else (
        echo [!] config\hydrus.example.json is missing. Re-download the project.
    )
)

echo.
echo [4/4] Done.
echo ====================================================
echo Setup complete!
echo 1. Save your Twitter cookies as "cookies.txt" in this folder.
echo 2. Edit "config\config.json": set twitter_username, parent_dir
echo    (a full path like D:\X_Likes, or a name like X_Likes to keep
echo    everything next to this folder) and "tagger_backend": "camie".
echo 3. In Hydrus: enable the Client API, create a local tag service
echo    called "camie", and add an access key (see README.md).
echo    Paste the key into "config\hydrus.json" as access_key.
echo 4. Open Hydrus, then double-click "run_update.bat".
echo    Option 4 tests the Hydrus connection first.
echo ====================================================
pause