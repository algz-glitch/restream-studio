#define MyAppName "Restream Studio"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Restream Studio"
#define MyAppExeName "RestreamStudio.exe"

[Setup]
AppId={{6D5EE4A8-17C8-4DA0-84F8-28710EAB90F4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\RestreamStudio
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=Restream Studio
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\dist\installer
OutputBaseFilename=RestreamStudio-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes

[Files]
Source: "..\dist\RestreamStudio\*"; DestDir: "{app}"; Excludes: "RestreamStudioUpdateHelper.exe"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\RestreamStudio\RestreamStudioUpdateHelper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "firewall-install.ps1"; DestDir: "{app}\packaging"; Flags: ignoreversion
Source: "firewall-remove.ps1"; DestDir: "{app}\packaging"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\Restream Studio"; Filename: "{app}\RestreamStudio.exe"
Name: "{group}\Restream Studio"; Filename: "{app}\RestreamStudio.exe"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\firewall-install.ps1"" -ExecutablePath ""{app}\RestreamStudio.exe"""; StatusMsg: "Configuring the loopback firewall rule..."; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\packaging\firewall-remove.ps1"" -ExecutablePath ""{app}\RestreamStudio.exe"""; Flags: runhidden waituntilterminated; RunOnceId: "RestreamStudioFirewallRemove"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\RestreamStudio"; Check: ShouldDeleteUserData

[Code]
var
  DeleteDataDecisionMade: Boolean;
  DeleteUserData: Boolean;

function ShouldDeleteUserData(): Boolean;
begin
  if not DeleteDataDecisionMade then
  begin
    DeleteUserData := MsgBox(
      'Delete Restream Studio settings, logs, and downloaded updates from %LOCALAPPDATA%\RestreamStudio?' + #13#10 +
      'Choose No to preserve this data for upgrades or later reinstalls.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    DeleteDataDecisionMade := True;
  end;
  Result := DeleteUserData;
end;
