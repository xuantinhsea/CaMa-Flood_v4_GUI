; ============================================================================
;  CaMa_Floodcast -- Windows installer (Inno Setup 6)
;
;  Built by build_installer.bat, which first produces everything this script
;  packs:
;    dist\CaMa_Floodcast\            the frozen application   (build_exe.bat)
;    build\installer_stage\runtime\  MinGW runtime DLLs + model manifest
;    build\installer_stage\docs\     user manual (.docx/.md), NOTICE, licences
;    build\installer_stage\data\     example projects, their maps and forcing
;                                    (packaging\stage_installer.py)
;
;  Two install locations, on purpose:
;    {app}            the program -- per-user (%LOCALAPPDATA%\Programs) by
;                     default, or Program Files for all users
;    {code:DataDir}   the example workspace -- must be writable, since running
;                     a case writes its results there; %USERPROFILE% by default
;  Workspace files are never overwritten by an upgrade and never removed by
;  the uninstaller: after installation they are the user's own data.
; ============================================================================

#ifndef AppVersion
  #define AppVersion "1.0"
#endif
#define AppName      "CaMa_Floodcast"
#define AppExe       "CaMa_Floodcast.exe"
#define Publisher    "Nippon Koei Co.,Ltd."
#define PublisherURL "https://www.n-koei.co.jp/consulting/english/"
#define Root         ".."
#define Stage        Root + "\build\installer_stage"

[Setup]
AppId={{6C7B3E52-8F0A-4C2E-9D41-3A5E7F0B9C17}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#PublisherURL}
AppSupportURL={#PublisherURL}
AppContact=tinh-ng@n-koei.jp
AppCopyright=Copyright (C) 2026 Nippon Koei Co.,Ltd. Water Resources & Energy Dept.
VersionInfoVersion={#AppVersion}.0.0
VersionInfoCompany={#Publisher}
VersionInfoDescription={#AppName} {#AppVersion} Setup
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

LicenseFile=NOTICE.txt
SetupIconFile={#Root}\camafloodcast\resources\camafloodcast.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
WizardStyle=modern
OutputDir={#Root}\dist\installer
OutputBaseFilename={#AppName}_Setup_{#AppVersion}

Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "full";    Description: "Full installation (application and all example projects)"
Name: "compact"; Description: "Application only"
Name: "custom";  Description: "Custom installation"; Flags: iscustom

[Components]
Name: "app";                  Description: "CaMa_Floodcast application, CaMa-Flood v4.30 model and user manual"; Types: full compact custom; Flags: fixed
Name: "examples";             Description: "Example projects (with results, river maps and forcing)"; Types: full
Name: "examples\mekong";      Description: "Mekong basin -- 4 cases: baseline, no bifurcation, with dams, with levee"; Types: full
Name: "examples\vietnam";     Description: "Vietnam (national) -- a truncated national cut (Gate 1 demonstration)"; Types: full
Name: "examples\philippines"; Description: "Philippines (national) -- a hydrologically closed national cut"; Types: full
Name: "examples\cagayan";     Description: "Cagayan River basin -- a small catchment-scale cut"; Types: full

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; ---- the application -------------------------------------------------------
Source: "{#Root}\dist\CaMa_Floodcast\*"; DestDir: "{app}"; Components: app; Flags: ignoreversion recursesubdirs createallsubdirs
; MinGW runtime beside MAIN_cmf.exe and the tools (found there first -- services/binaries.py)
Source: "{#Stage}\runtime\*"; DestDir: "{app}\bin\win64"; Components: app; Flags: ignoreversion
Source: "{#Stage}\docs\*";    DestDir: "{app}\docs";      Components: app; Flags: ignoreversion

; ---- example workspace: user data once installed -----------------------------
#define DataFlags "recursesubdirs createallsubdirs onlyifdoesntexist uninsneveruninstall"
Source: "{#Stage}\data\projects\mekong\*";      DestDir: "{code:DataDir}\projects\mekong";      Components: examples\mekong;      Flags: {#DataFlags}
Source: "{#Stage}\data\map\mekong_15min\*";     DestDir: "{code:DataDir}\map\mekong_15min";     Components: examples\mekong;      Flags: {#DataFlags}
Source: "{#Stage}\data\projects\vietnam\*";     DestDir: "{code:DataDir}\projects\vietnam";     Components: examples\vietnam;     Flags: {#DataFlags}
Source: "{#Stage}\data\map\vnm_15min\*";        DestDir: "{code:DataDir}\map\vnm_15min";        Components: examples\vietnam;     Flags: {#DataFlags}
Source: "{#Stage}\data\projects\philippines\*"; DestDir: "{code:DataDir}\projects\philippines"; Components: examples\philippines; Flags: {#DataFlags}
Source: "{#Stage}\data\map\phl_15min\*";        DestDir: "{code:DataDir}\map\phl_15min";        Components: examples\philippines; Flags: {#DataFlags}
Source: "{#Stage}\data\projects\cagayan\*";     DestDir: "{code:DataDir}\projects\cagayan";     Components: examples\cagayan;     Flags: {#DataFlags}
Source: "{#Stage}\data\map\cagayan_15min\*";    DestDir: "{code:DataDir}\map\cagayan_15min";    Components: examples\cagayan;     Flags: {#DataFlags}
; the 1-degree runoff forcing every example uses
Source: "{#Stage}\data\inp\*"; DestDir: "{code:DataDir}\inp"; Components: examples\mekong or examples\vietnam or examples\philippines or examples\cagayan; Flags: {#DataFlags}

[Icons]
Name: "{group}\{#AppName}";               Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Mekong example";           Filename: "{app}\{#AppExe}"; Parameters: "--project ""{code:DataDir}\projects\mekong"""; WorkingDir: "{app}"; Comment: "Open the Mekong basin example project"; Components: examples\mekong
Name: "{group}\Example projects folder";  Filename: "{code:DataDir}\projects"; Components: examples\mekong or examples\vietnam or examples\philippines or examples\cagayan
Name: "{group}\User Manual";              Filename: "{app}\docs\USER_MANUAL.docx"
Name: "{group}\Uninstall {#AppName}";     Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";         Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--project ""{code:DataDir}\projects\mekong"""; WorkingDir: "{app}"; Description: "Launch {#AppName} with the Mekong example"; Components: examples\mekong; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "Launch {#AppName}"; Components: not examples\mekong; Flags: nowait postinstall skipifsilent

[Code]
var
  DataDirPage: TInputDirWizardPage;

function AnyExampleSelected: Boolean;
begin
  Result := WizardIsComponentSelected('examples\mekong') or
            WizardIsComponentSelected('examples\vietnam') or
            WizardIsComponentSelected('examples\philippines') or
            WizardIsComponentSelected('examples\cagayan');
end;

// The workspace folder -- the "code:DataDir" constant used in the sections above.
function DataDir(Param: String): String;
begin
  Result := RemoveBackslashUnlessRoot(DataDirPage.Values[0]);
end;

procedure InitializeWizard;
begin
  DataDirPage := CreateInputDirPage(wpSelectComponents,
    'Workspace folder',
    'Where should the example projects be installed?',
    'The example projects, their river maps and runoff forcing are installed into this ' +
    'folder, as projects\, map\ and inp\ side by side.' + #13#10#13#10 +
    'It must be a folder you can write to: running a case writes its results there. ' +
    'Avoid a folder synchronised by OneDrive or similar. Files already in this folder ' +
    'are never overwritten, and the uninstaller leaves the folder in place.',
    False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := GetPreviousData('DataDir',
    ExpandConstant('{%USERPROFILE}\CaMa_Floodcast'));
  // Scripted installs: setup.exe /VERYSILENT /DATADIR="D:\Hydro\CaMa_Floodcast"
  if ExpandConstant('{param:DATADIR}') <> '' then
    DataDirPage.Values[0] := ExpandConstant('{param:DATADIR}');
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', DataDirPage.Values[0]);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = DataDirPage.ID) and not AnyExampleSelected;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    // The map viewer's QtWebEngine files sit ~80 characters inside the folder, and
    // Windows will not load a DLL whose full path exceeds 260 characters.
    if Length(WizardDirValue) > 120 then
    begin
      MsgBox('Please choose a shorter application folder (at most 120 characters).' + #13#10 +
             'Parts of the program sit deep inside it, and Windows cannot load files whose ' +
             'full path is longer than 260 characters.', mbError, MB_OK);
      Result := False;
    end;
  end
  else if CurPageID = DataDirPage.ID then
  begin
    Dir := Lowercase(AddBackslash(DataDirPage.Values[0]));
    if (Pos(Lowercase(AddBackslash(ExpandConstant('{commonpf}'))), Dir) = 1) or
       (Pos(Lowercase(AddBackslash(ExpandConstant('{win}'))), Dir) = 1) then
    begin
      MsgBox('The workspace folder must be writable. Please choose a folder outside ' +
             'Program Files and Windows, for example in your user folder.', mbError, MB_OK);
      Result := False;
    end
    else if Length(DataDirPage.Values[0]) > 150 then
    begin
      MsgBox('Please choose a shorter workspace folder (at most 150 characters).',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine;
  if AnyExampleSelected then
    Result := Result + 'Workspace folder (example projects):' + NewLine +
              Space + DataDir('') + NewLine + NewLine;
  Result := Result + MemoTypeInfo + NewLine + NewLine + MemoComponentsInfo;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + NewLine + MemoTasksInfo;
end;
