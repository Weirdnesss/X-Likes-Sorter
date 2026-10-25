@echo off
title X Likes ^& Bookmarks Sync ^> Hydrus
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [!] venv not found. Run setup.bat first.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat

if not exist "config\config.json" (
    echo [!] config\config.json not found. Run setup.bat first.
    pause
    exit /b 1
)

:: ---- Read settings from config\config.json (each default matches char_sorter.py) ----
set "PARENT_DIR="
for /f "usebackq delims=" %%a in (`python -c "import json,os; c=json.load(open('config/config.json',encoding='utf-8-sig')); print(os.path.abspath(os.path.expanduser(c.get('parent_dir','X_Likes'))))" 2^>nul`) do set "PARENT_DIR=%%a"
if "%PARENT_DIR%"=="" (
    echo [!] Could not read config\config.json - check it is valid JSON ^(use \\ in Windows paths^).
    pause
    exit /b 1
)
call :getcfg TWITTER_USER twitter_username ""
call :getcfg COOKIES cookies_file cookies.txt
call :getcfg FETCH_LIKES fetch_likes 1
call :getcfg FETCH_BOOKMARKS fetch_bookmarks 1
call :getcfg GDL_RETRIES gallery_dl_retries 4
call :getcfg GDL_SLEEP gallery_dl_sleep 0
call :getcfg GDL_ABORT gallery_dl_abort 10
call :getcfg GDL_USER_ARGS gallery_dl_extra_args ""

set "GDL_EXTRA=--retries %GDL_RETRIES%"
if not "%GDL_SLEEP%"=="0" set "GDL_EXTRA=%GDL_EXTRA% --sleep %GDL_SLEEP%"
if not "%GDL_ABORT%"=="0" set "GDL_EXTRA=%GDL_EXTRA% --abort %GDL_ABORT%"
if defined GDL_USER_ARGS set "GDL_EXTRA=%GDL_EXTRA% %GDL_USER_ARGS%"
set "INBOX_DIR=%PARENT_DIR%\Incoming\Inbox"

:: ---- Command-line mode: run_update.bat sync | push | check   or   run_update.bat <char_sorter command> [options] ----
if not "%~1"=="" goto cli

:menu
cls
echo =========================================
echo   X Likes ^& Bookmarks  -^>  Tag  -^>  Hydrus
echo =========================================
echo.
echo Active Account: @%TWITTER_USER%
echo Library:        %PARENT_DIR%
echo.
echo  NEW LIKES
echo   [1] Sync (Fetch Likes + Bookmarks + Tag + Push To Hydrus)
echo   [2] Tag + Push What Is Already In The Inbox
echo   [3] Push Preview (dry run, Hydrus not contacted)
echo.
echo  HYDRUS
echo   [4] Check Hydrus Connection (key, permissions, tag service)
echo   [5] Re-push Everything That Changed (adds tags only)
echo   [6] Re-push And Remove Stale Tags (needs 'Search for and Fetch Files')
echo.
echo   [7] Export Costume Tag and Series Tag Parents (for Hydrus tag parents)
echo.
echo  TOOLS
echo   [8] Benchmark GPU / Batch Size
echo.
echo   [0] Exit
echo.
set "choice="
set /p choice="Select an option: "

if "%choice%"=="1" call :do_sync & goto finish
if "%choice%"=="2" call :do_process & goto finish
if "%choice%"=="3" python push_to_hydrus.py --dry-run --folder "%INBOX_DIR%" & goto finish
if "%choice%"=="4" python push_to_hydrus.py --check & goto finish
if "%choice%"=="5" python push_to_hydrus.py & goto finish
if "%choice%"=="6" python push_to_hydrus.py --resync & goto finish
if "%choice%"=="7" python export_parents.py & python export_series_parents.py & goto finish
if "%choice%"=="8" python char_sorter.py bench & goto finish
if "%choice%"=="0" exit /b
goto menu

:finish
echo.
echo =========================================
echo Done. Press any key to return to the menu.
echo =========================================
pause >nul
goto menu

:cli
if /i "%~1"=="sync" goto cli_sync
if /i "%~1"=="push" goto cli_push
if /i "%~1"=="check" goto cli_check
if /i "%~1"=="resync" goto cli_resync
if /i "%~1"=="parents" goto cli_parents
python char_sorter.py %*
exit /b %errorlevel%

:cli_sync
call :do_sync
exit /b %errorlevel%

:cli_push
call :do_process
exit /b %errorlevel%

:cli_check
python push_to_hydrus.py --check
exit /b %errorlevel%

:cli_parents
python export_parents.py
exit /b %errorlevel%

:cli_resync
python push_to_hydrus.py --resync
exit /b %errorlevel%

:: ---- Subroutine: fetch likes and bookmarks, then tag and push the Inbox ----
:do_sync
if not exist "%COOKIES%" (
    echo [!] ERROR: %COOKIES% was not found.
    echo Save your exported cookies there, or set "cookies_file" in config\config.json.
    exit /b 1
)
if "%TWITTER_USER%"=="" goto no_user
if /i "%TWITTER_USER%"=="YOUR_USERNAME_HERE" goto no_user

:: Fail fast if Hydrus is not open, before spending time downloading and tagging.
python push_to_hydrus.py --check
if errorlevel 1 (
    echo [!] Hydrus is not reachable. Open Hydrus, then run this again.
    exit /b 1
)

if not "%FETCH_LIKES%"=="1" goto skip_likes
echo.
echo [1/3] Fetching X Likes for @%TWITTER_USER%...
gallery-dl %GDL_EXTRA% --cookies "%COOKIES%" --download-archive data\archive.sqlite3 -o archive-event=file,skip --write-metadata "https://x.com/%TWITTER_USER%/likes" -d "%INBOX_DIR%"
:skip_likes

if not "%FETCH_BOOKMARKS%"=="1" goto skip_bookmarks
echo.
echo [2/3] Fetching X Bookmarks...
gallery-dl %GDL_EXTRA% --cookies "%COOKIES%" --download-archive data\archive.sqlite3 -o archive-event=file,skip --write-metadata "https://x.com/i/bookmarks" -d "%INBOX_DIR%"
:skip_bookmarks

echo.
echo [3/3] Tagging and pushing to Hydrus...
call :do_process
exit /b %errorlevel%

:: ---- Subroutine: tag new Inbox images (no moving), then push them to Hydrus ----
:do_process
python char_sorter.py intake
if errorlevel 1 exit /b 1
python push_to_hydrus.py --folder "%INBOX_DIR%"
exit /b %errorlevel%

:no_user
echo [!] ERROR: set "twitter_username" in config\config.json first.
exit /b 1

:: ---- Subroutine: read one config value. Usage: call :getcfg VARIABLE key default ----
:getcfg
set "%~1=%~3"
for /f "usebackq delims=" %%a in (`python -c "import json,sys; v=json.load(open('config/config.json',encoding='utf-8-sig')).get(sys.argv[1],sys.argv[2]); print(int(v) if isinstance(v,bool) else v)" "%~2" "%~3" 2^>nul`) do set "%~1=%%a"
exit /b