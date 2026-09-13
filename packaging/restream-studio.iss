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
Source: "..\dist\RestreamStudio\RestreamStudio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\RestreamStudio\*"; DestDir: "{app}"; Excludes: "RestreamStudio.exe,RestreamStudioUpdateHelper.exe"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\RestreamStudio\RestreamStudioUpdateHelper.exe"; DestDir: "{app}"; Flags: ignoreversion
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

function HexDigit(const Value: Integer): String;
begin
  if Value < 10 then
    Result := Chr(Ord('0') + Value)
  else
    Result := Chr(Ord('a') + Value - 10);
end;

function HexEncode(const Value: String): String;
var
  Index: Integer;
  Character: Integer;
begin
  Result := '';
  for Index := 1 to Length(Value) do
  begin
    Character := Ord(Value[Index]);
    Result := Result + HexDigit((Character shr 12) and 15) +
      HexDigit((Character shr 8) and 15) +
      HexDigit((Character shr 4) and 15) + HexDigit(Character and 15);
  end;
end;

function RunElevatedPowerShell(const Command, ApplicationPath: String;
  var ResultCode: Integer): Boolean;
var
  Parameters: String;
begin
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "& { ' +
    Command + ' }" ' + HexEncode(ApplicationPath);
  Result := ShellExec('runas',
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function InstalledFirewallCommand(): String;
begin
  Result :=
    '$ErrorActionPreference=''Stop'';' +
    '$h=$args[0];if(($h.Length%4)-ne 0){throw ''bad path''};' +
    '$p='''';for($i=0;$i-lt $h.Length;$i+=4){' +
      '$p+=[char][Convert]::ToUInt16($h.Substring($i,4),16)};' +
    'if((-not [IO.Path]::IsPathFullyQualified($p))-or' +
      '([IO.Path]::GetFileName($p)-ine ''RestreamStudio.exe'')){throw ''bad path''};' +
    '$n=''RestreamStudio-Installed-Localhost'';' +
    '$legacy=''Restream Studio (Loopback TCP)'';' +
    '$restoreProgram=$null;' +
    '$candidates=@(@(Get-NetFirewallRule -Name $n -EA SilentlyContinue)+' +
      '@(Get-NetFirewallRule -DisplayName $legacy -EA SilentlyContinue))|' +
      'Sort-Object Name -Unique;' +
    'foreach($r in $candidates){try{' +
      '$a=$r|Get-NetFirewallApplicationFilter -EA Stop;' +
      '$q=$r|Get-NetFirewallPortFilter -EA Stop;' +
      '$d=$r|Get-NetFirewallAddressFilter -EA Stop;' +
      'if(($r.Direction-eq ''Inbound'')-and($r.Action-eq ''Allow'')-and' +
      '($r.Enabled-eq ''True'')-and' +
      '([IO.Path]::IsPathFullyQualified($a.Program))-and' +
      '([IO.Path]::GetFileName($a.Program)-ieq ''RestreamStudio.exe'')-and' +
      '($q.Protocol-eq ''TCP'')-and(@($d.LocalAddress).Count-eq 1)-and' +
      '(@($d.LocalAddress)[0]-eq ''127.0.0.1'')-and' +
      '(@($d.RemoteAddress).Count-eq 1)-and' +
      '(@($d.RemoteAddress)[0]-eq ''127.0.0.1'')){' +
        '$restoreProgram=$a.Program;break}}catch{}};' +
    'try{' +
      'Get-NetFirewallRule -DisplayName $legacy -EA SilentlyContinue|' +
        'Where-Object{$_.Name-ne $n}|Remove-NetFirewallRule -EA Stop;' +
      'Get-NetFirewallRule -Name $n -EA SilentlyContinue|' +
        'Remove-NetFirewallRule -EA Stop;' +
      'New-NetFirewallRule -Name $n -DisplayName ''Restream Studio Installed localhost''' +
        ' -Direction Inbound -Action Allow -Enabled True -Program $p' +
        ' -Protocol TCP -LocalAddress ''127.0.0.1''' +
        ' -RemoteAddress ''127.0.0.1'' -Profile Any' +
        ' -EdgeTraversalPolicy Block|Out-Null;' +
      '$rs=@(Get-NetFirewallRule -Name $n -EA Stop);' +
      'if($rs.Count-ne 1){throw ''bad count''};$r=$rs[0];' +
      '$a=$r|Get-NetFirewallApplicationFilter;$q=$r|Get-NetFirewallPortFilter;' +
      '$d=$r|Get-NetFirewallAddressFilter;' +
      'if(($r.Direction-ne ''Inbound'')-or($r.Action-ne ''Allow'')-or' +
      '($r.Enabled-ne ''True'')-or($a.Program-ine $p)-or' +
      '($q.Protocol-ne ''TCP'')-or(@($d.LocalAddress).Count-ne 1)-or' +
      '(@($d.LocalAddress)[0]-ne ''127.0.0.1'')-or' +
      '(@($d.RemoteAddress).Count-ne 1)-or' +
      '(@($d.RemoteAddress)[0]-ne ''127.0.0.1'')){throw ''bad scope''}' +
    '}catch{' +
      'Get-NetFirewallRule -Name $n -EA SilentlyContinue|Remove-NetFirewallRule;' +
      'if($null-ne $restoreProgram){New-NetFirewallRule -Name $n' +
        ' -DisplayName ''Restream Studio Installed localhost''' +
        ' -Direction Inbound -Action Allow -Enabled True -Program $restoreProgram' +
        ' -Protocol TCP -LocalAddress ''127.0.0.1''' +
        ' -RemoteAddress ''127.0.0.1'' -Profile Any' +
        ' -EdgeTraversalPolicy Block|Out-Null};throw}';
end;

function RemoveInstalledFirewallCommand(): String;
begin
  Result :=
    '$ErrorActionPreference=''Stop'';' +
    '$h=$args[0];if(($h.Length%4)-ne 0){throw ''bad path''};' +
    '$p='''';for($i=0;$i-lt $h.Length;$i+=4){' +
      '$p+=[char][Convert]::ToUInt16($h.Substring($i,4),16)};' +
    'if([IO.Path]::GetFileName($p)-ine ''RestreamStudio.exe''){throw ''bad path''};' +
    '$n=''RestreamStudio-Installed-Localhost'';' +
    '$legacy=''Restream Studio (Loopback TCP)'';' +
    '$candidates=@(@(Get-NetFirewallRule -Name $n -EA SilentlyContinue)+' +
      '@(Get-NetFirewallRule -DisplayName $legacy -EA SilentlyContinue))|' +
    'Sort-Object Name -Unique;' +
    'foreach($r in $candidates){try{' +
      '$a=$r|Get-NetFirewallApplicationFilter -EA Stop;' +
      'if($a.Program -ieq $p){$r|Remove-NetFirewallRule -EA Stop}}catch{continue}};' +
    '$remaining=@(@(Get-NetFirewallRule -Name $n -EA SilentlyContinue)+' +
      '@(Get-NetFirewallRule -DisplayName $legacy -EA SilentlyContinue))|' +
      'Sort-Object Name -Unique;' +
    'foreach($r in $remaining){$a=$null;try{' +
      '$a=$r|Get-NetFirewallApplicationFilter -EA Stop}catch{continue};' +
      'if($a.Program -ieq $p){throw ''firewall removal verification failed''}}';
end;

procedure ConfigureInstalledFirewall;
var
  ResultCode: Integer;
  ApplicationPath: String;
begin
  ResultCode := -1;
  ApplicationPath := ExpandConstant('{app}\RestreamStudio.exe');
  if (not RunElevatedPowerShell(InstalledFirewallCommand(), ApplicationPath,
    ResultCode)) or (ResultCode <> 0) then
    RaiseException('Firewall configuration failed; installation was rolled back.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    ConfigureInstalledFirewall;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  ApplicationPath: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    ResultCode := -1;
    ApplicationPath := ExpandConstant('{app}\RestreamStudio.exe');
    if (not RunElevatedPowerShell(RemoveInstalledFirewallCommand(),
      ApplicationPath, ResultCode)) or (ResultCode <> 0) then
    begin
      SuppressibleMsgBox(
        'Firewall cleanup failed. Uninstall was stopped before deleting files.',
        mbError, MB_OK, IDOK);
      Abort;
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
