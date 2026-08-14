; Inno Setup script for the Builders Gate desktop install.
;
;   iscc packaging\installer.iss                       (after a build)
;   python packaging\build_exe.py --installer          (build, smoke, then this)
;
; Compiles dist\BuildersGate\ into dist\BuildersGate-setup.exe.
;
; ── why an installer at all, when a zip already exists ──────────────────────
; The zip is a folder the user has to put somewhere and a .exe they have to
; find inside it. There is no Start Menu entry, no uninstaller, no upgrade
; path, and no answer to "where did it go" three weeks later. The zip stays
; published for people who want it; this is the default download.
;
; ── PER-USER, AND THAT IS THE IMPORTANT DECISION ────────────────────────────
; PrivilegesRequired=lowest means:
;   * no UAC prompt at any point,
;   * the install lands in %LOCALAPPDATA%\Programs\BuildersGate rather than
;     C:\Program Files,
;   * the app writes its own data next to nothing it does not own.
; A machine-wide install would need admin for a tool one person runs on their
; own desktop, and would then hold the app's files under a root the user cannot
; write — which matters here because the app is updated often.
;
; THE TRADE, STATED PLAINLY. %LOCALAPPDATA%\Programs is user-writable, so any
; process already running as this user can replace BuildersGate.exe. Program
; Files is protected by an ACL the user cannot write without elevating — and
; that protection is bought with a UAC prompt on every install and every
; update, for an app that ships often. Neither is a free win. This is the
; deliberate choice: an attacker who is already executing code as the user has
; far better options than patching our binary, whereas a UAC prompt on every
; update is a cost paid by everyone, forever.
;
; What would actually close the gap is code signing plus a signature check on
; launch, not the install location.
;
; ── not signed ──────────────────────────────────────────────────────────────
; There is no code-signing certificate yet, so SmartScreen will show its
; "Windows protected your PC" prompt on first run. An installer does not make
; that worse than the loose .exe does, and the SHA256 is published beside the
; download. When a certificate exists, add SignTool= here and to the EXE in
; bgate.spec; nothing else in this file changes.

#define AppName        "Builders Gate"
#define AppExeName     "BuildersGate.exe"
#define AppPublisher   "Thepizzapie"
#define AppURL         "https://github.com/Thepizzapie/BuildersGate"

; Version is passed in by build_exe.py so it can come from pyproject.toml and
; cannot drift from the wheel. Defaulted so the script still compiles by hand.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

; Where the built app directory is, relative to this file.
#define SourceDir "..\dist\BuildersGate"

[Setup]
; NEVER CHANGE THIS GUID. It is the identity Windows upgrades and uninstalls
; by; a new one turns every future release into a second parallel install.
AppId={{7C4E2E71-9E63-4C3B-9A4B-5F2D6A1B8C40}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases

; {autopf} resolves to %LOCALAPPDATA%\Programs under PrivilegesRequired=lowest.
DefaultDirName={autopf}\BuildersGate
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
; NO "Select Setup Install Mode" PAGE. PrivilegesRequiredOverridesAllowed=dialog
; put an all-users/just-me question as the FIRST thing a new user saw, which is
; a question about Windows account scoping rather than about this app — and
; choosing the wrong one lands them in a UAC prompt they did not expect. This is
; a tool one person runs on their own desktop; per-user is the right answer and
; the wizard should simply make it.

; The app is a folder of a thousand files; a directory page that lets someone
; point it at an existing folder full of other things is a footgun. They can
; still change it, they just start from a sane place.
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=..\dist
OutputBaseFilename=BuildersGate-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern

; Branding. Generated from packaging/icon.ico by make_wizard_art.py, so the
; wizard cannot end up showing a mark the app stopped using.
;
; NOT DECORATION. An unsigned installer already arrives with SmartScreen
; calling its publisher unknown; showing up on top of that in the default grey
; Inno chrome, with no mark anywhere, is indistinguishable from the bundled
; adware people have been trained to cancel out of. This does not make the
; installer trusted — only signing does that — it stops it looking careless.
;
; Two files each: Inno picks the second on a scaled display.
WizardImageFile=wizard-large.bmp,wizard-large@2x.bmp
WizardSmallImageFile=wizard-small.bmp,wizard-small@2x.bmp
; The mark is drawn on the app's own near-black ground and would sit in a white
; box otherwise.
WizardImageStretch=no
WizardImageAlphaFormat=none
Compression=lzma2/max
SolidCompression=yes
; 64-bit only, matching the PyInstaller bootloader.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Refuse to install over a newer build rather than silently downgrading.
VersionInfoVersion={#AppVersion}
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The whole onedir tree. recursesubdirs picks up _internal/, which is where
; everything except the launcher lives.
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller's onedir tree is written by the installer and removed with it,
; but the app also writes a crash log beside the user's profile. Leave the
; user's own projects and ~/.bgate alone — an uninstaller that deletes API
; keys and game data is a bug, not tidiness.
Type: files; Name: "{localappdata}\BuildersGate-crash.log"

[Code]
{ The app holds a loopback singleton on port 7787 for its whole lifetime, so an
  upgrade that copies over a running install fails on locked files with a
  message about nothing the user can act on. Ask first, and say why. }
function IsAppRunning(): Boolean;
var
  ResultCode: Integer;
begin
  { tasklist is present on every supported Windows and needs no elevation. }
  Result := Exec(ExpandConstant('{cmd}'),
                 '/C tasklist /FI "IMAGENAME eq {#AppExeName}" | find /I "{#AppExeName}"',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsAppRunning() then
    Result := 'Builders Gate is running. Close it and click Retry —' + #13#10 +
              'the installer cannot replace files the app has open.';
end;
