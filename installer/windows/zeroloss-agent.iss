; ZeroLoss Agent installer: a Windows sender or receiver PC (docs/06-install.md 4.2, 11.4).
; Built by ``python -m tools.build installer-agent --app <the built node folder>
; --tools <folder with TSDuck\, ffmpeg\, mpv\>``, which passes:
;   AppVer   the build, e.g. 20261121.1800       FileVer  its Windows 4-part form
;   Src      the node folder (agent\, deployer\, desktop\, modules\)
;   Tools    TSDuck\, ffmpeg\, mpv\ (installed apart from ZeroLoss: ffmpeg and mpv are GPL)
;   OutDir   where the setup file goes
;
; 2026-11-21 user ("윈도우즈 sender,receiver도 원클릭 인스톨되게 해주고", the tools in the
; kit): one click, no questions. Two services that Windows starts at boot and restarts
; when they fail: ZeroLossAgent (the node, TCP 10992) and ZeroLossDeployer (TCP 10991,
; updates from the Controller); Agent Desktop at every login (the player's window: a
; service cannot show one, D38). Data (C:\ProgramData\ZeroLoss\Agent) stays unless the
; person says to remove it (06 1 5). A running test is never cut (06 1 7).

#ifndef AppVer
  #error AppVer is required
#endif

[Setup]
AppId={{3F7B2A64-1C9E-4B7D-A0F2-8E5C6D1B9A47}
AppName=ZeroLoss Agent
AppVersion={#AppVer}
AppVerName=ZeroLoss Agent {#AppVer}
VersionInfoVersion={#FileVer}
AppPublisher=ZeroLoss
DefaultDirName={autopf}\ZeroLoss\Agent
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutDir}
OutputBaseFilename=ZeroLoss-Agent-Setup-{#AppVer}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=ZeroLoss Agent
UninstallDisplayIcon={app}\desktop\zeroloss-agent-desktop.exe
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "ko"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ko.Running=이 PC에서 시험이 진행 중입니다. 시험이 끝난 뒤 다시 실행하세요. 시험은 그대로 계속됩니다.
en.Running=A test is running on this PC. Run this again after the test ends. The test goes on.
ko.IsController=이 PC에는 ZeroLoss Controller가 설치되어 있습니다. Controller PC는 그 자체로 송신·수신 PC가 될 수 있어 Agent가 필요 없습니다(같은 포트 10992를 씁니다). 송신·수신 PC에서 실행하세요.
en.IsController=ZeroLoss Controller is installed on this PC. The Controller PC can already be a sender or receiver by itself, so it needs no Agent (both use port 10992). Run this on the sender or receiver PC.
ko.KeepData=시험 기록과 설정(C:\ProgramData\ZeroLoss\Agent)도 지울까요?%n%n[아니요]를 누르면 그대로 남습니다 (다시 설치하면 이어서 씁니다).
en.KeepData=Also delete the test records and settings (C:\ProgramData\ZeroLoss\Agent)?%n%nNo keeps them (a new install goes on using them).
ko.Launch=ZeroLoss Agent Desktop 켜기
en.Launch=Start ZeroLoss Agent Desktop

[InstallDelete]
; the last build's program folders, so nothing of it stays beside the new one (06 0)
Type: filesandordirs; Name: "{app}\agent"
Type: filesandordirs; Name: "{app}\deployer"
Type: filesandordirs; Name: "{app}\desktop"
Type: filesandordirs; Name: "{app}\modules"
Type: filesandordirs; Name: "{autopf}\ZeroLoss\Tools"

[Dirs]
Name: "{commonappdata}\ZeroLoss\Agent"

[Files]
Source: "{#Src}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; the tools, each in its own folder apart from ZeroLoss (core/blocks/tool_dirs.py)
Source: "{#Tools}\TSDuck\*"; DestDir: "{autopf}\ZeroLoss\Tools\TSDuck"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Tools}\ffmpeg\*"; DestDir: "{autopf}\ZeroLoss\Tools\ffmpeg"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Tools}\mpv\*"; DestDir: "{autopf}\ZeroLoss\Tools\mpv"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; 06 4.2: version, role and install path only
Root: HKLM; Subkey: "Software\ZeroLoss\Agent"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVer}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\ZeroLoss\Agent"; ValueType: string; ValueName: "Role"; ValueData: "sender+receiver"
Root: HKLM; Subkey: "Software\ZeroLoss\Agent"; ValueType: string; ValueName: "Path"; ValueData: "{app}"
; Agent Desktop at every login (the player's window and the small status windows, F20)
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ZeroLoss Agent Desktop"; ValueData: """{app}\desktop\zeroloss-agent-desktop.exe"" --modules ""{app}\modules"""; Flags: uninsdeletevalue

[Icons]
Name: "{autoprograms}\ZeroLoss Agent Desktop"; Filename: "{app}\desktop\zeroloss-agent-desktop.exe"; Parameters: "--modules ""{app}\modules"""

[Run]
; inbound rules for this program's own files only, by exact name, the local subnet only
; (06 4.2: before pairing, the same subnet; the router WAN is the sender's subnet, 07 3.3)
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Agent"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Deployer"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss player"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ZeroLoss Agent"" dir=in action=allow program=""{app}\agent\zeroloss.exe"" remoteip=localsubnet enable=yes profile=any"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ZeroLoss Deployer"" dir=in action=allow program=""{app}\deployer\zeroloss-deployer.exe"" remoteip=localsubnet enable=yes profile=any"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ZeroLoss player"" dir=in action=allow program=""{autopf}\ZeroLoss\Tools\mpv\mpv.exe"" remoteip=localsubnet enable=yes profile=any"; Flags: runhidden
Filename: "{app}\desktop\zeroloss-agent-desktop.exe"; Parameters: "--modules ""{app}\modules"""; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Agent"""; Flags: runhidden; RunOnceId: "fw1"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Deployer"""; Flags: runhidden; RunOnceId: "fw2"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss player"""; Flags: runhidden; RunOnceId: "fw3"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\modules"
Type: filesandordirs; Name: "{autopf}\ZeroLoss\Tools"
Type: dirifempty; Name: "{app}"
Type: dirifempty; Name: "{autopf}\ZeroLoss"

[Code]
const
  Health = 'http://127.0.0.1:10992/v1/health';
  ControllerKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7C1F6C2E-5A0B-4E57-9B4E-2E1D0C5A9F31}_is1';

function Get(const Url: String; var Text: String): Boolean;
var
  Req: Variant;
begin
  Result := False;
  try
    Req := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Req.SetTimeouts(2000, 2000, 3000, 3000);
    Req.Open('GET', Url, False);
    Req.Send('');
    Text := Req.ResponseText;
    Result := True;
  except
  end;
end;

procedure Sc(const Args: String);
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), Args, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ Stop and remove the services (an update, or the uninstall); the node's Job Object ends
  its players and senders with it (12 4-3). }
procedure RemoveServices();
var
  Code: Integer;
begin
  Sc('stop ZeroLossDeployer');
  Sc('stop ZeroLossAgent');
  Sleep(3000);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM zeroloss-agent-desktop.exe', '', SW_HIDE,
       ewWaitUntilTerminated, Code);
  Sc('delete ZeroLossDeployer');
  Sc('delete ZeroLossAgent');
end;

{ One service: its program line, start at boot, restart 2 s after a failure (06 4.2). }
procedure AddService(const Name, Display, Line: String);
begin
  Sc('create ' + Name + ' binPath= "' + Line + '" start= auto DisplayName= "' + Display + '"');
  Sc('failure ' + Name + ' reset= 60 actions= restart/2000/restart/2000/restart/2000');
  Sc('start ' + Name);
end;

function Quoted(const S: String): String;
begin
  Result := '\"' + S + '\"';
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if RegKeyExists(HKLM, ControllerKey) then
  begin
    SuppressibleMsgBox(CustomMessage('IsController'), mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Text: String;
begin
  Result := '';
  if Get(Health, Text) and (Pos('"state":"IDLE"', Text) = 0) and (Pos('"state"', Text) > 0) then
  begin
    Result := CustomMessage('Running');
    Exit;
  end;
  RemoveServices();
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  App, Data, Modules: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  App := ExpandConstant('{app}');
  Data := ExpandConstant('{commonappdata}\ZeroLoss\Agent');
  Modules := App + '\modules';
  AddService('ZeroLossAgent', 'ZeroLoss Agent',
    Quoted(App + '\agent\zeroloss.exe') + ' serve --role agent --data ' + Quoted(Data + '\data')
    + ' --modules ' + Quoted(Modules) + ' --player-host desktop --windows-service ZeroLossAgent');
  AddService('ZeroLossDeployer', 'ZeroLoss Deployer',
    Quoted(App + '\deployer\zeroloss-deployer.exe') + ' --data ' + Quoted(Data + '\data')
    + ' --modules ' + Quoted(Modules) + ' --port 10991 --agent-port 10992'
    + ' --service windows:ZeroLossAgent --windows-service ZeroLossDeployer');
end;

function InitializeUninstall(): Boolean;
var
  Text: String;
begin
  Result := True;
  if Get(Health, Text) and (Pos('"state":"IDLE"', Text) = 0) and (Pos('"state"', Text) > 0) then
  begin
    SuppressibleMsgBox(CustomMessage('Running'), mbError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;
  RemoveServices();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  { default No: results are the person's (06 1 5) }
  if SuppressibleMsgBox(CustomMessage('KeepData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2,
                        IDNO) = IDYES then
  begin
    DelTree(ExpandConstant('{commonappdata}\ZeroLoss\Agent'), True, True, True);
    RemoveDir(ExpandConstant('{commonappdata}\ZeroLoss'));
  end;
end;
