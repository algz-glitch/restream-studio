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
Source: "firewall-install.ps1"; Flags: dontcopy
Source: "firewall-install.ps1"; DestDir: "{app}\packaging"; Flags: ignoreversion
Source: "firewall-remove.ps1"; DestDir: "{app}\packaging"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\Restream Studio"; Filename: "{app}\RestreamStudio.exe"
Name: "{group}\Restream Studio"; Filename: "{app}\RestreamStudio.exe"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\RestreamStudio"; Check: ShouldDeleteUserData

[Code]
var
  DeleteDataDecisionMade: Boolean;
  DeleteUserData: Boolean;
  LaunchAfterInstallCheck: TNewCheckBox;

function HasCommandLineArgument(const Expected: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
  begin
    if CompareText(ParamStr(Index), Expected) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function ShouldDeleteUserData(): Boolean;
var
  DataPath: String;
begin
  if UninstallSilent then
  begin
    Result := HasCommandLineArgument('/DELETEUSERDATA=1');
    Exit;
  end;

  if not DeleteDataDecisionMade then
  begin
    DataPath := ExpandConstant('{localappdata}\RestreamStudio');
    DeleteUserData := MsgBox(
      'Delete Restream Studio settings, logs, and downloaded updates from ' +
      DataPath + '?' + #13#10 +
      'Choose No to preserve this data for upgrades or later reinstalls.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    DeleteDataDecisionMade := True;
  end;
  Result := DeleteUserData;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  PowerShellPath: String;
  ScriptPath: String;
  ApplicationPath: String;
  Parameters: String;
begin
  Result := '';
  ResultCode := -1;
  ExtractTemporaryFile('firewall-install.ps1');
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ScriptPath := ExpandConstant('{tmp}\firewall-install.ps1');
  ApplicationPath := ExpandConstant('{app}\RestreamStudio.exe');
  Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath +
    '" -ExecutablePath "' + ApplicationPath + '"';
  if not Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated,
    ResultCode) then
  begin
    Result := 'Unable to start the required firewall configuration.';
    Exit;
  end;
  if ResultCode <> 0 then
    Result := 'Firewall configuration failed. Installation was not started.';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  PowerShellPath: String;
  ScriptPath: String;
  ApplicationPath: String;
  Parameters: String;
  Started: Boolean;
begin
  if CurUninstallStep = usUninstall then
  begin
    ResultCode := -1;
    PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
    ScriptPath := ExpandConstant('{app}\packaging\firewall-remove.ps1');
    ApplicationPath := ExpandConstant('{app}\RestreamStudio.exe');
    Parameters := '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath +
      '" -ExecutablePath "' + ApplicationPath + '"';
    Started := Exec(PowerShellPath, Parameters, '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
    if (not Started) or (ResultCode <> 0) then
    begin
      SuppressibleMsgBox(
        'Firewall cleanup failed. Uninstall was stopped before deleting files.',
        mbError, MB_OK, IDOK);
      Abort;
    end;
  end;
end;

procedure InitializeWizard;
begin
  LaunchAfterInstallCheck := TNewCheckBox.Create(WizardForm);
  LaunchAfterInstallCheck.Parent := WizardForm.FinishedPage;
  LaunchAfterInstallCheck.Left := WizardForm.FinishedLabel.Left;
  LaunchAfterInstallCheck.Top := WizardForm.FinishedLabel.Top +
    WizardForm.FinishedLabel.Height + ScaleY(16);
  LaunchAfterInstallCheck.Width := WizardForm.FinishedPage.ClientWidth -
    LaunchAfterInstallCheck.Left - ScaleX(16);
  LaunchAfterInstallCheck.Caption := 'Launch Restream Studio';
  LaunchAfterInstallCheck.Checked := True;
  LaunchAfterInstallCheck.Visible := not WizardSilent;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if (CurPageID = wpFinished) and (not WizardSilent) and
    LaunchAfterInstallCheck.Checked then
  begin
    ResultCode := -1;
    if not Exec(ExpandConstant('{app}\RestreamStudio.exe'), '',
      ExpandConstant('{app}'), SW_SHOWNORMAL, ewNoWait, ResultCode) then
      SuppressibleMsgBox('Restream Studio could not be started.',
        mbError, MB_OK, IDOK);
  end;
end;
