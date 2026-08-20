#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#ifndef MySourceDir
  #error MySourceDir must point to the portable application directory.
#endif

#ifndef MyOutputDir
  #define MyOutputDir SourcePath
#endif

#define MyAppName "ND2 ROI Mapper"
#define MyAppExeName "ND2 ROI Mapper.exe"

[Setup]
AppId={{DC32F147-B8CC-4993-B6A6-310FDBECF419}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
SetupArchitecture=x64
MinVersion=10.0.17763
OutputDir={#MyOutputDir}
OutputBaseFilename=ND2-ROI-Mapper-Setup-v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=auto
UninstallDisplayIcon={app}\{#MyAppExeName}
AppReadmeFile={app}\README.md
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductVersion={#MyAppVersion}.0
VersionInfoProductTextVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
