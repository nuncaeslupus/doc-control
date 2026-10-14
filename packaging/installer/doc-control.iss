; Inno Setup script for the plain Windows installer (PACKAGE T7).
;
; Per-user and unsigned: installs the PyInstaller one-folder build (build-out/doc-control)
; to %LOCALAPPDATA%\Programs\doc-control without admin rights, adds that folder to the
; user PATH (HKCU\Environment), shows the claude_desktop_config.json snippet after the
; install, and registers an uninstaller that takes the PATH entry back out.
;
; Build (package.yml does this with the version from pyproject.toml):
;   ISCC /DAppVersion=X.Y.Z packaging\installer\doc-control.iss
; Relative paths below resolve against this file's folder.

#ifndef AppVersion
  #error AppVersion is not defined: compile with ISCC /DAppVersion=X.Y.Z (pyproject.toml's version)
#endif
#define SourceDir "..\..\build-out\doc-control"

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
UninstallDisplayName=doc-control {#AppVersion}
UninstallDisplayIcon={app}\doc-control.exe

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

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
    'doc-control is on your PATH for new terminals. To use it from Claude Desktop, ' +
    'merge this into %APPDATA%\Claude\claude_desktop_config.json:', '');
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
