@echo off
title X Likes ^& Bookmarks Sync ^& Sorter
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

:: ---- Command-line mode: run_update.bat sync   or   run_update.bat <char_sorter command> [options] ----
if not "%~1"=="" goto cli

:menu
cls
echo =========================================
echo   X Likes ^& Bookmarks Sync ^& Sorter
echo =========================================
echo.
echo Active Account: @%TWITTER_USER%
echo Library:        %PARENT_DIR%
echo.
echo  SYNC ^& SORT NEW FILES
echo   [1] Sync (Fetch Likes + Bookmarks + Tag + Sort)
echo   [2] Process Inbox Only (files already in Inbox)
echo   [3] Inbox Dry-Run Preview (no files moved)
echo.
echo  WHOLE LIBRARY
echo   [4] Full Re-sort Preview (dry run)
echo   [5] Full Re-sort (moves files)
echo   [6] Undo Moves From The Sort Log
echo.
echo  HASHTAGS
echo   [7] Top Unmapped Hashtags
echo   [8] Suggest Hashtag Rules (preview)
echo   [9] Suggest Hashtag Rules (add to hashtag_map.csv)
echo   [10] Rebuild Hashtag Data (from saved sidecars)
echo   [11] Report On Unsorted Images (run option 4 first)
echo.
echo  TOOLS
echo   [12] Benchmark GPU / Batch Size
echo.
echo   [0] Exit
echo.
set "choice="
set /p choice="Select an option: "

if "%choice%"=="1" call :do_sync & goto finish
if "%choice%"=="2" python char_sorter.py inbox & goto finish
if "%choice%"=="3" python char_sorter.py inbox --dry-run & goto finish
if "%choice%"=="4" python char_sorter.py sort --dry-run & goto finish
if "%choice%"=="5" goto resort
if "%choice%"=="6" goto undo
if "%choice%"=="7" python char_sorter.py tags --top 50 & goto finish
if "%choice%"=="8" python char_sorter.py suggest-map & goto finish
if "%choice%"=="9" goto suggest_apply
if "%choice%"=="10" python char_sorter.py rebuild-tags & goto finish
if "%choice%"=="11" python char_sorter.py report --top 40 & goto finish
if "%choice%"=="12" python char_sorter.py bench & goto finish
if "%choice%"=="0" exit /b
goto menu

:resort
echo.
echo This moves files inside %PARENT_DIR%\Sorted using your current rules.
echo Tip: run option 4 first to preview. Moves are logged, so option 6 can undo them.
set "ok="
set /p ok="Continue? (y/n): "
if /i not "%ok%"=="y" goto finish
python char_sorter.py sort
goto finish

:undo
echo.
echo This reverses EVERY move recorded in data\sort_log.csv, not just the last run.
set "ok="
set /p ok="Continue? (y/n): "
if /i not "%ok%"=="y" goto finish
python char_sorter.py undo
goto finish

:suggest_apply
echo.
echo This appends every suggested rule to config\hashtag_map.csv. Option 8 shows them first.
set "ok="
set /p ok="Continue? (y/n): "
if /i not "%ok%"=="y" goto finish
python char_sorter.py suggest-map --apply
goto finish

:finish
echo.
echo =========================================
echo Done. Press any key to return to the menu.
echo =========================================
pause >nul
goto menu

:cli
if /i "%~1"=="sync" goto cli_sync
python char_sorter.py %*
exit /b %errorlevel%

:cli_sync
call :do_sync
exit /b %errorlevel%

:: ---- Subroutine: fetch likes and bookmarks, then tag and sort the Inbox ----
:do_sync
if not exist "%COOKIES%" (
    echo [!] ERROR: %COOKIES% was not found.
    echo Save your exported cookies there, or set "cookies_file" in config\config.json.
    exit /b 1
)
if "%TWITTER_USER%"=="" goto no_user
if /i "%TWITTER_USER%"=="YOUR_USERNAME_HERE" goto no_user

if not "%FETCH_LIKES%"=="1" goto skip_likes
echo [1/2] Fetching X Likes for @%TWITTER_USER%...
gallery-dl %GDL_EXTRA% --cookies "%COOKIES%" --download-archive data\archive.sqlite3 -o archive-event=file,skip --write-metadata "https://x.com/%TWITTER_USER%/likes" -d "%INBOX_DIR%"
:skip_likes

if not "%FETCH_BOOKMARKS%"=="1" goto skip_bookmarks
echo.
echo [2/2] Fetching X Bookmarks...
gallery-dl %GDL_EXTRA% --cookies "%COOKIES%" --download-archive data\archive.sqlite3 -o archive-event=file,skip --write-metadata "https://x.com/i/bookmarks" -d "%INBOX_DIR%"
:skip_bookmarks

echo.
echo Processing all downloaded files...
python char_sorter.py inbox
exit /b

:no_user
echo [!] ERROR: set "twitter_username" in config\config.json first.
exit /b 1

:: ---- Subroutine: read one config value. Usage: call :getcfg VARIABLE key default ----
:getcfg
set "%~1=%~3"
for /f "usebackq delims=" %%a in (`python -c "import json,sys; v=json.load(open('config/config.json',encoding='utf-8-sig')).get(sys.argv[1],sys.argv[2]); print(int(v) if isinstance(v,bool) else v)" "%~2" "%~3" 2^>nul`) do set "%~1=%%a"
exit /b
