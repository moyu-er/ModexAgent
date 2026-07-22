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
;      tauri\                     ← Tauri desktop shell (if packaged)
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
;  After install: double-click desktop icon → ModexBot.exe (Tauri) →
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

; Detect whether Tauri was packaged into staging (build.bat --skip-tauri
; omits it).  The same .iss produces a desktop or browser-only installer.
#ifexist "staging\tauri\ModexBot.exe"
  #define HasTauri
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
#ifdef HasTauri
UninstallDisplayIcon={app}\tauri\ModexBot.exe
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

; Tauri desktop shell (only if packaged — skipped with build.bat --skip-tauri)
#ifdef HasTauri
Source: "staging\tauri\*"; DestDir: "{app}\tauri"; Flags: ignoreversion recursesubdirs createallsubdirs
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
#ifdef HasTauri
Name: "{group}\ModexBot"; \
  Filename: "{app}\tauri\ModexBot.exe"; \
  WorkingDir: "{app}\tauri"; \
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
#ifdef HasTauri
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
  Parameters: "-m modexbot stop"; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Comment: "Stop ModexBot"

; Start Menu — logs
Name: "{group}\ModexBot Logs"; \
  Filename: "{app}\python\python.exe"; \
  Parameters: "-m modexbot logs -f"; \
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
#ifdef HasTauri
Name: "{userdesktop}\ModexBot"; \
  Filename: "{app}\tauri\ModexBot.exe"; \
  WorkingDir: "{app}\tauri"; \
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
  Parameters: "-m modexbot stop"; \
  WorkingDir: "{app}\app\examples\bot_project"; \
  Flags: runhidden; \
  RunOnceId: "StopBot"

; ============================================================================
; [UninstallDelete]
; ============================================================================
[UninstallDelete]
#ifdef HasTauri
Type: filesandordirs; Name: "{app}\tauri"
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

// ============================================================================
// Idempotent PATH registration (Layer 3 fix)
// ============================================================================
// Previous EnvAddPath/EnvRemovePath matched the *exact* string of the path
// being added. Reinstalling to a different directory left the old path
// entry behind, accumulating stale entries across reinstalls. The new
// implementation uses a marker substring ('\python\Scripts' under a ModexBot
// install) to identify our entries, so:
//
//   - EnvAddPath: removes ALL existing ModexBot marker entries first, then
//     prepends the new one. Idempotent — reinstalling to the same or a
//     different directory leaves exactly ONE entry, always the latest.
//   - EnvRemovePath: removes ALL marker entries, not just one. Survives
//     uninstall even if the install dir changed between install and uninstall.
//
// Marker choice: '\python\Scripts' (case-insensitive). This is specific
// enough — only ModexBot's bundled python-build-standalone layout uses
// 'python\Scripts' as a directory name on the user's PATH. Other apps
// typically use '<AppName>\Scripts' or '<AppName>\bin'. Even if a false
// positive occurred, the worst case is removing an unrelated stale entry,
// not corrupting the user's PATH (we only touch HKCU\Environment\Path).
//
// Inno Setup 6.4+ provides StringSplit / StringJoin with this signature:
//   function StringSplit(const S: String; const Separators: TArrayOfString;
//                        const Typ: TSplitType): TArrayOfString;
//   function StringJoin(const Separator: String;
//                       const Values: TArrayOfString): String;
// ============================================================================

function IsModexBotPathEntry(Entry: string): Boolean;
begin
  // Match our install layout: any path ending in '\python\Scripts' (case-
  // insensitive). The ModexBot install dir is per-user (%LOCALAPPDATA%\
  // Programs\ModexBot), so this is specific to our registration.
  Result := Pos('\python\Scripts', LowerCase(Entry)) > 0;
end;

procedure EnvAddPath(Path: string);
var
  Paths: string;
  RawEntries: TArrayOfString;
  KeptEntries: TArrayOfString;
  I: Integer;
  KeptCount: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
    Paths := '';

  // stExcludeEmpty drops empty entries (no leading/trailing/duplicate ';'
  // artefacts). Single-element Separators array per Inno Setup API.
  RawEntries := StringSplit(Paths, [';'], stExcludeEmpty);

  // First pass: count entries we'll keep so we can size the array once.
  KeptCount := 0;
  for I := 0 to GetArrayLength(RawEntries) - 1 do
    if not IsModexBotPathEntry(RawEntries[I]) then
      KeptCount := KeptCount + 1;

  // Always reserve one slot for the new path we're prepending.
  SetArrayLength(KeptEntries, KeptCount + 1);
  KeptEntries[0] := Path;
  KeptCount := 1;
  for I := 0 to GetArrayLength(RawEntries) - 1 do
  begin
    if not IsModexBotPathEntry(RawEntries[I]) then
    begin
      KeptEntries[KeptCount] := RawEntries[I];
      KeptCount := KeptCount + 1;
    end;
  end;

  RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', StringJoin(';', KeptEntries));
  BroadcastSettingChange();
end;

procedure EnvRemovePath(Path: string);
var
  Paths: string;
  RawEntries: TArrayOfString;
  KeptEntries: TArrayOfString;
  I: Integer;
  KeptCount: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Paths) then
    Exit;

  RawEntries := StringSplit(Paths, [';'], stExcludeEmpty);

  // Count survivors first.
  KeptCount := 0;
  for I := 0 to GetArrayLength(RawEntries) - 1 do
    if not IsModexBotPathEntry(RawEntries[I]) then
      KeptCount := KeptCount + 1;

  SetArrayLength(KeptEntries, KeptCount);
  KeptCount := 0;
  for I := 0 to GetArrayLength(RawEntries) - 1 do
  begin
    if not IsModexBotPathEntry(RawEntries[I]) then
    begin
      KeptEntries[KeptCount] := RawEntries[I];
      KeptCount := KeptCount + 1;
    end;
  end;

  // Only write + broadcast if something actually changed. Avoids spurious
  // WM_SETTINGCHANGE broadcasts on uninstall when no marker was present.
  if StringJoin(';', KeptEntries) <> Paths then
  begin
    RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', StringJoin(';', KeptEntries));
    BroadcastSettingChange();
  end;
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
