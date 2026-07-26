@echo off
setlocal enabledelayedexpansion
pushd "%~dp0"

:: Resolve to absolute path so the registry entry has no "..\" components
for %%i in ("%~dp0..\..\.venv") do set "ROOT_VENV=%%~fi"
set "VENV_PYTHON=%ROOT_VENV%\Scripts\python.exe"
set "VENV_MARKER=%ROOT_VENV%\.modexbot-pyproject-mtime"
set "TEMP_FILE=%TEMP%\_mx_setup_path.txt"
set "VER_FILE=%TEMP%\_mx_ver.txt"
set "BOT_PID_FILE=%~dp0.modex\bot.pid"

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
:: Helper: discover the uv executable. uv's install location varies (standalone
:: installer -> %USERPROFILE%\.local\bin, winget -> WinGet Links), so never
:: assume a fixed path. Probe PATH first, then known dirs, and pin the absolute
:: path into UV_EXE. All later uv calls use "%UV_EXE%" instead of bare uv.
:: ==========================================================================
goto :skip_discover_uv
:discover_uv
    set "UV_EXE="
    for /f "delims=" %%p in ('where uv 2^>nul') do if not defined UV_EXE set "UV_EXE=%%p"
    if defined UV_EXE goto :eof
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
        goto :eof
    )
    if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" (
        set "UV_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
        goto :eof
    )
    if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" (
        set "UV_EXE=%LOCALAPPDATA%\Programs\uv\uv.exe"
        goto :eof
    )
    goto :eof
:skip_discover_uv

:: ==========================================================================
:: Helper: ensure Node's bin directory is on the CURRENT session PATH so the
:: child "modexbot install" subprocess (which shells out to npm) can find it.
:: Node's install dir varies, so probe known locations instead of assuming.
:: ==========================================================================
goto :skip_ensure_node
:ensure_node_on_path
    where node >nul 2>&1
    if not errorlevel 1 goto :eof
    set "_NODEDIR="
    if exist "%ProgramFiles%\nodejs\node.exe" set "_NODEDIR=%ProgramFiles%\nodejs"
    if not defined _NODEDIR if exist "%ProgramFiles(x86)%\nodejs\node.exe" set "_NODEDIR=%ProgramFiles(x86)%\nodejs"
    if not defined _NODEDIR if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\node.exe" set "_NODEDIR=%LOCALAPPDATA%\Microsoft\WinGet\Links"
    if defined _NODEDIR set "PATH=!PATH!;!_NODEDIR!"
    set "_NODEDIR="
    goto :eof
:skip_ensure_node

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
winget install -e --id OpenJS.NodeJS.LTS --source winget --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :node_winget_fail
call :reload_path
call :ensure_node_on_path
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
:: Only install uv when none is available. If one already exists (PATH or a
:: known dir), reuse it. After any install we re-discover the real path because
:: the install location is not guaranteed.
call :discover_uv
if defined UV_EXE goto :uv_done

echo   [INFO] uv package manager not found (required for Python dependency management^).
echo.
set /p "UV_CHOICE=Install uv automatically? [Y/n]: "
if /i "!UV_CHOICE!"=="n" goto :uv_denied

echo.
echo   Installing uv...
:: winget first (does not pull uv.exe from GitHub), fall back to official installer
where winget >nul 2>&1
if not errorlevel 1 (
    winget install -e --id astral-sh.uv --source winget --accept-source-agreements --accept-package-agreements
    call :reload_path
    call :discover_uv
    if defined UV_EXE goto :uv_ok
)
:: Official PowerShell installer (pulls uv.exe from GitHub releases)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 goto :uv_failed
call :reload_path
call :discover_uv
if not defined UV_EXE goto :uv_not_on_path

:uv_ok
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
echo   [ERROR] uv installed but could not be located.
echo   Try restarting your terminal and re-running install.bat.
popd
exit /b 1

:uv_done
if defined UV_EXE echo   Using uv: !UV_EXE!

:: ==========================================================================
:: 3. Virtual environment
:: ==========================================================================
:: VENV_PYTHON now points to the root venv (ModexAgent\.venv).
:: All venv operations must use the same path to stay in sync.
if exist "%VENV_PYTHON%" (
    echo   Virtual environment found, checking health...
    rem Verify the venv is actually isolated: sys.prefix must point inside it.
    rem Do NOT import third-party packages here; only validate Python itself.
    "%VENV_PYTHON%" -c "import sys; sys.exit(0 if sys.prefix.lower() == r'%ROOT_VENV%'.lower() else 1)" >nul 2>&1
    if errorlevel 1 (
        echo   Existing venv is unhealthy ^(not isolated from system Python^), recreating...
        rmdir /s /q "%ROOT_VENV%"
    ) else (
        echo   Virtual environment is healthy.
        goto :venv_skip_create
    )
)

echo Creating virtual environment (uv-managed Python 3.12)...
:: Force uv to use ONLY its own managed Python — never the system interpreter —
:: so the environment is reproducible and independent of whatever Python (if
:: any) happens to be installed on this machine.
set "UV_PYTHON_PREFERENCE=only-managed"
"%UV_EXE%" python install 3.12
if errorlevel 1 goto :py_download_fail
"%UV_EXE%" venv --python 3.12 "%ROOT_VENV%"
if errorlevel 1 goto :py_download_fail
goto :venv_skip_create

:py_download_fail
echo.
echo [ERROR] Could not obtain uv-managed Python 3.12.
echo   The interpreter is downloaded from GitHub (python-build-standalone^) and
echo   may be blocked or time out on some networks. Set a mirror and re-run:
echo     set "UV_PYTHON_INSTALL_MIRROR=^<mirror-base-url^>"
echo     install.bat
echo   See https://docs.astral.sh/uv/ for the mirror URL format.
popd
exit /b 1

:venv_skip_create

:: ==========================================================================
:: 4. Python dependencies
:: ==========================================================================
:: Copy mode avoids cross-filesystem hardlink failures (cache on C:, venv on
:: another drive) that can leave packages half-extracted. Belt-and-suspenders
:: with [tool.uv] link-mode in the root pyproject.
set "UV_LINK_MODE=copy"

:: uv pip install runs with cwd = bot_project, whose pyproject.toml has no
:: [tool.uv] table, so the root project's mirror index would not be discovered.
:: Set it explicitly so package downloads use the mirror deterministically.
set "UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"

:: Stop any running bot first. On Windows a process holds its imported .pyd/.py
:: files open; reinstalling aiohttp (or any package the bot imports) while it is
:: running corrupts the install — old files deleted, new ones not written, RECORD
:: left empty (the "No module named 'aiohttp._cookie_helpers'" crash).
if exist "%BOT_PID_FILE%" (
    echo   Stopping running bot before dependency reinstall...
    "%VENV_PYTHON%" -m modexbot stop >nul 2>&1
)

set "NEEDS_PIP=0"
if not exist "%VENV_MARKER%" set "NEEDS_PIP=1"

:: Fingerprint BOTH pyproject files: most framework deps live in the root
:: project (..\..\), so a change there must also trigger a reinstall — the bot
:: pyproject alone would miss it.
set "CUR_TS="
for %%I in ("pyproject.toml") do set "CUR_TS=!CUR_TS!%%~tI#"
for %%I in ("..\..\pyproject.toml") do set "CUR_TS=!CUR_TS!%%~tI"
if exist "%VENV_MARKER%" (
    set /p STORED_TS=<"%VENV_MARKER%"
    if not "!CUR_TS!"=="!STORED_TS!" set "NEEDS_PIP=1"
)

if "!NEEDS_PIP!"=="1" (
    echo Installing Python dependencies...
    call :pip_install
    if errorlevel 1 goto :pip_failed
    >"%VENV_MARKER%" echo !CUR_TS!
)

:: Integrity smoke check — runs even when the marker says "already installed",
:: so a previously-corrupted install (interrupted / files held open) is detected
:: and self-heals instead of being silently skipped. aiohttp._cookie_helpers is a
:: canary: it is a pure-python module whose absence is exactly the production
:: crash signature of a half-extracted aiohttp.
"%VENV_PYTHON%" -c "import aiohttp, aiohttp._cookie_helpers, aiohttp.web" >nul 2>&1
if errorlevel 1 (
    echo   Critical import check failed — environment is corrupted, forcing clean reinstall...
    call :pip_install --reinstall
    if errorlevel 1 goto :pip_failed
    "%VENV_PYTHON%" -c "import aiohttp, aiohttp._cookie_helpers, aiohttp.web" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] aiohttp still fails to import after reinstall.
        echo   Ensure the bot is stopped, then delete %ROOT_VENV% and re-run install.bat.
        popd
        exit /b 1
    )
    >"%VENV_MARKER%" echo !CUR_TS!
)
goto :pip_done

:pip_failed
popd
exit /b 1

:pip_done

:: ==========================================================================
:: 5. Environment file
:: ==========================================================================
if exist ".env" goto :env_done
if not exist ".env.example" goto :env_done

echo.
echo [INFO] Created .env from .env.example.
copy .env.example .env >nul
echo   File: %~dp0.env
echo   Edit it only if you use integrations that read it (see the file's comments).
echo.

:env_done

:: ==========================================================================
:: 5b. Global model config
:: ==========================================================================
:: No example template is shipped — model.yml is bootstrapped entirely by the
:: `modexbot config` wizard (run by `modexbot install` below). The wizard
:: creates config/model.yml from scratch with the provider/key you enter.
if exist "config\model.yml" goto :model_done

echo.
echo   ^>^>^> ACTION REQUIRED: Set your model via 'modexbot config' ^<^<^<
echo   The wizard creates config/model.yml from scratch.
echo   Minimum required: model, api_key, url
echo.

:model_done

:: ==========================================================================
:: 6. modexbot install (config wizard + frontend build)
:: ==========================================================================
if "!HAS_NODE!"=="1" (
    echo.
    echo Running modexbot install ^(config check + frontend build^)...
    rem Make sure node/npm is on this session PATH so the child build subprocess
    rem - modexbot shells out to npm - inherits it; node dir may not be on PATH yet.
    call :ensure_node_on_path
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
set "VENV_SCRIPTS=%ROOT_VENV%\Scripts"

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
    rem Broadcast environment change to all running Windows processes so new
    rem terminals and Explorer pick up the updated PATH immediately.
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')" >nul 2>&1
    rem Refresh current session PATH from registry - picks up the venv entry
    rem we just wrote, plus any other recent system-wide changes.
    call :reload_path
    rem Ensure the venv Scripts is in the current session PATH - belt and suspenders.
    call :path_contains PATH VENV_SCRIPTS
    if "!PATH_CONTAINS!"=="0" set "PATH=!PATH!;!VENV_SCRIPTS!"
) else (
    echo   [WARNING] Could not update user PATH automatically.
    echo   Add this directory manually to your PATH:
    echo     !VENV_SCRIPTS!
)
if exist "%TEMP_FILE%" del "%TEMP_FILE%" 2>nul
echo.
echo   Environment variable refresh:
echo     - This cmd window: PATH is already updated, 'modexbot' is ready.
echo     - New terminal windows: will pick up the change automatically.
echo     - Current PowerShell: refresh your session with this command:
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
echo  Next steps:
echo.
echo    1. Configure your model ^(sets model / api_key / url^):
echo.
echo          modexbot config
echo.
echo    2. Start the bot:
echo.
echo          modexbot start
echo.
echo  (start/restart will prompt for config if the model is unset.)
echo.
echo  (If 'modexbot' is not found, open a NEW terminal window first.)
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

:: ==========================================================================
:: Subroutine: install framework + bot deps. Optional %1 = extra uv flags
:: (e.g. --reinstall for the self-healing recovery path). Returns errorlevel.
:: ==========================================================================
:pip_install
set "_PIP_EXTRA=%~1"
:: When --reinstall is requested (self-healing path), explicitly uninstall the
:: editable packages first. uv's --reinstall alone can leave stale physical
:: copies in site-packages when the previous install was interrupted or files
:: were locked — the stale bot\ directory then shadows the .pth source path
:: and the CLI imports outdated code (the modexctl ModuleNotFoundError bug).
if "!_PIP_EXTRA!"=="--reinstall" (
    "%UV_EXE%" pip uninstall --python "%VENV_PYTHON%" modex-bot-project ModexAgent >nul 2>&1
)
"%UV_EXE%" pip install !_PIP_EXTRA! --python "%VENV_PYTHON%" -e "..\..\.[all,dev]"
if errorlevel 1 exit /b 1
"%UV_EXE%" pip install !_PIP_EXTRA! --python "%VENV_PYTHON%" -e ".[webui,dev]"
if errorlevel 1 exit /b 1
:: Editable install guard: a stale physical bot\ directory in any site-packages
:: entry shadows the .pth source path. getsitepackages() can return multiple
:: paths (.venv, .venv\Lib\site-packages), so check each one.
for /f "delims=" %%p in ('"%VENV_PYTHON%" -c "import site,os;[print(sp) for sp in site.getsitepackages() if os.path.isdir(os.path.join(sp,'bot'))]" 2^>nul') do (
    echo   [INFO] Removing stale %%p\bot\ ^(shadows editable source^)...
    rmdir /s /q "%%p\bot" 2>nul
)
exit /b 0
