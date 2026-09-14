#define MyAppName "Restream Studio"
#ifndef MyAppVersion
#define MyAppVersion "0.1.4"
#endif
#ifndef SmokeTestBuild
#define SmokeTestBuild 0
#endif
#ifndef MyDistributionDir
#define MyDistributionDir "..\dist\RestreamStudio"
#endif
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
Source: "{#MyDistributionDir}\RestreamStudio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyDistributionDir}\*"; DestDir: "{app}"; Excludes: "RestreamStudio.exe,RestreamStudioUpdateHelper.exe"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyDistributionDir}\RestreamStudioUpdateHelper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "firewall-install.ps1"; DestDir: "{app}\packaging"; Flags: ignoreversion
Source: "firewall-remove.ps1"; DestDir: "{app}\packaging"; Flags: ignoreversion; AfterInstall: ConfigureFirewall

[Icons]
Name: "{autodesktop}\Restream Studio"; Filename: "{app}\RestreamStudio.exe"
Name: "{group}\Restream Studio"; Filename: "{app}\RestreamStudio.exe"

[UninstallDelete]
Type: filesandordirs; Name: "{code:GetUserDataDir}"; Check: ShouldDeleteUserData

[Code]
const
  MY_FILE_ATTRIBUTE_REPARSE_POINT = $400;
  MY_INVALID_FILE_ATTRIBUTES = $FFFFFFFF;

var
  DeleteDataDecisionMade: Boolean;
  DeleteUserData: Boolean;
  LaunchAfterInstallCheck: TNewCheckBox;
  FirewallChanged: Boolean;
  InstallCommitted: Boolean;
  PreviousFirewallProgram: String;
  UserDataDir: String;
  DefaultUserDataDir: String;

function GetCommandLineValue(const Prefix: String; var Value: String): Boolean;
var
  Index: Integer;
  Argument: String;
begin
  Result := False;
  Value := '';
  for Index := 1 to ParamCount do
  begin
    Argument := ParamStr(Index);
    if CompareText(Copy(Argument, 1, Length(Prefix)), Prefix) = 0 then
    begin
      Value := Copy(Argument, Length(Prefix) + 1, MaxInt);
      Result := True;
      Exit;
    end;
  end;
end;

function IsAbsoluteDrivePath(const Value: String): Boolean;
begin
  Result := (Length(Value) >= 3) and
    (((Value[1] >= 'A') and (Value[1] <= 'Z')) or
     ((Value[1] >= 'a') and (Value[1] <= 'z'))) and
    (Value[2] = ':') and ((Value[3] = '\') or (Value[3] = '/'));
end;

function IsPathInside(const Value, Root: String): Boolean;
var
  FullValue: String;
  FullRoot: String;
begin
  FullValue := RemoveBackslashUnlessRoot(ExpandFileName(Value));
  FullRoot := RemoveBackslashUnlessRoot(ExpandFileName(Root));
  Result := (Length(FullValue) > Length(FullRoot)) and
    (CompareText(Copy(FullValue, 1, Length(FullRoot)), FullRoot) = 0) and
    (FullValue[Length(FullRoot) + 1] = '\');
end;

function GetFileAttributesW(lpFileName: String): LongWord;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function HasReparsePointAncestor(const Value: String): Boolean;
var
  CurrentPath: String;
  ParentPath: String;
  Attributes: LongWord;
begin
  Result := False;
  CurrentPath := RemoveBackslashUnlessRoot(ExpandFileName(Value));
  while CurrentPath <> '' do begin
    Attributes := GetFileAttributesW(CurrentPath);
    if (Attributes <> MY_INVALID_FILE_ATTRIBUTES) and
       ((Attributes and MY_FILE_ATTRIBUTE_REPARSE_POINT) <> 0) then begin
      Result := True;
      Exit;
    end;
    ParentPath := ExtractFileDir(CurrentPath);
    if CompareText(ParentPath, CurrentPath) = 0 then Exit;
    CurrentPath := ParentPath;
  end;
end;

function IsTempSmokePath(const Value: String): Boolean;
var
  TempRoot: String;
  RelativeValue: String;
  Separator: Integer;
  FirstDirectory: String;
begin
  Result := False;
  TempRoot := RemoveBackslashUnlessRoot(ExpandConstant('{%TEMP}'));
  if not IsPathInside(Value, TempRoot) then
    Exit;
  RelativeValue := Copy(ExpandFileName(Value), Length(TempRoot) + 2, MaxInt);
  Separator := Pos('\', RelativeValue);
  if Separator = 0 then
    FirstDirectory := RelativeValue
  else
    FirstDirectory := Copy(RelativeValue, 1, Separator - 1);
  Result := Pos('RestreamStudioInstallerSmoke-', FirstDirectory) = 1;
end;

function IsWorkspaceSmokePath(const Value: String): Boolean;
var
  CandidateRoot: String;
  WorkspaceRoot: String;
  ParentRoot: String;
  Attempt: Integer;
begin
  Result := False;
  WorkspaceRoot := ExtractFileDir(ExpandConstant('{srcexe}'));
  for Attempt := 1 to 8 do
  begin
    CandidateRoot := AddBackslash(WorkspaceRoot) + 'artifacts\smoke';
    if (FileExists(AddBackslash(WorkspaceRoot) + '.git') or
        DirExists(AddBackslash(WorkspaceRoot) + '.git')) and
       IsPathInside(Value, CandidateRoot) then
    begin
      Result := True;
      Exit;
    end;
    ParentRoot := ExtractFileDir(WorkspaceRoot);
    if CompareText(ParentRoot, WorkspaceRoot) = 0 then
      Exit;
    WorkspaceRoot := ParentRoot;
  end;
end;

function IsSafeSmokeUserDataDir(const Value: String): Boolean;
begin
  Result := (Value <> '') and IsAbsoluteDrivePath(Value) and
    (IsTempSmokePath(Value) or IsWorkspaceSmokePath(Value));
end;

function InitializeUserDataDir(): Boolean;
var
  OverridePath: String;
begin
  DefaultUserDataDir := ExpandConstant('{localappdata}\RestreamStudio');
  UserDataDir := DefaultUserDataDir;
  if GetCommandLineValue('/USERDATADIR=', OverridePath) then
  begin
    if not IsSafeSmokeUserDataDir(OverridePath) then
    begin
      SuppressibleMsgBox('Refusing unsafe /USERDATADIR override.',
        mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
    if HasReparsePointAncestor(OverridePath) then
    begin
      SuppressibleMsgBox('Refusing /USERDATADIR with a reparse-point ancestor.',
        mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
    UserDataDir := RemoveBackslashUnlessRoot(ExpandFileName(OverridePath));
  end;
  Result := True;
end;

#if SmokeTestBuild
function SmokeFirewallFailureMode(): String;
var
  Value: String;
begin
  Result := '';
  if GetCommandLineValue('/SMOKEFIREWALLFAIL=', Value) then
  begin
    if (CompareText(Value, 'cancel') <> 0) and
       (CompareText(Value, 'command') <> 0) then
      RaiseException('Invalid /SMOKEFIREWALLFAIL test mode.');
    Result := Lowercase(Value);
  end;
end;
#endif

function InitializeSetup(): Boolean;
begin
  Result := InitializeUserDataDir();
end;

function InitializeUninstall(): Boolean;
begin
  Result := InitializeUserDataDir();
end;

function GetUserDataDir(Param: String): String;
begin
  Result := UserDataDir;
end;

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

function HexValue(const Value: Char): Integer;
begin
  if (Value >= '0') and (Value <= '9') then
    Result := Ord(Value) - Ord('0')
  else if (Value >= 'a') and (Value <= 'f') then
    Result := Ord(Value) - Ord('a') + 10
  else if (Value >= 'A') and (Value <= 'F') then
    Result := Ord(Value) - Ord('A') + 10
  else
    Result := -1;
end;

function HexDecode(const Value: String; var Decoded: String): Boolean;
var
  Index: Integer;
  A, B, C, D: Integer;
begin
  Result := False;
  Decoded := '';
  if (Length(Value) mod 4) <> 0 then
    Exit;
  Index := 1;
  while Index <= Length(Value) do
  begin
    A := HexValue(Value[Index]);
    B := HexValue(Value[Index + 1]);
    C := HexValue(Value[Index + 2]);
    D := HexValue(Value[Index + 3]);
    if (A < 0) or (B < 0) or (C < 0) or (D < 0) then
      Exit;
    Decoded := Decoded + Chr((A shl 12) or (B shl 8) or (C shl 4) or D);
    Index := Index + 4;
  end;
  Result := True;
end;

function IsFullyQualifiedApplicationPath(const Value: String): Boolean;
var
  Index: Integer;
  SeparatorCount: Integer;
  DrivePath: Boolean;
  UncPath: Boolean;
begin
  SeparatorCount := 0;
  for Index := 1 to Length(Value) do
    if (Value[Index] = '\') or (Value[Index] = '/') then
      SeparatorCount := SeparatorCount + 1;
  DrivePath := (Length(Value) >= 3) and
    (((Value[1] >= 'A') and (Value[1] <= 'Z')) or
     ((Value[1] >= 'a') and (Value[1] <= 'z'))) and
    (Value[2] = ':') and ((Value[3] = '\') or (Value[3] = '/'));
  UncPath := (Length(Value) >= 5) and
    ((Value[1] = '\') or (Value[1] = '/')) and
    ((Value[2] = '\') or (Value[2] = '/')) and (SeparatorCount >= 4);
  Result := (DrivePath or UncPath) and
    (CompareText(ExtractFileName(Value), 'RestreamStudio.exe') = 0);
end;

function NewFirewallStatePath(): String;
var
  Attempt: Integer;
begin
  for Attempt := 1 to 32 do
  begin
    Result := AddBackslash(ExpandConstant('{tmp}')) +
      'RestreamStudio-firewall-' + IntToStr(Random(1000000000)) + '-' +
      IntToStr(Random(1000000000)) + '.state';
    if (not FileExists(Result)) and (not DirExists(Result)) then
      Exit;
  end;
  RaiseException('Unable to allocate a unique firewall state path.');
end;

function RunSnapshotPowerShell(const Command, EncodedArguments: String;
  var ResultCode: Integer): Boolean;
var
  Parameters: String;
begin
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "& { ' +
    Command + ' }" ' + EncodedArguments;
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function RunElevatedPowerShell(const Command, EncodedArguments: String;
  var ResultCode: Integer): Boolean;
var
  Parameters: String;
begin
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "& { ' +
    Command + ' }" ' + EncodedArguments;
  Result := ShellExec('runas',
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parameters,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function SnapshotFirewallCommand(): String;
begin
  Result :=
    '$ErrorActionPreference=''Stop'';' +
    '$sh=$args[0];if(($sh.Length%4)-ne 0){throw ''bad state path''};' +
    '$s='''';for($i=0;$i-lt $sh.Length;$i+=4){' +
      '$s+=[char][Convert]::ToUInt16($sh.Substring($i,4),16)};' +
    '$isFull={param($v)(($v-match ''^[A-Za-z]:[\\/]'' )-or' +
      '($v-match ''^[\\/]{2}[^\\/]+[\\/][^\\/]+[\\/]''))};' +
    'if(-not (&$isFull $s)){throw ''bad state path''};' +
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
      '(&$isFull $a.Program)-and' +
      '([IO.Path]::GetFileName($a.Program)-ieq ''RestreamStudio.exe'')-and' +
      '($q.Protocol-eq ''TCP'')-and(@($d.LocalAddress).Count-eq 1)-and' +
      '(@($d.LocalAddress)[0]-eq ''127.0.0.1'')-and' +
      '(@($d.RemoteAddress).Count-eq 1)-and' +
      '(@($d.RemoteAddress)[0]-eq ''127.0.0.1'')){' +
        '$restoreProgram=$a.Program;break}}catch{}};' +
    '$snapshot='''';if($null-ne $restoreProgram){' +
      'foreach($c in [char[]]$restoreProgram){$snapshot+=([int]$c).ToString(''x4'')}};' +
    '[IO.File]::WriteAllText($s,$snapshot,[Text.Encoding]::ASCII)';
end;

function InstalledFirewallCommand(): String;
begin
  Result :=
    '$ErrorActionPreference=''Stop'';' +
    '$h=$args[0];if(($h.Length%4)-ne 0){throw ''bad path''};' +
    '$p='''';for($i=0;$i-lt $h.Length;$i+=4){' +
      '$p+=[char][Convert]::ToUInt16($h.Substring($i,4),16)};' +
    '$isFull={param($v)(($v-match ''^[A-Za-z]:[\\/]'' )-or' +
      '($v-match ''^[\\/]{2}[^\\/]+[\\/][^\\/]+[\\/]''))};' +
    'if((-not (&$isFull $p))-or' +
      '([IO.Path]::GetFileName($p)-ine ''RestreamStudio.exe'')){throw ''bad path''};' +
    '$n=''RestreamStudio-Installed-Localhost'';' +
    '$legacy=''Restream Studio (Loopback TCP)'';' +
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
      'foreach($r in @(Get-NetFirewallRule -Name $n -EA SilentlyContinue)){' +
        'try{$a=$r|Get-NetFirewallApplicationFilter -EA Stop;' +
        'if($a.Program-ieq $p){$r|Remove-NetFirewallRule -EA SilentlyContinue}}catch{}};' +
      'throw}';
end;

function RemoveInstalledFirewallCommand(): String;
begin
  Result :=
    '$ErrorActionPreference=''Stop'';' +
    '$h=$args[0];if(($h.Length%4)-ne 0){throw ''bad path''};' +
    '$p='''';for($i=0;$i-lt $h.Length;$i+=4){' +
      '$p+=[char][Convert]::ToUInt16($h.Substring($i,4),16)};' +
    '$isFull={param($v)(($v-match ''^[A-Za-z]:[\\/]'' )-or' +
      '($v-match ''^[\\/]{2}[^\\/]+[\\/][^\\/]+[\\/]''))};' +
    'if((-not (&$isFull $p))-or' +
      '([IO.Path]::GetFileName($p)-ine ''RestreamStudio.exe'')){throw ''bad path''};' +
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

function RollbackFirewallCommand(): String;
begin
  Result :=
    '$ErrorActionPreference=''Stop'';' +
    '$h=$args[0];$oh=$args[1];' +
    'if((($h.Length%4)-ne 0)-or(($oh.Length%4)-ne 0)){throw ''bad path''};' +
    '$p='''';for($i=0;$i-lt $h.Length;$i+=4){' +
      '$p+=[char][Convert]::ToUInt16($h.Substring($i,4),16)};' +
    '$old='''';for($i=0;$i-lt $oh.Length;$i+=4){' +
      '$old+=[char][Convert]::ToUInt16($oh.Substring($i,4),16)};' +
    '$isFull={param($v)(($v-match ''^[A-Za-z]:[\\/]'' )-or' +
      '($v-match ''^[\\/]{2}[^\\/]+[\\/][^\\/]+[\\/]''))};' +
    'if((-not (&$isFull $p))-or' +
      '([IO.Path]::GetFileName($p)-ine ''RestreamStudio.exe'')){throw ''bad path''};' +
    'if(($old-ne '''')-and((-not (&$isFull $old))-or' +
      '([IO.Path]::GetFileName($old)-ine ''RestreamStudio.exe''))){throw ''bad old path''};' +
    '$n=''RestreamStudio-Installed-Localhost'';' +
    '$legacy=''Restream Studio (Loopback TCP)'';' +
    '$candidates=@(@(Get-NetFirewallRule -Name $n -EA SilentlyContinue)+' +
      '@(Get-NetFirewallRule -DisplayName $legacy -EA SilentlyContinue))|' +
      'Sort-Object Name -Unique;' +
    'foreach($r in $candidates){try{' +
      '$a=$r|Get-NetFirewallApplicationFilter -EA Stop;' +
      'if($a.Program -ieq $p){$r|Remove-NetFirewallRule -EA Stop}}catch{continue}};' +
    'if($old-ne ''''){' +
      '$valid=$false;foreach($r in @(Get-NetFirewallRule -Name $n -EA SilentlyContinue)){' +
        'try{$a=$r|Get-NetFirewallApplicationFilter -EA Stop;' +
        '$q=$r|Get-NetFirewallPortFilter -EA Stop;' +
        '$d=$r|Get-NetFirewallAddressFilter -EA Stop;' +
        'if(($r.Direction-eq ''Inbound'')-and($r.Action-eq ''Allow'')-and' +
        '($r.Enabled-eq ''True'')-and($a.Program-ieq $old)-and' +
        '($q.Protocol-eq ''TCP'')-and(@($d.LocalAddress).Count-eq 1)-and' +
        '(@($d.LocalAddress)[0]-eq ''127.0.0.1'')-and' +
        '(@($d.RemoteAddress).Count-eq 1)-and' +
        '(@($d.RemoteAddress)[0]-eq ''127.0.0.1'')){$valid=$true;break}}catch{}};' +
      'if(-not $valid){New-NetFirewallRule -Name $n' +
        ' -DisplayName ''Restream Studio Installed localhost''' +
        ' -Direction Inbound -Action Allow -Enabled True -Program $old' +
        ' -Protocol TCP -LocalAddress ''127.0.0.1''' +
        ' -RemoteAddress ''127.0.0.1'' -Profile Any' +
        ' -EdgeTraversalPolicy Block|Out-Null}};' +
    '$remaining=@(@(Get-NetFirewallRule -Name $n -EA SilentlyContinue)+' +
      '@(Get-NetFirewallRule -DisplayName $legacy -EA SilentlyContinue))|' +
      'Sort-Object Name -Unique;' +
    'foreach($r in $remaining){try{' +
      '$a=$r|Get-NetFirewallApplicationFilter -EA Stop;' +
      'if($a.Program-ieq $p){throw ''current firewall rule remains''}}catch{' +
      'if($_.Exception.Message-eq ''current firewall rule remains''){throw}}}';
end;

procedure ConfigureFirewall;
var
  ResultCode: Integer;
  RollbackResultCode: Integer;
  ApplicationPath: String;
  StatePath: String;
  SnapshotHex: AnsiString;
  SnapshotProgram: String;
  Started: Boolean;
  SnapshotValid: Boolean;
#if SmokeTestBuild
  FailureMode: String;
#endif
begin
  ResultCode := -1;
  ApplicationPath := ExpandConstant('{app}\RestreamStudio.exe');
  StatePath := NewFirewallStatePath();
  SnapshotValid := False;
  try
    Started := RunSnapshotPowerShell(SnapshotFirewallCommand(),
      HexEncode(StatePath), ResultCode);
    if FileExists(StatePath) then
    begin
      if not LoadStringFromFile(StatePath, SnapshotHex) then
        RaiseException('Unable to read the firewall state file.');
      if (not HexDecode(String(SnapshotHex), SnapshotProgram)) or
        ((SnapshotProgram <> '') and
         (not IsFullyQualifiedApplicationPath(SnapshotProgram))) then
        RaiseException('The firewall state file is invalid.');
      PreviousFirewallProgram := SnapshotProgram;
      SnapshotValid := True;
      if not DeleteFile(StatePath) then
        RaiseException('Unable to delete the firewall state file.');
      StatePath := '';
    end;
  finally
    if (StatePath <> '') and FileExists(StatePath) then
      DeleteFile(StatePath);
  end;
  if (not Started) or (ResultCode <> 0) or (not SnapshotValid) then
    RaiseException('Firewall state capture failed; installation was rolled back.');

  ResultCode := -1;
#if SmokeTestBuild
  FailureMode := SmokeFirewallFailureMode();
  if CompareText(FailureMode, 'cancel') = 0 then
  begin
    Started := False;
    ResultCode := 1223;
  end
  else
#endif
  Started := RunElevatedPowerShell(InstalledFirewallCommand(),
    HexEncode(ApplicationPath), ResultCode);
#if SmokeTestBuild
  if (CompareText(FailureMode, 'command') = 0) and Started and
     (ResultCode = 0) then
    ResultCode := 91;
#endif
  if Started then
    FirewallChanged := True;
  if (not Started) or (ResultCode <> 0) then
  begin
    if FirewallChanged then
    begin
      RollbackResultCode := -1;
      if RunElevatedPowerShell(RollbackFirewallCommand(),
        HexEncode(ApplicationPath) + ' ' + HexEncode(PreviousFirewallProgram),
        RollbackResultCode) and (RollbackResultCode = 0) then
        FirewallChanged := False
      else
        Log(Format('Immediate firewall rollback failed with result code %d.', [RollbackResultCode]));
    end;
    RaiseException('Firewall configuration failed; installation was rolled back.');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then
    InstallCommitted := True;
end;

procedure DeinitializeSetup;
var
  ResultCode: Integer;
  ApplicationPath: String;
begin
  if FirewallChanged and (not InstallCommitted) then
  begin
    ResultCode := -1;
    ApplicationPath := ExpandConstant('{app}\RestreamStudio.exe');
    if (not RunElevatedPowerShell(RollbackFirewallCommand(),
      HexEncode(ApplicationPath) + ' ' + HexEncode(PreviousFirewallProgram),
      ResultCode)) or (ResultCode <> 0) then
      Log(Format('Firewall rollback failed with result code %d.', [ResultCode]));
  end;
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
      HexEncode(ApplicationPath), ResultCode)) or (ResultCode <> 0) then
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
    DataPath := UserDataDir;
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
