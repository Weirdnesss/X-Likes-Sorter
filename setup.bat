@echo off
title X Likes Sorter Setup
cd /d "%~dp0"

echo =========================================
echo     X Likes Sorter - Environment Setup
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
echo [3/4] Creating Project Folders ^& Templates...
if not exist "config" mkdir config
if not exist "data" mkdir data
if not exist "model" mkdir model

if not exist "config\hashtag_map.csv" (
    echo hashtag,folder> config\hashtag_map.csv
    echo Created config\hashtag_map.csv template.
)

if not exist "config\config.json" (
    if exist "config\config.example.json" (
        copy "config\config.example.json" "config\config.json" >nul
        echo Created config\config.json from config.example.json.
    ) else (
        echo [!] config\config.example.json is missing. Re-download the project.
    )
)

echo.
echo [4/4] Done.
echo ====================================================
echo Setup complete!
echo 1. Save your Twitter cookies as "cookies.txt" in this folder.
echo 2. Edit "config\config.json": set your username and parent_dir
echo    (parent_dir can be a full path like D:\X_Likes, or a name
echo    like X_Likes to keep everything next to this folder).
echo 3. Double-click "run_update.bat" to start syncing.
echo ====================================================
pause
