; ============================================================================
;  ModexBot Windows Installer — Inno Setup script (fully self-contained)
; ============================================================================
;  Build:  iscc /DMyAppVersion=x.x.x modexbot.iss
;  Output: ModexBot-Setup-x.x.x.exe
;
;  Install layout:
;    {app}\
;    python\                    ← bundled CPython 3.12 + ALL third-party deps
;      postinstall.py             ← .pth creation + config init (NO NETWORK)
;      launcher.pyw               ← fallback launcher (starts bot + opens browser)
;      electron\                  ← Electron desktop shell (if packaged)
;        ModexBot.exe             ← desktop window (starts bot + shows WebUI)
;      app\                       ← git archive output (full repo source)
;        pyproject.toml
;        src\modex_agent\
;        examples\bot_project\
;          modexbot\              ← CLI (importable via .pth)
;          bot\                   ← business logic + web/dist (pre-built)
;          config\                ← writable (model.yml, pools/, ...)
;          .env, logs/, data/, .modex\  ← runtime data
;
;  After install: double-click desktop icon → ModexBot.exe (Electron) →
;  bot starts → WebUI opens in desktop window.
;  Fallback: Start Menu → "ModexBot (Browser)" → launcher.pyw → system browser.
;
;  NO DOWNLOADS during installation. Everything is pre-packaged.
;
;  Per-user install (no admin required):
;    PrivilegesRequired=lowest, installs to %LOCALAPPDATA%\Programs\ModexBot
;    No UAC prompt, no Program Files permission hacks needed.
; ============================================================================

#define MyAppName "ModexBot"
#define MyAppPublisher "ModexAgent"
#define MyAppURL "https://github.com/moyu-er/ModexAgent"

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

; Detect whether Electron was packaged into staging (build.bat --skip-electron
; omits it).  The same .iss produces a desktop or browser-only installer.
#ifexist "staging\electron\ModexBot-win32-x64\ModexBot.exe"
  #define HasElectron
#endif

[Setup]
AppId={{B7F3E2A1-4D5C-6E8F-9A0B-1C2D3E4F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsePreviousPrivileges=no
Compression=lzma2/max
SolidCompression=no
LZMANumBlockThreads=4
WizardStyle=modern
OutputDir=.
OutputBaseFilename=ModexBot-Setup-{#MyAppVersion}
SetupIconFile=logo.ico
#ifdef HasElectron
UninstallDisplayIcon={app}\electron\ModexBot.exe
#else
UninstallDisplayIcon={app}\logo.ico
#endif
UninstallDisplayName={#MyAppName}

; ============================================================================
; [Files]
; ============================================================================
[Files]

; Bundled Python runtime (python-build-standalone + all third-party deps)
Source: "staging\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

; Full app source (git archive — only tracked files, no secrets)
Source: "staging\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

; Scripts
Source: "postinstall.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "launcher.pyw"; DestDir: "{app}"; Flags: ignoreversion
Source: "logo.ico"; DestDir: "{app}"; Flags: ignoreversion

; Electron desktop shell (only if packaged — skipped with build.bat --skip-electron)
#ifdef HasElectron
Source: "staging\electron\ModexBot-win32-x64\*"; DestDir: "{app}\electron"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

; ============================================================================
; [Dirs] — runtime directories (per-user install: all dirs are writable)
; ============================================================================
[Dirs]
Name: "{app}\app\examples\bot_project\config"
Name: "{app}\app\examples\bot_project\logs"
Name: "{app}\app\examples\bot_project\data"
Name: "{app}\app\examples\bot_project\.modex"
Name: "{app}\app\examples\bot_project\bot\web\dist"
Name: "{app}\python\Lib\site-packages"

; ============================================================================
; [Tasks]
; ============================================================================
[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

; ============================================================================
; [Run] — post-installation (NO NETWORK, just file ops + verify)
; ============================================================================
[Run]

; Create .pth files + init config + verify imports — uses bundled Python
Filename: "{app}\python\python.exe"; \
  Parameters: """{app}\postinstall.py"" --app-dir ""{app}"""; \
  StatusMsg: "Wiring up source links and initialising configuration..."; \
  Flags: runhidden

; ============================================================================
; [Icons] — shortcuts
; ============================================================================
[Icons]

; Start Menu — main launch
#ifdef HasElectron
Name: "{group}\ModexBot"; \
  Filename: "{app}\electron\ModexBot.exe"; \
  WorkingDir: "{app}\electron"; \
  Comment: "Start ModexBot (Desktop)"
#else
Name: "{group}\ModexBot"; \
  Filename: "{app}\python\pythonw.exe"; \
  Parameters: """{app}\launcher.pyw"""; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Comment: "Start ModexBot and open WebUI"; \
  IconFilename: "{app}\logo.ico"
#endif

; Start Menu — browser fallback (always available)
#ifdef HasElectron
Name: "{group}\ModexBot (Browser)"; \
  Filename: "{app}\python\pythonw.exe"; \
  Parameters: """{app}\launcher.pyw"""; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Comment: "Start ModexBot and open in system browser"; \
  IconFilename: "{app}\logo.ico"
#endif

; Start Menu — stop
Name: "{group}\ModexBot Stop"; \
  Filename: "{app}\python\python.exe"; \
  Parameters: "-m modexbot stop --port 21810"; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Comment: "Stop ModexBot"

; Start Menu — logs
Name: "{group}\ModexBot Logs"; \
  Filename: "{app}\python\python.exe"; \
  Parameters: "-m modexbot logs -f --port 21810"; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Comment: "View ModexBot logs"

; Start Menu — config wizard
Name: "{group}\ModexBot Config"; \
  Filename: "{app}\python\python.exe"; \
  Parameters: "-m modexbot config"; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Comment: "Configure model and settings"

; Start Menu — open config folder in Explorer
Name: "{group}\ModexBot Config Folder"; \
  Filename: "explorer.exe"; \
  Parameters: """{app}\app\examples\bot_project\config"""; \
  Comment: "Open the configuration folder"

; Start Menu — open logs folder in Explorer
Name: "{group}\ModexBot Logs Folder"; \
  Filename: "explorer.exe"; \
  Parameters: """{app}\app\examples\bot_project\logs"""; \
  Comment: "Open the logs folder"

; Desktop — main launch
#ifdef HasElectron
Name: "{userdesktop}\ModexBot"; \
  Filename: "{app}\electron\ModexBot.exe"; \
  WorkingDir: "{app}\electron"; \
  Comment: "Start ModexBot (Desktop)"; \
  Tasks: desktopicon
#else
Name: "{userdesktop}\ModexBot"; \
  Filename: "{app}\python\pythonw.exe"; \
  Parameters: """{app}\launcher.pyw"""; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Comment: "Start ModexBot and open WebUI"; \
  IconFilename: "{app}\logo.ico"; \
  Tasks: desktopicon
#endif

; ============================================================================
; [UninstallRun]
; ============================================================================
[UninstallRun]
Filename: "{app}\python\python.exe"; \
  Parameters: "-m modexbot stop --port 21810"; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Flags: runhidden; \
  RunOnceId: "StopBot"

; ============================================================================
; [UninstallDelete]
; ============================================================================
[UninstallDelete]
#ifdef HasElectron
Type: filesandordirs; Name: "{app}\electron"
#endif
Type: filesandordirs; Name: "{app}\app\examples\bot_project\logs"
Type: filesandordirs; Name: "{app}\app\examples\bot_project\data"
Type: filesandordirs; Name: "{app}\app\examples\bot_project\.modex"

; ============================================================================
; [Code] — Pascal script: register/unregister PATH + __pycache__ cleanup
; ============================================================================
[Code]

const
  WM_SETTINGCHANGE = 26;

procedure BroadcastSettingChange();
var
  Dummy: DWORD;
begin
  PostMessage(HWND_BROADCAST, WM_SETTINGCHANGE, 0, 0);
end;

procedure DeletePycacheDir(Dir: string);
var
  FindRec: TFindRec;
  SubPath: string;
begin
  if not DirExists(Dir) then
    Exit;
  if FindFirst(Dir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          SubPath := Dir + '\' + FindRec.Name;
          if FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
          begin
            if FindRec.Name = '__pycache__' then
              DelTree(SubPath, True, True, True)
            else
              DeletePycacheDir(SubPath);
          end;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function ModPathDir(): String;
begin
  Result := ExpandConstant('{app}\python\Scripts');
end;

procedure EnvAddPath(Path: string);
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
    Paths := '';
  if Pos(';' + LowerCase(Path) + ';', ';' + LowerCase(Paths) + ';') > 0 then
    Exit;
  // Prepend (not append) so the installed modexbot.bat wins over dev venv
  // or anaconda copies of modexbot.exe that may already be on PATH.
  if Paths <> '' then
    Paths := Path + ';' + Paths
  else
    Paths := Path;
  RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths);
  BroadcastSettingChange();
end;

procedure EnvRemovePath(Path: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
    Exit;
  P := Pos(';' + LowerCase(Path) + ';', ';' + LowerCase(Paths) + ';');
  if P = 0 then
    Exit;
  Delete(Paths, P, Length(Path) + 1);
  RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths);
  BroadcastSettingChange();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    EnvAddPath(ModPathDir());
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    EnvRemovePath(ModPathDir());
    DeletePycacheDir(ExpandConstant('{app}\app'));
    DeletePycacheDir(ExpandConstant('{app}\python\Lib'));
  end;
end;
