; KeyUp Dispatch Console installer (Inno Setup 6). Built by .github/workflows/console-windows.yml:
;   iscc /DAppVersion=1.0.0 /DPublishDir=..\..\publish installer\KeyUpConsole.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef PublishDir
  #define PublishDir "..\publish"
#endif

[Setup]
AppId={{5C6A3B2E-7D41-4E8F-9B0A-4B6579557001}
AppName=KeyUp Dispatch Console
AppVersion={#AppVersion}
AppPublisher=KeyUp
DefaultDirName={autopf}\KeyUp Dispatch Console
DefaultGroupName=KeyUp Dispatch Console
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
OutputDir=..\..\dist
OutputBaseFilename=KeyUp-Dispatch-Console-Setup-{#AppVersion}
LicenseFile=..\LICENSE.txt
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\KeyUpConsole.exe
MinVersion=10.0.17763

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#PublishDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Dirs]
; the console (per user) hands its tunnel request to the KeyUp Tunnel service through this folder
Name: "{commonappdata}\KeyUp Dispatch Console"; Permissions: users-modify

[Icons]
Name: "{group}\KeyUp Dispatch Console"; Filename: "{app}\KeyUpConsole.exe"
Name: "{autodesktop}\KeyUp Dispatch Console"; Filename: "{app}\KeyUpConsole.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\KeyUpConsole.exe"; Parameters: "--install-service"; Flags: runhidden waituntilterminated; StatusMsg: "Installing the KeyUp Tunnel service..."
Filename: "{app}\KeyUpConsole.exe"; Description: "Start KeyUp Dispatch Console"; Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
Filename: "{app}\KeyUpConsole.exe"; Parameters: "--uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "KeyUpTunnel"

[Code]
// stop the tunnel service before files are replaced on an upgrade
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop KeyUpTunnel', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
  Result := '';
end;
