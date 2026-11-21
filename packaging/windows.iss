[Setup]
AppId={{0E4C2DB9-0C6C-45F3-8EC0-FE10E89A784E}
AppName=CuePorch
AppVersion=0.3.0
AppPublisher=ForkCommons
DefaultDirName={localappdata}\Programs\ForkCommons\CuePorch
DefaultGroupName=ForkCommons
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CuePorch-0.3.0-Windows-x64-Setup
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\CuePorch.exe
Compression=lzma2/fast
SolidCompression=yes
#ifdef Signed
SignTool=owner
SignedUninstaller=yes
#endif
[Files]
Source: "..\dist\layout\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\CuePorch"; Filename: "{app}\CuePorch.exe"; Parameters: "--data ""{code:DataDirectory}"""
Name: "{autodesktop}\CuePorch"; Filename: "{app}\CuePorch.exe"; Parameters: "--data ""{code:DataDirectory}"""; Tasks: desktopicon
[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked
[Run]
Filename: "{app}\CuePorch.exe"; Parameters: "--data ""{code:DataDirectory}"""; Description: "Open CuePorch"; Flags: postinstall nowait skipifsilent
[Code]
var LibraryPage: TInputDirWizardPage;
procedure InitializeWizard;
begin
  LibraryPage := CreateInputDirPage(wpSelectDir, 'Your video library', 'Where should CuePorch keep your programs and settings?', 'For a new library, keep the suggested folder. To restore a library, close CuePorch and choose the restored folder containing operator.json and institution.sqlite. Uninstalling CuePorch keeps this folder.', False, '');
  LibraryPage.Add('');
  LibraryPage.Values[0] := ExpandConstant('{param:LIBRARY|' + GetPreviousData('LibraryDirectory',ExpandConstant('{localappdata}\ForkCommons\CuePorch\Data')) + '}');
end;
function DataDirectory(Param: string): string;
begin Result := LibraryPage.Values[0]; end;
function NextButtonClick(CurPageID: Integer): Boolean;
var Data, Binaries: string;
begin
  Result := True;
  if CurPageID = LibraryPage.ID then begin
    Data := AddBackslash(ExpandFileName(LibraryPage.Values[0]));
    Binaries := AddBackslash(ExpandFileName(WizardDirValue));
    if (Pos(Lowercase(Binaries), Lowercase(Data)) = 1) or (Pos(Lowercase(Data), Lowercase(Binaries)) = 1) then begin
      MsgBox('Choose a library folder separate from the program folder.', mbError, MB_OK); Result := False;
    end;
  end;
end;
procedure RegisterPreviousData(PreviousDataKey: Integer);
begin SetPreviousData(PreviousDataKey, 'LibraryDirectory', LibraryPage.Values[0]); end;
