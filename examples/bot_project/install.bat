@echo off
setlocal enabledelayedexpansion
pushd "%~dp0"

set "ROOT_VENV=%~dp0..\..\.venv"
set "VENV_PYTHON=%ROOT_VENV%\Scripts\python.exe"
set "VENV_MARKER=%ROOT_VENV%\.modexbot-pyproject-mtime"
set "TEMP_FILE=%TEMP%\_mx_setup_path.txt"
set "VER_FILE=%TEMP%\_mx_ver.txt"

echo.
echo  =============================================
echo   ModexBot - Environment Setup
echo  =============================================
echo.

:: ==========================================================================
:: Helper: reload PATH from the registry
:: ==========================================================================
goto :skip_reload_path
:reload_path
    set "NEW_PATH="
    reg query "HKCU\Environment" /v PATH > "%TEMP_FILE%" 2>nul
    if not errorlevel 1 (
        for /f "skip=2 tokens=2*" %%a in (%TEMP_FILE%) do set "NEW_PATH=%%b"
    )
    reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v PATH > "%TEMP_FILE%" 2>nul
    if not errorlevel 1 (
        for /f "skip=2 tokens=2*" %%a in (%TEMP_FILE%) do (
            if defined NEW_PATH (set "NEW_PATH=!NEW_PATH!;%%b") else (set "NEW_PATH=%%b")
        )
    )
    if exist "%TEMP_FILE%" del "%TEMP_FILE%" 2>nul
    if defined NEW_PATH set "PATH=!NEW_PATH!"
    goto :eof
:skip_reload_path

:: ==========================================================================
:: Helper: get tool version into variable (avoids for /f in nested blocks)
:: ==========================================================================
goto :skip_getver
:getver
    :: %1 = tool name (node, uv), %2 = output variable name
    %1 --version > "%VER_FILE%" 2>nul
    set /p %2=<"%VER_FILE%"
    del "%VER_FILE%" 2>nul
    goto :eof
:skip_getver

:: ==========================================================================
:: Helper: check whether a semicolon-separated list (variable named %1)
:: contains the exact entry stored in variable named %2 (case-insensitive).
:: Sets PATH_CONTAINS=1 if found, 0 otherwise.
:: Uses PowerShell because findstr mishandles paths with dots/backslashes.
:: ==========================================================================
goto :skip_path_contains
:path_contains
    set "PATH_CONTAINS=0"
    set "_HAYSTACK=!%1!"
    set "_NEEDLE=!%2!"
    for /f "delims=" %%r in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$h='!_HAYSTACK!'; $n='!_NEEDLE!'; $found=0; foreach ($e in ($h -split ';')) { if ($e.Trim() -ieq $n) { $found=1; break } } Write-Output $found"') do set "PATH_CONTAINS=%%r"
    set "_HAYSTACK="
    set "_NEEDLE="
    goto :eof
:skip_path_contains

:: ==========================================================================
:: 1. Node.js
:: ==========================================================================
set "HAS_NODE=0"
where node >nul 2>&1
if not errorlevel 1 goto :node_found
goto :node_missing

:node_found
set "HAS_NODE=1"
call :getver node NODE_VER
echo   Node.js: !NODE_VER!
goto :node_done

:node_missing
echo.
echo   [WARNING] Node.js not found.
echo   Node.js is required to build the WebUI frontend.
echo.
where winget >nul 2>&1
if not errorlevel 1 goto :node_winget
goto :node_manual

:node_winget
echo   WinGet detected - can install Node.js automatically.
set /p "NODE_INSTALL=Install Node.js LTS via winget now? [Y/n]: "
if /i "!NODE_INSTALL!"=="n" goto :node_manual
echo   Installing Node.js LTS via winget...
winget install -e --id OpenJS.NodeJS.LTS --source winget
if errorlevel 1 goto :node_winget_fail
call :reload_path
where node >nul 2>&1
if errorlevel 1 goto :node_winget_fail
set "HAS_NODE=1"
call :getver node NODE_VER
echo   Node.js !NODE_VER! installed successfully.
goto :node_done

:node_winget_fail
echo   winget install finished but node not found on PATH.
echo   Try restarting your terminal and re-running install.bat.
goto :node_manual

:node_manual
echo   Install manually from: https://nodejs.org (LTS version recommended^)
echo.
set /p "NODE_CHOICE=Continue without frontend build? [y/N]: "
if /i "!NODE_CHOICE!"=="y" (
    echo   OK - will skip frontend build. WebUI will NOT be available.
    echo   After installing Node.js, re-run install.bat or use: modexbot install
    goto :node_done
)
echo   Setup aborted. Install Node.js and re-run install.bat.
popd
exit /b 1

:node_done
echo.

:: ==========================================================================
:: 2. uv
:: ==========================================================================
where uv >nul 2>&1
if not errorlevel 1 goto :uv_done

echo   [INFO] uv package manager not found (required for Python dependency management^).
echo.
set /p "UV_CHOICE=Install uv automatically (official standalone installer^)? [Y/n]: "
if /i "!UV_CHOICE!"=="n" goto :uv_denied

echo.
echo   Installing uv...
:: Try winget first (no PowerShell dependency), fall back to official installer
where winget >nul 2>&1
if not errorlevel 1 (
    winget install -e --id astral-sh.uv --source winget
    if not errorlevel 1 goto :uv_installed
)
:: Official PowerShell installer (works on all Windows 10+)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 goto :uv_failed

:uv_installed

call :reload_path
where uv >nul 2>&1
if errorlevel 1 goto :uv_not_on_path

echo   uv installed successfully.
echo.
goto :uv_done

:uv_denied
echo.
echo   Cannot proceed without uv.
echo   Install manually: https://docs.astral.sh/uv/
echo   Then re-run install.bat.
popd
exit /b 1

:uv_failed
echo.
echo   [ERROR] uv installer failed (PowerShell error^).
echo   Install manually: https://docs.astral.sh/uv/
echo   Then re-run install.bat.
popd
exit /b 1

:uv_not_on_path
echo.
echo   [ERROR] uv installed but not found on PATH after reload.
echo   Try restarting your terminal and re-running install.bat.
popd
exit /b 1

:uv_done

:: ==========================================================================
:: 3. Virtual environment
:: ==========================================================================
:: VENV_PYTHON now points to the root venv (ModexAgent\.venv).
:: All venv operations must use the same path to stay in sync.
if exist "%VENV_PYTHON%" (
    echo   Virtual environment already exists, skipping creation.
    goto :venv_skip_create
)

echo Creating virtual environment...
:: Use miniforge Python if available (avoids corporate SSL issues with GitHub downloads)
if exist "D:\programs\miniforge\python.exe" (
    echo   Using system Python ...
    "D:\programs\miniforge\python.exe" -m venv "%ROOT_VENV%"
    if not errorlevel 1 goto :venv_skip_create
)

:: Try uv with existing Python first
uv venv "%ROOT_VENV%" 2>nul
if not errorlevel 1 goto :venv_skip_create

:: Last resort: uv downloads Python (may fail on corporate SSL intercepted networks)
uv venv --python 3.12 "%ROOT_VENV%"
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create virtual environment.
    echo   Check network connectivity and retry.
    popd
    exit /b 1
)

:venv_skip_create

:: ==========================================================================
:: 4. Python dependencies
:: ==========================================================================
set "NEEDS_PIP=0"
if not exist "%VENV_MARKER%" set "NEEDS_PIP=1"

if exist "pyproject.toml" (
    for %%I in ("pyproject.toml") do set "CUR_TS=%%~tI"
    if exist "%VENV_MARKER%" (
        set /p STORED_TS=<"%VENV_MARKER%"
        if not "!CUR_TS!"=="!STORED_TS!" set "NEEDS_PIP=1"
    )
)

if "!NEEDS_PIP!"=="0" goto :pip_done

echo Installing Python dependencies...
uv pip install --python "%VENV_PYTHON%" -e "..\..\.[all,dev]"
if errorlevel 1 (
    echo [ERROR] Framework install failed
    popd
    exit /b 1
)
uv pip install --python "%VENV_PYTHON%" -e ".[webui,dev]"
if errorlevel 1 (
    echo [ERROR] CLI install failed
    popd
    exit /b 1
)
for %%I in ("pyproject.toml") do echo %%~tI> "%VENV_MARKER%"

:pip_done

:: ==========================================================================
:: 5. Environment file
:: ==========================================================================
if exist ".env" goto :env_done
if not exist ".env.example" goto :env_done

echo.
echo [INFO] Creating .env from .env.example...
copy .env.example .env >nul
echo.
echo   ^>^>^> ACTION REQUIRED: Edit .env with your credentials ^<^<^<
echo   File: %~dp0.env
echo   Minimum required: LLM_MODEL, LLM_API_KEY, LLM_BASE_URL
echo.

:env_done

:: ==========================================================================
:: 6. modexbot install (config wizard + frontend build)
:: ==========================================================================
if "!HAS_NODE!"=="1" (
    echo.
    echo Running modexbot install ^(config check + frontend build^)...
    "%VENV_PYTHON%" -m modexbot install
    if errorlevel 1 (
        echo.
        echo [WARNING] modexbot install encountered errors.
        echo   You can retry after fixing the issues above:
        echo     "%VENV_PYTHON%" -m modexbot install
    )
) else (
    echo.
    echo [INFO] Node.js not available - running config wizard only.
    echo   Frontend build will be skipped ^(WebUI will NOT be available^).
    echo.
    "%VENV_PYTHON%" -m modexbot config
    echo.
    echo   After installing Node.js, rebuild the frontend with:
    echo     "%VENV_PYTHON%" -m modexbot install -f
)

:: ==========================================================================
:: 7. Register modexbot CLI globally (add .venv\Scripts to user PATH)
:: ==========================================================================
set "VENV_SCRIPTS=%~dp0..\..\.venv\Scripts"

:: Check if already registered by looking for the full venv Scripts path in
:: the current HKCU PATH. This works regardless of the parent directory name.
set "USER_PATH="
reg query "HKCU\Environment" /v PATH > "%TEMP_FILE%" 2>nul
if not errorlevel 1 (
    for /f "skip=2 tokens=2*" %%a in (%TEMP_FILE%) do set "USER_PATH=%%b"
)
if defined USER_PATH (
    call :path_contains USER_PATH VENV_SCRIPTS
    if "!PATH_CONTAINS!"=="1" goto :path_skip
)

echo.
echo   [INFO] The 'modexbot' CLI is installed in .venv\Scripts\modexbot.exe
echo   Adding this directory to your user PATH lets you run 'modexbot'
echo   from any terminal without activating the venv.
echo.
set /p "PATH_CHOICE=Add .venv\Scripts to your user PATH? [Y/n]: "
if /i "!PATH_CHOICE!"=="n" goto :path_done

:: Append venv scripts to the existing user PATH, preserving every existing
:: entry exactly. If there is no user PATH yet, create one with only venv.
if defined USER_PATH (
    reg add "HKCU\Environment" /v PATH /t REG_EXPAND_SZ /d "!USER_PATH!;!VENV_SCRIPTS!" /f >nul 2>&1
) else (
    reg add "HKCU\Environment" /v PATH /t REG_EXPAND_SZ /d "!VENV_SCRIPTS!" /f >nul 2>&1
)
if not errorlevel 1 (
    echo   Added to user PATH.
    :: Update current session PATH without duplicating the entry.
    call :path_contains PATH VENV_SCRIPTS
    if "!PATH_CONTAINS!"=="0" set "PATH=!PATH!;!VENV_SCRIPTS!"
) else (
    echo   [WARNING] Could not update user PATH automatically.
    echo   Add this directory manually to your PATH:
    echo     !VENV_SCRIPTS!
)
if exist "%TEMP_FILE%" del "%TEMP_FILE%" 2>nul
echo.
echo   To use 'modexbot' right now without restarting:
echo     - cmd.exe: already active in this window
echo     - PowerShell: run this in your current terminal:
echo         $env:Path = [Environment]::GetEnvironmentVariable('Path','User')
goto :path_done

:path_skip
:: Already registered — nothing to do, not even a prompt
if exist "%TEMP_FILE%" del "%TEMP_FILE%" 2>nul

:path_done

:: ==========================================================================
:: Done
:: ==========================================================================
echo.
echo  =============================================
echo   Setup complete!
echo  =============================================
echo.
echo  What's been set up:
echo    - uv package manager
echo    - Python virtual environment ^(%ROOT_VENV%^)
echo    - Framework + bot dependencies
if "!HAS_NODE!"=="1" (
    echo    - WebUI frontend ^(bot\web\dist^)
) else (
    echo    - WebUI frontend: SKIPPED ^(Node.js not available^)
)
echo.
echo  Next step:
echo.
echo        modexbot start
echo.
echo  The bot will be available at: http://localhost:21800/webui/
if "!HAS_NODE!"=="0" (
    echo.
    echo  ^(WebUI will not work until Node.js is installed and frontend is built^)
    echo  After installing Node.js, run: modexbot install -f
)
echo.
echo  Other commands:
echo    modexbot stop         - Stop the bot
echo    modexbot logs -f      - View live logs
echo    modexbot install -f   - Rebuild frontend
echo    modexbot config       - Interactive config wizard
echo.

popd
exit /b 0
