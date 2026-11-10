; Instalátor Maileru (Inno Setup 6). Sestavuje ho build.bat ze složky dist\Mailer:
;   iscc /DAppVersion=1.0.0 installer.iss
; Instaluje se jen pro aktuálního uživatele (bez práv správce) do %LOCALAPPDATA%\Programs\Mailer.
; Odinstalace data aplikace (%APPDATA%\Mailer) ani hesla ve Správci pověření nemaže.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{7C4E2A51-3B8F-4D6A-9E21-5F0C8B3D1A64}
AppName=Mailer
AppVersion={#AppVersion}
AppVerName=Mailer {#AppVersion}
DefaultDirName={localappdata}\Programs\Mailer
DefaultGroupName=Mailer
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=dist
OutputBaseFilename=Mailer-Setup-{#AppVersion}
SetupIconFile=assets\mailer.ico
UninstallDisplayIcon={app}\Mailer.exe
UninstallDisplayName=Mailer
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "czech"; MessagesFile: "compiler:Languages\Czech.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; soubory staré verze (jiné verze knihoven) by se jinak mohly načíst
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\Mailer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Mailer"; Filename: "{app}\Mailer.exe"; AppUserModelID: "Mailer.Desktop"
Name: "{autodesktop}\Mailer"; Filename: "{app}\Mailer.exe"; AppUserModelID: "Mailer.Desktop"; Tasks: desktopicon

[Run]
Filename: "{app}\Mailer.exe"; Description: "{cm:LaunchProgram,Mailer}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2(): Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'SOFTWARE\' + WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'Software\' + WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssPostInstall) and not HasWebView2() and not WizardSilent() then
    if MsgBox('Mailer potřebuje Microsoft Edge WebView2 Runtime, který na tomto počítači chybí.' + #13#10#13#10 +
              'Chcete otevřít stránku Microsoftu, odkud ho stáhnete (zvolte „Evergreen Bootstrapper“)?',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;
