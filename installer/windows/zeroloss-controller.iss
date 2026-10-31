; ZeroLoss Controller installer (docs/06-install.md 3, 4.1, 7). Built by
; ``python -m tools.build installer --app <the built Controller folder>``, which passes:
;   AppVer   the build, e.g. 20261031.0048       FileVer  its Windows 4-part form
;   Src      the Controller folder (ZeroLoss.exe, cli\, modules\, packages\)
;   Guide    the install and use guide (.html)   Register the Linux PC helper (.bat)
;   OutDir   where the setup file goes           Tools    (optional) packages\tools files
;
; 2026-10-31 user ("설치가이드. Offline kit. Installer . 사용법"): one click, no questions.
; Program files go to Program Files; data (C:\ProgramData\ZeroLoss\Controller) and reports
; (C:\ZeroLoss\Reports) are the user's and stay unless the person says to remove them
; (06 1 5). A running test is never cut: install and uninstall both refuse (06 1 7).

#ifndef AppVer
  #error AppVer is required
#endif
#ifndef CtlPort
  #define CtlPort "10994"  ; another only to check this setup beside a running Controller
#endif

[Setup]
AppId={{7C1F6C2E-5A0B-4E57-9B4E-2E1D0C5A9F31}
AppName=ZeroLoss Controller
AppVersion={#AppVer}
AppVerName=ZeroLoss Controller {#AppVer}
VersionInfoVersion={#FileVer}
AppPublisher=ZeroLoss
DefaultDirName={autopf}\ZeroLoss\Controller
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=yes
PrivilegesRequired=admin
; /CURRENTUSER only for a check of the setup itself (no firewall rules then)
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutDir}
OutputBaseFilename=ZeroLoss-Controller-Setup-{#AppVer}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=ZeroLoss Controller
UninstallDisplayIcon={app}\ZeroLoss.exe
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no

[Languages]
Name: "ko"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ko.Running=시험이 진행 중입니다. 시험이 끝난 뒤(또는 ZeroLoss 화면에서 [Stop]을 누른 뒤) 다시 실행하세요. 시험은 그대로 계속됩니다.
en.Running=A test is running. Run this again after the test ends (or after [Stop] in ZeroLoss). The test goes on.
ko.NotEnded=ZeroLoss 프로그램이 끝나지 않았습니다. ZeroLoss 창을 닫고 다시 실행하세요.
en.NotEnded=The ZeroLoss program did not end. Close the ZeroLoss window and run this again.
ko.KeepData=시험 보고서(C:\ZeroLoss\Reports)와 설정(C:\ProgramData\ZeroLoss\Controller)도 지울까요?%n%n[아니요]를 누르면 그대로 남습니다 (다시 설치하면 이어서 씁니다).
en.KeepData=Also delete the test reports (C:\ZeroLoss\Reports) and settings (C:\ProgramData\ZeroLoss\Controller)?%n%nNo keeps them (a new install goes on using them).
ko.Launch=ZeroLoss 켜기
en.Launch=Start ZeroLoss
ko.Guide=ZeroLoss 사용 안내
en.Guide=ZeroLoss guide
ko.Register=ZeroLoss 리눅스 PC 등록
en.Register=ZeroLoss - register a Linux PC

[InstallDelete]
; the last build's program folders, so nothing of it stays beside the new one (06 0)
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\cli"
Type: filesandordirs; Name: "{app}\modules"
Type: filesandordirs; Name: "{app}\packages"

[Files]
Source: "{#Src}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Guide}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Register}"; DestDir: "{app}"; Flags: ignoreversion
#ifdef Tools
Source: "{#Tools}\*"; DestDir: "{app}\packages\tools"; Flags: ignoreversion
#endif

[Icons]
Name: "{autoprograms}\ZeroLoss Controller"; Filename: "{app}\ZeroLoss.exe"
Name: "{autoprograms}\{cm:Guide}"; Filename: "{app}\{#GuideName}"
Name: "{autoprograms}\{cm:Register}"; Filename: "{app}\{#RegisterName}"
Name: "{autodesktop}\ZeroLoss Controller"; Filename: "{app}\ZeroLoss.exe"

[Run]
; inbound rules for this program's own files only, by exact name (removed by that name)
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Controller service"""; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Controller screen"""; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Controller iperf3"""; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ZeroLoss Controller service"" dir=in action=allow program=""{app}\cli\zeroloss.exe"" enable=yes profile=any"; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ZeroLoss Controller screen"" dir=in action=allow program=""{app}\ZeroLoss.exe"" enable=yes profile=any"; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""ZeroLoss Controller iperf3"" dir=in action=allow program=""{app}\cli\iperf3.exe"" enable=yes profile=any"; Flags: runhidden; Check: IsAdminInstallMode
Filename: "{app}\ZeroLoss.exe"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Controller service"""; Flags: runhidden; RunOnceId: "fw1"; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Controller screen"""; Flags: runhidden; RunOnceId: "fw2"; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""ZeroLoss Controller iperf3"""; Flags: runhidden; RunOnceId: "fw3"; Check: IsAdminInstallMode

[UninstallDelete]
Type: filesandordirs; Name: "{app}\modules"
Type: filesandordirs; Name: "{app}\packages"
Type: dirifempty; Name: "{app}"
Type: dirifempty; Name: "{autopf}\ZeroLoss"

[Code]
const
  Base = 'http://127.0.0.1:{#CtlPort}/v1/controller';

{ The Controller service on this PC, whichever copy runs it (06 1 7). }
function Ask(const Verb, Path, Body: String; var Text: String): Boolean;
var
  Req: Variant;
begin
  Result := False;
  try
    Req := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Req.SetTimeouts(2000, 2000, 5000, 5000);
    Req.Open(Verb, Base + Path, False);
    Req.SetRequestHeader('Content-Type', 'application/json');
    Req.SetRequestHeader('X-ZL-Controller-Boot', 'installer');
    Req.Send(Body);
    Text := Req.ResponseText;
    Result := True;
  except
  end;
end;

function Generation(const Text: String): String;
var
  I: Integer;
begin
  Result := '';
  I := Pos('"generation":', Text);
  if I = 0 then
    Exit;
  I := I + Length('"generation":');
  while (I <= Length(Text)) and (Text[I] >= '0') and (Text[I] <= '9') do
  begin
    Result := Result + Text[I];
    I := I + 1;
  end;
end;

{ '' when no service is left; else why not. A test or the broadcast keeps it. }
function EndService(): String;
var
  Text, Gen: String;
  I: Integer;
begin
  Result := '';
  if not Ask('GET', '/status', '', Text) then
    Exit;
  if Pos('"state":"IDLE"', Text) = 0 then
  begin
    Result := CustomMessage('Running');
    Exit;
  end;
  Gen := Generation(Text);
  if Gen = '' then
    Gen := '0';
  Ask('POST', '/quit', '{"run_id":null,"command_id":"installer-' + GetDateTimeString('hhnnsszzz', #0, #0)
      + '","generation":' + Gen + ',"params":{}}', Text);
  if Pos('"ok":true', Text) = 0 then
  begin
    Result := CustomMessage('Running');
    Exit;
  end;
  for I := 1 to 40 do
  begin
    Sleep(500);
    if not Ask('GET', '/status', '', Text) then
      Exit;
  end;
  Result := CustomMessage('NotEnded');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := EndService();
end;

function InitializeUninstall(): Boolean;
var
  Why: String;
begin
  Why := EndService();
  Result := Why = '';
  if not Result then
    SuppressibleMsgBox(Why, mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  { default No: results are the person's (06 1 5) }
  if SuppressibleMsgBox(CustomMessage('KeepData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2,
                        IDNO) = IDYES then
  begin
    DelTree(ExpandConstant('{commonappdata}\ZeroLoss\Controller'), True, True, True);
    RemoveDir(ExpandConstant('{commonappdata}\ZeroLoss'));
    DelTree(ExpandConstant('{sd}\ZeroLoss\Reports'), True, True, True);
    RemoveDir(ExpandConstant('{sd}\ZeroLoss'));
  end;
end;
