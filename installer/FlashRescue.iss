; FlashRescue installer (Inno Setup 6). Built by the GitHub release workflow, or by
; installer\build_installer.bat on a PC with Inno Setup installed.
;   iscc /DAppVersion=1.7.0 /DSourceDir=..\dist\FlashRescue installer\FlashRescue.iss
; Installs for the current user (no administrator rights needed to install, so updates install
; quietly). FlashRescue itself asks for administrator rights when it starts, because it reads
; drives directly. Settings live in %APPDATA%\FlashRescue and ffmpeg (if downloaded) in
; %LOCALAPPDATA%\FlashRescue - never touched by installing, updating or uninstalling.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\FlashRescue"
#endif

[Setup]
AppId={{3B8E5D2A-7C41-4F09-A6E2-5D1C9F4B7E31}
AppName=FlashRescue
AppVersion={#AppVersion}
AppVerName=FlashRescue {#AppVersion}
AppPublisher=FlashRescue
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\FlashRescue
DefaultGroupName=FlashRescue
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=FlashRescue-Setup-{#AppVersion}
SetupIconFile=..\src\icon.ico
UninstallDisplayIcon={app}\FlashRescue.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; an update closes a running FlashRescue first and starts it again afterwards
CloseApplications=force
RestartApplications=no
; refresh Windows' icon cache after installing, so the new FlashRescue.exe shows its own icon on
; the taskbar straight away (FaceSort showed a generic icon until the second start without this)
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; files from the previous version that this one no longer has
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; the same app id FlashRescue gives itself, so the taskbar matches the running window to these
; shortcuts (and their icon) at once - also on the first start after an update
Name: "{autoprograms}\FlashRescue"; Filename: "{app}\FlashRescue.exe"; IconFilename: "{app}\FlashRescue.exe"; AppUserModelID: "FlashRescue.App"
Name: "{autodesktop}\FlashRescue"; Filename: "{app}\FlashRescue.exe"; IconFilename: "{app}\FlashRescue.exe"; AppUserModelID: "FlashRescue.App"; Tasks: desktopicon

[Run]
; after installing - and after a quiet update from inside FlashRescue - start it (again).
; shellexec: FlashRescue asks for administrator rights, which needs the Windows prompt when the
; installer itself isn't running as administrator
Filename: "{app}\FlashRescue.exe"; Description: "Start FlashRescue"; Flags: nowait postinstall shellexec

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
