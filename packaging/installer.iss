#ifndef Payload
  #error Payload required
#endif
#ifndef BuildOutput
  #error BuildOutput required
#endif
#ifndef BuildIcon
  #error BuildIcon required
#endif
#ifndef BuildVersion
  #define BuildVersion "0.5.0.45"
#endif
#ifndef AppIdentity
  #define AppIdentity "GNSSLogger-PrivateEvaluation"
#endif
#ifndef AppGroup
  #define AppGroup "GNSS Logger"
#endif
[Setup]
AppId={#AppIdentity}
AppName=GNSS Logger (private evaluation)
AppVersion={#BuildVersion}
SetupIconFile={#BuildIcon}
DefaultDirName={localappdata}\Programs\GNSSLogger
DefaultGroupName={#AppGroup}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#BuildOutput}
OutputBaseFilename=GNSSLogger-{#BuildVersion}-setup
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no
AppMutex=Local\GNSSLogger.GUI,Local\GNSSLogger.CLI
UninstallDisplayIcon={app}\gnss-logger-gui.exe
DisableProgramGroupPage=yes
[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\GNSS Logger"; Filename: "{app}\gnss-logger-gui.exe"
Name: "{group}\GNSS Logger CLI help"; Filename: "{cmd}"; Parameters: "/K ""{app}\gnss-logger.exe"" --help"
[Code]
function InitializeSetup(): Boolean;
begin
  Result := not CheckForMutexes('Local\GNSSLogger.GUI,Local\GNSSLogger.CLI');
  if not Result then SuppressibleMsgBox('Close GNSS Logger safely before updating. Recording will never be terminated by Setup.', mbError, MB_OK, IDOK);
end;
function InitializeUninstall(): Boolean;
begin
  Result := not CheckForMutexes('Local\GNSSLogger.GUI,Local\GNSSLogger.CLI');
  if not Result then SuppressibleMsgBox('Close GNSS Logger safely before uninstalling.', mbError, MB_OK, IDOK);
end;
