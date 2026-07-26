#define MyAppName "ALGA VECTOR"
#define MyAppVersion "0.7.0"
#define MyAppPublisher "Буйвол и Задира"
#define MyAppExeName "ALGA VECTOR.exe"

[Setup]
AppId={{89534FD7-10A1-40F9-BDC0-5387F11123A1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ALGA VECTOR
DefaultGroupName=ALGA VECTOR
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=ALGA-VECTOR-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "..\dist\ALGA VECTOR\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ALGA VECTOR"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\ALGA VECTOR"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные значки:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить ALGA VECTOR"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; User captures, configuration, logs and legacy profile data are intentionally not deleted here.
Type: filesandordirs; Name: "{app}\__pycache__"
