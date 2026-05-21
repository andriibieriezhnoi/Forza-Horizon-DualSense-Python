@echo off
REM FH DualSense - Windows launcher (zuv).
REM Bundle lives in app/. Auto-downloads from GitHub Releases if missing.
REM Set PRERELEASE=true to track rolling test builds (v999.0.0 tag).
setlocal EnableDelayedExpansion

set "PRERELEASE=false"

set "DIR=%~dp0"
set "APP=%DIR%app"
set "BUNDLE=%APP%\fhds.zuv.py"
REM Source repo for the bundle. Override with the FHDS_REPO env var if needed.
if not defined FHDS_REPO set "FHDS_REPO=andriibieriezhnoi/Forza-Horizon-DualSense-Python"
set "REPO=%FHDS_REPO%"

if /i "%PRERELEASE%"=="true" (
    set "URL=https://github.com/%REPO%/releases/download/v999.0.0/fhds.zuv.py"
    set "FLAGS=--prerelease"
) else (
    set "URL=https://github.com/%REPO%/releases/latest/download/fhds.zuv.py"
    set "FLAGS="
)

REM Args starting with -- forward to bundle; rest = Steam wrapper game cmd.
set "GAME="
:argloop
if "%~1"=="" goto ready
set "a=%~1"

if "!a:~0,2!"=="--" goto flag_arg
if not defined GAME (set "GAME=%1") else set "GAME=!GAME! %1"
goto next_arg
:flag_arg
set "FLAGS=!FLAGS! %1"
:next_arg

shift
goto argloop

:ready
if not exist "%APP%" mkdir "%APP%"

REM Always grab the latest release. Replace atomically; keep the cached copy if offline.
echo Fetching latest fhds.zuv.py from %REPO%...
curl.exe -L --fail -o "%BUNDLE%.new" "%URL%" && move /y "%BUNDLE%.new" "%BUNDLE%" >nul
del /q "%BUNDLE%.new" 2>nul
if not exist "%BUNDLE%" (
    echo Download failed and no cached bundle. Get it from https://github.com/%REPO%/releases
    pause & exit /b 1
)

where uv >nul 2>nul
if errorlevel 1 (
    echo Installing uv...
    powershell -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    where uv >nul 2>nul || (echo uv not on PATH - restart terminal. & pause & exit /b 1)
)

REM Optional Steam wrapper: pass game cmd (e.g. start "" steam://rungameid/1551360)
if defined GAME start "" %GAME%

REM Don't let host Python env leak into the bundled venv.
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"

uv run "%BUNDLE%" %FLAGS%
endlocal
exit /b %ERRORLEVEL%
