; Inno Setup script for Wer (Windows Eos Recorder).
;
; Compiled by build.ps1 -Installer, which builds the one-folder payload first
; and passes the version in. Compiling this by hand works too:
;
;   ISCC.exe /DAppVersion=1.0.0 installer\wer.iss
;
; Why an installer at all, when dist\Wer is already a folder you can copy:
; one file is what you can actually send someone, but Qt is LGPL and users must
; be able to replace its libraries. A one-file PyInstaller exe hides them in a
; temp directory that is recreated every launch. This gives both -- a single
; artifact to hand over, which lays out the ordinary folder on disk.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

#define AppName "Windows Eos Recorder"
#define AppShortName "Wer"
#define AppPublisher "Hudco Lighting LLC"
#define AppURL "https://hudco.lighting"
#define AppContact "wer@hudco.lighting"
; Must stay identical to APP_USER_MODEL_ID in src\wer\app.py.
#define AppUserModelID "HudCo.Wer.WindowsEosRecorder"

[Setup]
; Never change AppId: it is how Windows recognises an existing installation to
; upgrade or uninstall. A new GUID would leave the old copy stranded in
; Add/Remove Programs with nothing able to remove it.
AppId={{F47964D4-CF1D-4041-A98B-D92CEED9F508}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Windows shows this in Add/Remove Programs and in the setup exe's own file
; properties, which is where someone checks who a signed-by-nobody installer
; claims to come from. GPL v3 section 5(a) wants the notice kept on the work;
; this is the copy that travels with the artifact itself.
AppCopyright=Copyright (C) 2026 {#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
AppContact={#AppContact}

; Per-user by default, so there is no UAC prompt and no need for an
; administrator. Wer runs on booth and venue laptops where the operator often
; is not one. {autopf} resolves to %LOCALAPPDATA%\Programs here; an admin who
; wants Program Files can still pass /ALLUSERS on the command line.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes

; GPL v3. Shown before anything is written, which is the point at which someone
; deciding whether to accept it can still decline.
LicenseFile=..\LICENSE

; The payload is ~260 MB of Qt, Python, OpenCV and ffmpeg, and compresses hard.
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=..\dist
OutputBaseFilename={#AppShortName}-{#AppVersion}-setup
SetupIconFile=..\assets\wer.ico
UninstallDisplayIcon={app}\{#AppShortName}.exe
UninstallDisplayName={#AppName}
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole one-folder build, minus the logs. Those are written by whoever ran
; the build and are none of the recipient's business -- and the app makes its
; own on first run.
Source: "..\dist\Wer\Wer.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\Wer\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; The licence travels with the binary, not only inside Help -> Licences.
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; And the notices for everyone else's work. LGPL v3 section 4(a) wants prominent
; notice with each COPY, not only inside the running program, so this sits beside
; the exe rather than only in _internal\.
Source: "..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; AppUserModelID on every shortcut that launches Wer, and it must match
; APP_USER_MODEL_ID in src\wer\app.py exactly. Wer tells Windows what it is
; called at startup, and Windows resolves the taskbar button's icon and name
; through that identity. With no shortcut claiming it, the shell has nothing to
; resolve: the title bar shows the right icon and the taskbar does not, which
; is the confusing half of the symptom.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppShortName}.exe"; AppUserModelID: "{#AppUserModelID}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppShortName}.exe"; Tasks: desktopicon; AppUserModelID: "{#AppUserModelID}"

[Run]
Filename: "{app}\{#AppShortName}.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Logs the app wrote next to its own exe. Settings, show files and recordings
; live in %LOCALAPPDATA%\Wer and under Videos\Wer, and are deliberately left
; alone: uninstalling a program should not destroy the work done with it.
Type: filesandordirs; Name: "{app}\logs"
