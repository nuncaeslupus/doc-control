; Inno Setup script for the plain Windows installer (PACKAGE T7).
;
; Per-user and unsigned: installs the PyInstaller one-folder build (build-out/doc-control)
; to %LOCALAPPDATA%\Programs\doc-control without admin rights, adds that folder to the
; user PATH (HKCU\Environment), shows the claude_desktop_config.json snippet after the
; install, and registers an uninstaller that takes the PATH entry back out.
;
; It also ships the .mcpb (PACKAGE T14, D-P9). Claude Desktop registers no .mcpb file
; association on Windows, so the finish page offers, unchecked, "Add to Claude Desktop":
; it launches Claude Desktop's Store alias with the file (which opens its extension install
; dialog) or, when the alias is missing, opens Explorer with the file selected to drag onto
; Claude Desktop > Settings > Extensions. A second unchecked option, shown only when the
; config mentions uvx and doc-control, runs `doc-control.exe --remove-uvx-entry --pause`
; (scripts/remove_uvx_entry.py, frozen in): it asks, keeps a .bak and removes only that entry.
;
; Build (package.yml does this with the version from pyproject.toml):
;   ISCC /DAppVersion=X.Y.Z packaging\installer\doc-control.iss
; Relative paths below resolve against this file's folder.

#ifndef AppVersion
  #error AppVersion is not defined: compile with ISCC /DAppVersion=X.Y.Z (pyproject.toml's version)
#endif
#define SourceDir "..\..\build-out\doc-control"
; Built by package.yml (scripts/build_mcpb.py) before ISCC runs; the T8 release asset name.
#define McpbName "doc-control-" + AppVersion + "-windows-x64.mcpb"
; The App Execution Alias the Microsoft Store build of Claude Desktop installs.
#define ClaudeLauncher "{localappdata}\Microsoft\WindowsApps\claude-desktop.exe"

[Setup]
; Never change AppId: it is how an upgrade finds the installed copy and its uninstaller.
AppId={{CFEFB1D0-71C2-4ACA-8849-A24F7BAAFCDD}
AppName=doc-control
AppVersion={#AppVersion}
AppVerName=doc-control {#AppVersion}
DefaultDirName={localappdata}\Programs\doc-control
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ChangesEnvironment=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\build-out
OutputBaseFilename=doc-control-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\doc-control.ico
UninstallDisplayName=doc-control {#AppVersion}
UninstallDisplayIcon={app}\doc-control.exe

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\build-out\{#McpbName}"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; Finish-page options, all unchecked and skipped by a silent install. Exactly one of the
; first two is shown, depending on whether Claude Desktop's launcher alias exists.
Filename: "{#ClaudeLauncher}"; Parameters: """{app}\{#McpbName}"""; Description: "Add to Claude Desktop (opens its extension install dialog)"; Check: HasClaudeLauncher; Flags: postinstall unchecked skipifsilent nowait shellexec
Filename: "{win}\explorer.exe"; Parameters: "/select,""{app}\{#McpbName}"""; Description: "Add to Claude Desktop: show the .mcpb in Explorer; drag it onto Claude Desktop > Settings > Extensions"; Check: not HasClaudeLauncher; Flags: postinstall unchecked skipifsilent nowait
Filename: "{app}\doc-control.exe"; Parameters: "--remove-uvx-entry --pause"; Description: "Remove the old ""uvx doc-control"" entry from claude_desktop_config.json (asks first, keeps a .bak; then restart Claude Desktop)"; Check: HasUvxEntry; Flags: postinstall unchecked skipifsilent

[UninstallDelete]
; Only the frozen build's own folder: {app} itself is removed by the uninstaller once empty,
; so a /DIR= pointed at a folder with other files never loses them.
Type: filesandordirs; Name: "{app}\_internal"

[Code]
const
  EnvKey = 'Environment';

var
  SnippetPage: TOutputMsgMemoWizardPage;

function SameDir(A, B: String): Boolean;
begin
  Result := Lowercase(RemoveBackslashUnlessRoot(Trim(A))) = Lowercase(RemoveBackslashUnlessRoot(Trim(B)));
end;

{ The user PATH with every entry equal to Dir dropped (and empty entries with them). }
function PathWithoutDir(Path, Dir: String; var Found: Boolean): String;
var
  Rest, Part: String;
  P: Integer;
begin
  Result := '';
  Found := False;
  Rest := Path;
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Part := Rest;
      Rest := '';
    end
    else
    begin
      Part := Copy(Rest, 1, P - 1);
      Delete(Rest, 1, P);
    end;
    if SameDir(Part, Dir) then
      Found := True
    else if Trim(Part) <> '' then
    begin
      if Result <> '' then
        Result := Result + ';';
      Result := Result + Part;
    end;
  end;
end;

procedure AddToUserPath(Dir: String);
var
  Path, Kept: String;
  Found: Boolean;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Path) then
    Path := '';
  Kept := PathWithoutDir(Path, Dir, Found);
  if Found then
    Exit;
  if (Path <> '') and (Path[Length(Path)] <> ';') then
    Path := Path + ';';
  if not RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Path + Dir) then
    SuppressibleMsgBox('Could not add ' + Dir + ' to the user PATH.', mbError, MB_OK, IDOK);
end;

procedure RemoveFromUserPath(Dir: String);
var
  Path, Kept: String;
  Found: Boolean;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Path) then
    Exit;
  Kept := PathWithoutDir(Path, Dir, Found);
  if Found then
    RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Kept);
end;

function HasClaudeLauncher: Boolean;
begin
  Result := FileExists(ExpandConstant('{#ClaudeLauncher}'));
end;

{ A cheap look, not a parse: does this config mention both uvx and doc-control?
  scripts/remove_uvx_entry.py decides what (if anything) to remove, and asks first. }
function MentionsUvxDocControl(Path: String): Boolean;
var
  Text: AnsiString;
begin
  Result := False;
  if LoadStringFromFile(Path, Text) then
    Result := (Pos('uvx', Text) > 0) and (Pos('doc-control', Text) > 0);
end;

{ Claude Desktop's config: %APPDATA%\Claude, or the Store build's virtualised copy. Read only. }
function HasUvxEntry: Boolean;
var
  Find: TFindRec;
  Packages: String;
begin
  Result := MentionsUvxDocControl(ExpandConstant('{userappdata}\Claude\claude_desktop_config.json'));
  if Result then
    Exit;
  Packages := ExpandConstant('{localappdata}\Packages\');
  if FindFirst(Packages + 'Claude_*', Find) then
  try
    repeat
      if MentionsUvxDocControl(Packages + Find.Name + '\LocalCache\Roaming\Claude\claude_desktop_config.json') then
      begin
        Result := True;
        Break;
      end;
    until not FindNext(Find);
  finally
    FindClose(Find);
  end;
end;

function JsonPath(S: String): String;
begin
  Result := S;
  StringChangeEx(Result, '\', '\\', True);
end;

procedure InitializeWizard;
begin
  SnippetPage := CreateOutputMsgMemoPage(wpInfoAfter,
    'Use doc-control from Claude Desktop',
    'Add this to claude_desktop_config.json, then restart Claude Desktop.',
    'doc-control is on your PATH for new terminals. For Claude Desktop, the simplest way ' +
    'is "Add to Claude Desktop" on the last page (it installs the .mcpb extension). ' +
    'Or merge this into %APPDATA%\Claude\claude_desktop_config.json:', '');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = SnippetPage.ID then
    SnippetPage.RichEditViewer.Text :=
      '{"mcpServers": {"doc-control": {"command": "' +
      JsonPath(ExpandConstant('{app}\doc-control.exe')) + '", "args": ["mcp"]}}}';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddToUserPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromUserPath(ExpandConstant('{app}'));
end;
