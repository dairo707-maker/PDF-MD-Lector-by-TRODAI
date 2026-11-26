; Inno Setup 6 script for PDF/MD Lector by TRODAI — Multilingual Per-User Windows 11 Installer
#ifndef MyAppVersion
  #define MyAppVersion "0.3.0"
#endif

#define MyAppName "PDF/MD Lector"
#define MyAppSafeName "PDF-MD-Lector"
#define MyAppPublisher "TRODAI (Dairo Trocha) - Open Source MIT"
#define MyAppURL "https://github.com/dairo707-maker/PDF-MD-Lector-by-TRODAI"
#define MyAppExeName "PDF-MD-Lector.exe"
#define MyAppUserModelID "TRODAI.PDFMDLector"
#define SourceAppDir "..\dist\PDF-MD-Lector"

[Setup]
AppId=TRODAI.PDFMDLector
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\TRODAI\{#MyAppSafeName}
DisableDirPage=no
AllowNoIcons=yes
DisableProgramGroupPage=no
DefaultGroupName={#MyAppSafeName} by TRODAI
LicenseFile=..\LICENSE
SetupIconFile=..\assets\icons\pdf_lector.ico
WizardImageFile=..\assets\icons\wizard_large.bmp,..\assets\icons\wizard_large_150.bmp,..\assets\icons\wizard_large_200.bmp
WizardSmallImageFile=..\assets\icons\wizard_small.bmp,..\assets\icons\wizard_small_150.bmp,..\assets\icons\wizard_small_200.bmp
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} by TRODAI
ChangesAssociations=yes
OutputDir=..\dist
OutputBaseFilename=PDF-MD-Lector-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=yes
#ifdef MySignTool
SignTool=signtool
SignedUninstaller=yes
#endif

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "pt_br"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "pt"; MessagesFile: "compiler:Languages\Portuguese.isl"

[CustomMessages]
es.DesktopIcon=Crear un acceso directo en el Escritorio
en.DesktopIcon=Create a Desktop shortcut
pt_br.DesktopIcon=Criar um atalho na Area de Trabalho
pt.DesktopIcon=Criar um atalho no Ambiente de Trabalho

es.StartMenuIcon=Crear acceso directo en el Menu Inicio
en.StartMenuIcon=Create Start Menu shortcut
pt_br.StartMenuIcon=Criar atalho no Menu Iniciar
pt.StartMenuIcon=Criar atalho no Menu Iniciar

es.ShortcutsGroup=Accesos directos:
en.ShortcutsGroup=Shortcuts:
pt_br.ShortcutsGroup=Atalhos:
pt.ShortcutsGroup=Atalhos:

es.IntegrationGroup=Integracion con Windows 11 (solo usuario actual HKCU):
en.IntegrationGroup=Windows 11 integration (current user HKCU only):
pt_br.IntegrationGroup=Integracao com Windows 11 (apenas usuario atual HKCU):
pt.IntegrationGroup=Integracao com Windows 11 (apenas utilizador atual HKCU):

es.AssocPdfTask=Registrar en "Abrir con" para archivos .pdf
en.AssocPdfTask=Register in "Open with" for .pdf files
pt_br.AssocPdfTask=Registrar em "Abrir com" para arquivos .pdf
pt.AssocPdfTask=Registar em "Abrir com" para ficheiros .pdf

es.AssocMdTask=Registrar en "Abrir con" para archivos Markdown (.md / .markdown)
en.AssocMdTask=Register in "Open with" for Markdown files (.md / .markdown)
pt_br.AssocMdTask=Registrar em "Abrir com" para arquivos Markdown (.md / .markdown)
pt.AssocMdTask=Registar em "Abrir com" para ficheiros Markdown (.md / .markdown)

es.LaunchNow=Ejecutar {#MyAppName} ahora
en.LaunchNow=Launch {#MyAppName} now
pt_br.LaunchNow=Executar {#MyAppName} agora
pt.LaunchNow=Executar {#MyAppName} agora

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; GroupDescription: "{cm:ShortcutsGroup}"
Name: "startmenuicon"; Description: "{cm:StartMenuIcon}"; GroupDescription: "{cm:ShortcutsGroup}"
Name: "assocpdf"; Description: "{cm:AssocPdfTask}"; GroupDescription: "{cm:IntegrationGroup}"
Name: "assocmd"; Description: "{cm:AssocMdTask}"; GroupDescription: "{cm:IntegrationGroup}"; Flags: unchecked

[Files]
Source: "{#SourceAppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\ARCHITECTURE.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\SECURITY_AND_PRIVACY.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\docs\CODE_SIGNING_AND_INSTALL.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppSafeName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"
Name: "{group}\Uninstall {#MyAppSafeName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppSafeName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"; Tasks: desktopicon
Name: "{autoprograms}\{#MyAppSafeName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"; Tasks: startmenuicon

[Registry]
; Persist selected installer language for first launch
Root: HKCU; Subkey: "Software\TRODAI\PDFLector"; ValueType: string; ValueName: "Language"; ValueData: "{language}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\TRODAI\PDFLector"; ValueType: string; ValueName: "AppUserModelID"; ValueData: "{#MyAppUserModelID}"; Flags: uninsdeletekey

; Optional per-user HKCU ProgID registration for .pdf
Root: HKCU; Subkey: "Software\Classes\TRODAI.PDFMDLector.PDF"; ValueType: string; ValueName: ""; ValueData: "PDF Document (PDF/MD Lector by TRODAI)"; Flags: uninsdeletekey; Tasks: assocpdf
Root: HKCU; Subkey: "Software\Classes\TRODAI.PDFMDLector.PDF\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Flags: uninsdeletekey; Tasks: assocpdf
Root: HKCU; Subkey: "Software\Classes\TRODAI.PDFMDLector.PDF\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey; Tasks: assocpdf
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "TRODAI.PDFMDLector.PDF"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assocpdf

; Optional per-user HKCU ProgID registration for .md / .markdown
Root: HKCU; Subkey: "Software\Classes\TRODAI.PDFMDLector.MD"; ValueType: string; ValueName: ""; ValueData: "Markdown Document (PDF/MD Lector by TRODAI)"; Flags: uninsdeletekey; Tasks: assocmd
Root: HKCU; Subkey: "Software\Classes\TRODAI.PDFMDLector.MD\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Flags: uninsdeletekey; Tasks: assocmd
Root: HKCU; Subkey: "Software\Classes\TRODAI.PDFMDLector.MD\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Flags: uninsdeletekey; Tasks: assocmd
Root: HKCU; Subkey: "Software\Classes\.md\OpenWithProgids"; ValueType: string; ValueName: "TRODAI.PDFMDLector.MD"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assocmd
Root: HKCU; Subkey: "Software\Classes\.markdown\OpenWithProgids"; ValueType: string; ValueName: "TRODAI.PDFMDLector.MD"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assocmd

[Run]
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden skipifdoesntexist
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\docs"
Type: dirifempty; Name: "{app}"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /T /IM PDF-MD-Lector.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(400);
  Exec('cmd.exe', '/c tasklist /FI "IMAGENAME eq PDF-MD-Lector.exe" | find /I "PDF-MD-Lector.exe" >nul', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if ResultCode = 0 then
    Result := 'PDF/MD Lector sigue en ejecucion. Cierra todas las ventanas abiertas y vuelve a intentarlo.'
  else
    Result := '';
end;
