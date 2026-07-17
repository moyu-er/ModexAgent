@echo off
setlocal enabledelayedexpansion
pushd "%~dp0"

:: ============================================================================
::  ModexBot Installer Builder (fully self-contained)
:: ============================================================================
::  Produces: ModexBot-Setup-<version>.exe
::
::  Prerequisites on the BUILD machine:
::    - Python 3.10+ (to run build scripts)
::    - Node.js + npm (for frontend build)
::    - Inno Setup 7 (ISCC.exe)
::    - git + uv on PATH
::
::  Usage:
::    build.bat              — full build
::    build.bat --skip-fe    — skip frontend rebuild
::    build.bat --skip-electron  — skip Electron packaging (browser-only)
:: ============================================================================

set "STAGING=%~dp0staging"
set "SKIP_FE=0"
set "SKIP_ELECTRON=0"

if /i "%~1"=="--skip-fe" set "SKIP_FE=1"
if /i "%~1"=="--skip-electron" set "SKIP_ELECTRON=1"
if /i "%~2"=="--skip-fe" set "SKIP_FE=1"
if /i "%~2"=="--skip-electron" set "SKIP_ELECTRON=1"

echo.
echo  =============================================
echo   ModexBot Installer Builder
echo  =============================================
echo.

:: --- 0a. Locate Python (prefer repo .venv, fall back to PATH) ---
set "PYTHON_EXE="
set "REPO_ROOT=%~dp0..\..\.."
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('where python') do if not defined PYTHON_EXE set "PYTHON_EXE=%%i"
    )
)
if not defined PYTHON_EXE (
    echo  [ERROR] Python not found.
    echo    Create a .venv at repo root, or put python on PATH.
    popd
    exit /b 1
)
echo  Python: %PYTHON_EXE%

:: --- 0b. Locate ISCC ---
set "ISCC_EXE="
for %%p in (iscc ISCC) do (
    where %%p >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%i in ('where %%p') do if not defined ISCC_EXE set "ISCC_EXE=%%i"
    )
)
if not defined ISCC_EXE (
    if exist "%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
)
if not defined ISCC_EXE (
    if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 7\ISCC.exe"
)
if not defined ISCC_EXE (
    if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
)
if not defined ISCC_EXE (
    if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
)
if not defined ISCC_EXE (
    if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)
if not defined ISCC_EXE (
    if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
)
if not defined ISCC_EXE (
    echo  [ERROR] Inno Setup ^(ISCC.exe^) not found.
    echo    Install from: https://jrsoftware.org/isdl.php
    popd
    exit /b 1
)
echo  Inno Setup: !ISCC_EXE!

:: --- 1. Version ---
set "VERSION_FILE=%~dp0..\..\..\src\modex_agent\_version.py"
set "VERSION=1.0.0"
for /f "delims=" %%l in ('findstr /r "__version__" "%VERSION_FILE%"') do (
    for /f "tokens=2 delims== " %%v in ("%%l") do (
        set "RAW_VER=%%v"
        set "RAW_VER=!RAW_VER:"=!"
        if not "!RAW_VER!"=="" set "VERSION=!RAW_VER!"
    )
)
echo  Version: %VERSION%

:: --- 2. Clean staging ---
if exist "%STAGING%" rmdir /s /q "%STAGING%"
mkdir "%STAGING%"
echo  Staging: %STAGING%
echo.

:: --- 3. Generate installer icon ---
echo  --- Step 1/7: Generating installer icon ---
"%PYTHON_EXE%" "%~dp0prepare_icon.py"
if errorlevel 1 goto :error
echo.

:: --- 4. Fetch uv ---
:: (Skipped — uv.exe is no longer bundled. The installer is self-contained
::  without it; users who need to add Python deps post-install can install
::  uv/pip themselves. This saves ~58 MB of install footprint.)
echo  --- Step 2/7: Skipping uv.exe fetch (no longer bundled) ---
echo.

:: --- 5. Build source archive ---
echo  --- Step 3/7: Building source archive ---
if "%SKIP_FE%"=="1" (
    "%PYTHON_EXE%" "%~dp0build_archive.py" --staging-dir "%STAGING%"
) else (
    "%PYTHON_EXE%" "%~dp0build_archive.py" --staging-dir "%STAGING%" --force-frontend
)
if errorlevel 1 goto :error
echo.

:: --- 6. Prepare Python runtime (download + install deps + strip) ---
echo  --- Step 4/7: Preparing Python runtime ^(this takes a while^) ---
"%PYTHON_EXE%" "%~dp0prepare_python.py" --staging-dir "%STAGING%"
if errorlevel 1 goto :error
echo.

:: --- 7. Package Electron desktop shell ---
if "%SKIP_ELECTRON%"=="1" (
    echo  --- Step 5/7: Skipping Electron packaging ^(--skip-electron^) ---
) else (
    echo  --- Step 5/7: Packaging Electron desktop shell ^(first run downloads ~200 MB^) ---
    if not exist "%~dp0electron\node_modules" (
        echo  Installing Electron dependencies...
        pushd "%~dp0electron"
        call npm install
        popd
        if errorlevel 1 goto :error
    )
    pushd "%~dp0electron"
    call npm run pack -- --staging-dir "%STAGING%" --icon "%~dp0logo.ico"
    popd
    if errorlevel 1 goto :error
)
echo.

:: --- 8. Compile installer ---
echo  --- Step 6/7: Compiling installer ---
"!ISCC_EXE!" /DMyAppVersion="%VERSION%" /Q "%~dp0modexbot.iss"
if errorlevel 1 goto :error
echo.

:: --- 9. Done ---
set "OUTPUT=%~dp0ModexBot-Setup-%VERSION%.exe"
echo  --- Step 7/7: Done ---
echo.
echo  =============================================
echo   Build complete!
echo  =============================================
echo.
for %%I in ("%OUTPUT%") do echo  Output: %%~fI
for %%I in ("%OUTPUT%") do echo  Size:   %%~zI bytes ^([math]::Round(%%~zI/1MB, 1^) MB^)
echo.
echo  Test on a clean machine — no Python/uv/Node needed on the target.
echo.
popd
exit /b 0

:error
echo.
echo  [ERROR] Build failed.
popd
exit /b 1
