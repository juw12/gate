; gate for Windows - per-user installer (no admin rights). build.py compiles it with:
;   iscc /DAppVersion=1.0.0 /DSourceDir=<dist\gate> /DOutputDir=<dist> /DIconFile=<build\gate.ico> gate.iss
; Installs to %LOCALAPPDATA%\Programs\gate, adds gate to the Start menu (and optionally the desktop) and lists it
; in Settings > Apps. The window keeps its data (what you saved in it) in %LOCALAPPDATA%\gate, which
; uninstalling leaves in place so a reinstall finds it again.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\gate"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef IconFile
  #define IconFile "build\gate.ico"
#endif

[Setup]
AppId={{E81247BA-698B-569E-A6DE-E1E1C366EEFB}
AppName=gate
AppVersion={#AppVersion}
AppVerName=gate {#AppVersion}
AppPublisher=Juwen Lie
AppPublisherURL=https://juw12.github.io/gate/
AppSupportURL=https://github.com/juw12/gate
DefaultDirName={autopf}\gate
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=gate-Windows-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\gate.exe
UninstallDisplayName=gate
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\gate"; Filename: "{app}\gate.exe"; Comment: "Many parallel agent sessions go in; a small number of tested"
Name: "{userdesktop}\gate"; Filename: "{app}\gate.exe"; Comment: "Many parallel agent sessions go in; a small number of tested"; Tasks: desktopicon

[Run]
Filename: "{app}\gate.exe"; Description: "{cm:LaunchProgram,gate}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\gate.exe"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "Stopgate"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  { Updating: close an open gate window first so its files can be replaced. }
  if FileExists(ExpandConstant('{app}\gate.exe')) then
    Exec(ExpandConstant('{app}\gate.exe'), '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
